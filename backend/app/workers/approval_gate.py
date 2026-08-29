"""The approval gate node.

Lives beside the rest of approval rather than in `graph.py`, because a graph module should
read as *topology* — which nodes exist and how they connect. A reader auditing "can anything
send without a human?" should be able to answer it from the edge list in twelve lines, and
that only works if the edges are not buried under node bodies.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from hashlib import sha256

from langgraph.types import interrupt

from app.agent.state import AgentState
from app.events.emitter import EventEmitter
from app.feedback.models import Feedback, FeedbackKind
from app.feedback.store import FeedbackStore
from app.llm.base import Message
from app.manager.draft import Draft
from app.security.patterns import TOKEN_RE
from app.security.vault import trust_addresses
from app.surface.base import EmailSurface
from app.surface.dispatch import approval_fingerprint
from app.telemetry.records import ErrorCode
from app.workers.approval import (
    Verdict,
    build_request,
    decision_from,
    draft_from_preview,
    is_gated,
    recipients_from_preview,
)

logger = logging.getLogger(__name__)

#: Applies ONE human correction to an existing draft, changing only what was asked.
#: Built by `build_reviser` in `manager.writer`; a plain callable here so the gate depends on
#: the capability rather than on the writer module.
Reviser = Callable[[Draft, str], Awaitable[Draft]]

#: Which feedback kind each verdict is. The gate is the only place in the system where a
#: human passes explicit judgement on a *specific proposed action*, which makes it the
#: richest signal available — and until now the only one that was thrown away.
#:
#: EDIT maps to CORRECTION rather than to its own kind deliberately: `FeedbackStore.
#: candidates()` counts CORRECTIONs to find recurring preferences, and "add regards",
#: "shorter please", "don't cc them" said across three different runs is *exactly* the
#: standing-rule signal that promotion path exists to catch. Filing edits anywhere else
#: would leave the promotion counter reading zero forever.
_KIND_FOR_VERDICT: dict[Verdict, FeedbackKind] = {
    Verdict.APPROVE: FeedbackKind.ENDORSEMENT,
    Verdict.EDIT: FeedbackKind.CORRECTION,
    Verdict.REJECT: FeedbackKind.REJECTION,
}


async def _record(
    feedback: FeedbackStore | None,
    state: AgentState,
    decision,
    summary: str,
) -> None:
    """File the human's verdict as feedback. Best-effort, and never with the preview in it.

    **The preview must not be stored, and this is the whole reason this is a function rather
    than three inline calls.** `preview` is the RESOLVED draft — real addresses, real body
    text, deliberately un-tokenized so a human can actually verify what they are approving.
    It is built for one authenticated cockpit and one pair of eyes. The feedback store is a
    persisted, cross-run, cross-thread surface that `candidates()` reads back out. Putting a
    resolved draft in there would quietly undo the vault one approval at a time.

    So what gets stored is `request.summary` — "Send this email", from a fixed table in
    `approval.py` keyed on the verb — plus the verb itself. Both are structural. The one
    exception is EDIT, whose text is the human's own instruction: that is their words, it is
    what the promotion path needs to count, and it is exactly what the existing mid-run
    feedback channel in `api/ws.py` already records.

    A failure here must never take down a run that a human just successfully approved, so it
    is logged and swallowed. Losing a learning signal is a bad day; losing the send the user
    just authorised is a broken product.
    """
    if feedback is None:
        return

    kind = _KIND_FOR_VERDICT.get(decision.verdict)
    if kind is None:  # EXPIRED — nobody decided anything, so there is nothing to learn
        return

    if kind is FeedbackKind.CORRECTION:
        text = decision.edit.strip()
    else:
        text = (decision.reason or "").strip() or summary
    if not text:
        return

    try:
        await feedback.record(
            Feedback(
                thread_id=state.thread_id,
                kind=kind,
                text=text,
                step=state.step,
                action=state.last_action.name if state.last_action else None,
                # Already delivered: the human said it TO the gate, and the gate has acted
                # on it in this same turn. Leaving it pending would have the loop replay
                # their own decision back at them as fresh guidance next turn.
                applied=True,
            )
        )
    except Exception:  # pragma: no cover - defensive
        logger.warning("could not record approval feedback", exc_info=True)


#: An edit that is about WHO the mail goes to, rather than what it says.
#:
#: Deliberately requires an explicit phrase, not merely a token: an edit like "mention P5 in
#: the first line" contains a token and is about the BODY. Guessing wrong here changes the
#: recipient of an email, which is the one field where a wrong guess is unrecoverable.
_RECIPIENT_EDIT = re.compile(
    r"\b(recipient|addressee|to\s*field|send\s+(?:it\s+)?to|mail\s+(?:it\s+)?to)\b",
    re.IGNORECASE,
)


def _recipient_change(instruction: str) -> str | None:
    """The new recipient an edit asks for, as tokens — or `None` if it asks for no such thing.

    **The gap this closes.** `Draft` holds subject, body and tone; the recipient lives in
    `intent.slots`. So "change the recipient to P5" went to the reviser, which rewrites the
    words, found nothing about the words to change, and `_stale_fields` named only subject
    and body. The instruction was understood, acted on, and had no effect on who the mail was
    addressed to — the human watched their correction be acknowledged and ignored.
    """
    if not _RECIPIENT_EDIT.search(instruction):
        return None
    tokens = list(dict.fromkeys(TOKEN_RE.findall(instruction)))
    return ", ".join(tokens) or None


def _retyped_recipient(shown: str, edited: str, vault) -> str | None:
    """The recipient the human typed into the box's `To:` line — or `None` if unchanged.

    **Why this is allowed to mint an addressable token.** The address arrived in the
    approval card, typed by the operator, in answer to "is this right?". That is the same
    trust boundary intake applies to the task and the context gate applies to an answer:
    the human typed it, so it is somewhere they meant to write. Nothing a *message* said
    can reach here — the preview is our own rendering, and the only thing read out of it is
    the line the human edited.

    The minting is also what makes the change possible at all. The dispatcher accepts
    recipients as vault tokens and nothing else, so an address left raw here would be
    refused as `UNKNOWN_TOKEN` no matter how clearly the human asked.

    A name rather than an address ("Biyash") is passed through as typed: Gmail's To field
    autocompletes from contacts, which is exactly how a person would do it.
    """
    if not edited:
        return None
    was = recipients_from_preview(shown)
    now = recipients_from_preview(edited)
    if now is None or was is None or now.strip() == was.strip():
        return None
    if not now.strip():
        # Clearing the recipient is not a retarget, and guessing at one would be worse than
        # leaving it: the loop asks.
        logger.info("the human emptied the To line in the approval box")
        return None
    return trust_addresses(now.strip(), vault)


def _recipient_lines(state: AgentState, tokens: str) -> list[str]:
    """How to swap the recipient, in the terms Gmail actually needs.

    Not "Clear the To field": a committed recipient is a CHIP, a separate node, and clearing
    the input beside it leaves the chip in place — the new address would be added alongside
    the old one and the mail would go to both. Removing the chip is its own click, and the
    chip is in the element list where the worker can see it.
    """
    mail = getattr(state.observation, "mail", None)
    index = getattr(mail, "to_index", None)
    at = f" [{index}]" if index is not None else ""
    return [
        "  - Remove the recipient already in the To field by clicking the × on its "
        "chip (it is in the list below).",
        f"  - Then type {tokens} into the To field{at}.",
    ]


def _replace_recipient(state: AgentState, tokens: str) -> str:
    """The recipient-only correction, as one message."""
    return _correction_message(state, recipient=tokens)


def _stale_fields(before: Draft | None, after: Draft) -> list[str]:
    """Which compose fields the browser now holds the WRONG text for."""
    if before is None:
        return ["subject", "body"]
    changed = []
    if before.subject.strip() != after.subject.strip():
        changed.append("subject")
    if before.body.strip() != after.body.strip():
        changed.append("body")
    return changed


def _draft_lines(state: AgentState, before: Draft | None, after: Draft) -> list[str]:
    """Which compose fields to rewrite, and where they are. Empty when nothing changed."""
    mail = getattr(state.observation, "mail", None)
    where = {
        "subject": getattr(mail, "subject_index", None),
        "body": getattr(mail, "body_index", None),
    }
    lines = []
    for field in _stale_fields(before, after):
        index = where.get(field)
        at = f" [{index}]" if index is not None else ""
        lines.append(
            f"  - {field.capitalize()}{at}: Clear it, then Type the new {field} exactly as "
            "it appears above."
        )
    return lines


def _correction_message(
    state: AgentState,
    *,
    recipient: str | None = None,
    before: Draft | None = None,
    after: Draft | None = None,
) -> str:
    """One message for one human correction — which may touch the recipient, the words, or both.

    **Composed rather than concatenated.** The recipient instruction ends with "leave the
    subject and body exactly as they are" and the draft instruction ends with "leave the
    recipient alone". Emitting both for an edit that changed the recipient AND the body
    hands the worker two directly contradictory orders, and it will obey one of them —
    silently dropping half of what the human just typed.
    """
    blocks: list[str] = []
    if recipient:
        blocks += _recipient_lines(state, recipient)
    if after is not None:
        blocks += _draft_lines(state, before, after)

    if not blocks:
        return (
            "Do not send yet. Nothing in the draft actually changed — the compose window is "
            "already correct. Propose sending again."
        )

    what = []
    if recipient:
        what.append(f"the RECIPIENT to {recipient}")
    if after is not None and _stale_fields(before, after):
        what.append("the words of the message")

    trailers = []
    if recipient:
        trailers.append(
            "Do NOT Clear the To field — that empties the input beside the chip and leaves "
            "the old recipient attached, so the mail would go to both."
        )
        if after is None:
            trailers.append("Leave the subject and body exactly as they are.")
    if after is not None and _stale_fields(before, after):
        trailers.append(
            "These fields will still show as FILLED — that means stale, not correct, "
            "so overwrite them."
        )
        if recipient is None:
            trailers.append("Leave the recipient alone.")

    return (
        f"Do not send yet. The human changed {' and '.join(what)}, and the compose window "
        "still holds the OLD text.\n"
        "Replace exactly this and nothing else:\n"
        + "\n".join(blocks)
        + "\n"
        + " ".join(trailers)
        + " Then propose sending again."
    )


def _apply_revision(state: AgentState, before: Draft | None, after: Draft) -> str:
    """Tell the worker exactly which fields to rewrite, and where they are.

    **The instruction this replaces asked for something the agent cannot do.** "Retype only
    the fields that changed" requires comparing the new draft against what is in the compose
    window — and the worker is never shown field CONTENTS, by design, because a To field
    holds an address and a body holds whatever was written. So "which changed?" was
    unanswerable from its side. Cornered, it asked the human to type the body out again.

    Two more things worked against it. The compose window still held the OLD text, and the
    observation reported those fields as FILLED — where the standing rule is "a FILLED field
    is done". The guard that stops a recipient being typed twice was, at that moment, the
    thing preventing the human's own edit from ever being applied.

    So the comparison happens HERE, where both drafts actually exist, and the result is
    named: which fields, at which index, with an explicit licence to overwrite them.
    """
    return _correction_message(state, before=before, after=after)


def build_approval_gate_node(
    surface: EmailSurface,
    emitter: EventEmitter,
    *,
    timeout_seconds: int = 600,
    revise: Reviser | None = None,
    feedback: FeedbackStore | None = None,
    vault=None,
):
    """Pause for a human before anything irreversible.

    The interrupt is what makes this durable: the run is checkpointed and stopped, not a
    coroutine parked in memory. A user can close the tab, come back in five minutes, and
    the draft is still sitting there waiting — which is the difference between a gate and
    a race against an HTTP timeout.

    Three outcomes, and only one of them sends:
      approve → the exact payload is authorized, then dispatched
      edit    → the human's text replaces the field; the loop resumes. NOT an approval:
                an edited draft is a different draft and has to be looked at again.
      reject  → nothing is dispatched; the agent offers an alternative or completes false.
    """

    async def approval_gate(state: AgentState) -> dict:
        call = state.last_action
        if not is_gated(call, state.observation):
            return {}

        # RESOLVED, from the executor: a human cannot verify "send to P17".
        preview = await surface.preview(call)
        # Derived from the payload AND the words, not random. Two properties, both load-bearing:
        #
        #   * Deterministic, so the node re-executing on resume presents ONE pending decision
        #     rather than several.
        #   * Sensitive to the draft, so a decision about different words is a different
        #     decision. **This is the bug that made "Apply & review" look broken.** The id was
        #     `Send|index=108` and nothing else, so after an edit the gate re-asked under the
        #     id the human had already answered — and it was then suppressed twice over: the
        #     emitter dropped it as a replay, and the cockpit hid it as already-answered. The
        #     run sat at the interrupt with no card, which reads exactly like "my edit did
        #     nothing". `fingerprint` has always included the preview for the same reason
        #     consent is about the email, not the button; the id simply never followed.
        #
        # Hashed rather than truncated, and that is not cosmetic: the fingerprint reads
        # `Send|index=108|content=…`, so the first sixteen characters are the verb and the
        # index — the content hash falls off the end, and adding the preview would have
        # changed nothing at all.
        request_id = f"ap-{sha256(approval_fingerprint(call, preview).encode()).hexdigest()[:16]}"
        request = build_request(
            call,
            request_id=request_id,
            preview=preview,
            timeout_seconds=timeout_seconds,
            # So a Click on the Send button is described as a send, not as "Run Click".
            observation=state.observation,
        )

        await emitter.approval_request(
            request_id=request.request_id,
            kind=request.kind,
            summary=request.summary,
            preview=request.preview,
            expires_at=request.expires_at.isoformat(),
        )

        raw = interrupt(
            {
                "approval": True,
                "requestId": request.request_id,
                "kind": request.kind,
                "summary": request.summary,
                "preview": request.preview,
                "expiresAt": request.expires_at.isoformat(),
            }
        )
        decision = decision_from(raw)
        await emitter.approval_result(request.request_id, decision.verdict.value)
        await _record(feedback, state, decision, request.summary)

        # The deadline is enforced by the transport, which is where the waiting actually
        # happens. It cannot be enforced here: this node re-executes on resume, so
        # `expires_at` is recomputed to a fresh future time and would never have elapsed.
        if decision.verdict is Verdict.EXPIRED or request.expired:
            # A pending approval never becomes an implicit yes.
            return {
                "status": "failed",
                "error_code": ErrorCode.APPROVAL_TIMEOUT,
                "finished": True,
                "success": False,
                "reason": "the approval expired before anyone answered",
            }

        if decision.verdict is Verdict.APPROVE:
            # Authorize THIS payload only. The surface refuses anything else.
            surface.approve(request.fingerprint)
            return {"messages": [Message(role="user", content="Approved — go ahead.")]}

        if decision.verdict is Verdict.EDIT:
            delta: dict = {"last_action": None}  # nothing is dispatched; the loop re-decides
            instruction = decision.edit
            typed = decision.edited_preview.strip()
            before = state.draft if isinstance(state.draft, Draft) else None

            # ── who it goes to ──
            # Two ways to say it, and both have to work: an instruction ("send it to P5"),
            # or editing the To: line in the box directly. The box is the one people reach
            # for, and it used to be read for its subject and body and ignored for its To
            # line — so a human retargeting the email watched it go to the original
            # recipient anyway. A recipient is not a draft field: it is a chip in the
            # compose window, changed by removing one and typing a token.
            new_recipient = _recipient_change(instruction) or _retyped_recipient(
                request.preview, typed, vault
            )

            # ── what it says ──
            # The human retyped the draft themselves — use their words, exactly.
            #
            # No model call, and that is the point. Asked to fix a duplicated greeting, the
            # reviser returned a body with the greeting deleted and the sign-off reworded;
            # the user's complaint was that correcting one sentence rewrote the email. When
            # somebody has typed the exact text they want, the only correct thing to do with
            # it is use it.
            revised: Draft | None = None
            unreadable = False
            if typed and typed != request.preview.strip():
                revised = draft_from_preview(
                    typed, tone=before.tone if before is not None else "professional"
                )
                # Not silently: falling through to an empty instruction is how a human's
                # retyped email became "Change it: " and then nothing at all.
                unreadable = revised is None

            # Revise the DRAFT rather than hand the instruction to the loop.
            #
            # Told "change the last sentence", the worker retyped the whole body from
            # scratch, and it never came back the same: a greeting the human had already
            # approved would quietly acquire new punctuation, a sentence they liked would be
            # "improved". They then had to re-read the entire email to find an edit they
            # never asked for. Revising against the existing text changes what was asked and
            # returns everything else byte for byte.
            #
            # Skipped when the instruction was ABOUT the recipient — there are no words to
            # rewrite — and when the human has already typed the exact draft they want.
            asked_in_words = ""
            if (
                revised is None
                and instruction
                and not new_recipient
                and revise is not None
                and before is not None
            ):
                revised = await revise(before, instruction)
                asked_in_words = f"The human asked: {instruction}\n"

            if new_recipient or revised is not None:
                logger.info(
                    "correction applied — recipient: %s, stale fields: %s",
                    new_recipient or "unchanged",
                    ", ".join(_stale_fields(before, revised)) if revised else "none",
                )
                updated = state.intent
                if new_recipient and updated is not None:
                    updated = updated.with_slots(recipient_identity=new_recipient)
                return {
                    **delta,
                    **({"draft": revised} if revised is not None else {}),
                    **({"intent": updated} if new_recipient and updated is not None else {}),
                    "messages": [
                        Message(
                            role="user",
                            content=asked_in_words
                            + _correction_message(
                                state,
                                recipient=new_recipient,
                                before=before,
                                after=revised,
                            ),
                        )
                    ],
                }

            if unreadable:
                # The human edited the box into something with no readable header or no
                # body left. Saying so beats acting on a misread email.
                return {
                    **delta,
                    "messages": [
                        Message(
                            role="user",
                            content=(
                                "Do not send yet. The human edited the draft, but the text "
                                "they left could not be read back as an email (it needs a "
                                "To: or Subject: line and a body). Ask them what it should "
                                "say, using AskUser."
                            ),
                        )
                    ],
                }

            delta["messages"] = [
                Message(
                    role="user",
                    content=(
                        f"Do not send yet. Change it: {instruction}\n"
                        "Change ONLY what was asked; leave every other word exactly as it "
                        "is. Then propose sending again."
                    ),
                )
            ]
            return delta

        return {
            "last_action": None,
            "messages": [
                Message(
                    role="user",
                    content=(
                        f"I declined that. {decision.reason or ''} "
                        "Suggest an alternative, or call Complete(success=false)."
                    ).strip(),
                )
            ],
        }

    return approval_gate
