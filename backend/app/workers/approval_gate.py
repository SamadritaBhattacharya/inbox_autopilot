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

from langgraph.types import interrupt

from app.agent.state import AgentState
from app.events.emitter import EventEmitter
from app.feedback.models import Feedback, FeedbackKind
from app.feedback.store import FeedbackStore
from app.llm.base import Message
from app.manager.draft import Draft
from app.security.patterns import TOKEN_RE
from app.surface.base import EmailSurface
from app.surface.dispatch import approval_fingerprint
from app.telemetry.records import ErrorCode
from app.workers.approval import (
    Verdict,
    build_request,
    decision_from,
    draft_from_preview,
    is_gated,
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


def _replace_recipient(state: AgentState, tokens: str) -> str:
    """Tell the worker to swap the recipient, in the terms Gmail actually needs.

    Not "Clear the To field": a committed recipient is a CHIP, a separate node, and clearing
    the input beside it leaves the chip in place — the new address would be added alongside
    the old one and the mail would go to both. Removing the chip is its own click, and the
    chip is in the element list where the worker can see it.
    """
    mail = getattr(state.observation, "mail", None)
    index = getattr(mail, "to_index", None)
    at = f" [{index}]" if index is not None else ""
    return (
        f"Do not send yet. The human changed the RECIPIENT to {tokens}.\n"
        "  - Remove the recipient already in the To field by clicking the × on its "
        "chip (it is in the list below).\n"
        f"  - Then type {tokens} into the To field{at}.\n"
        "Do NOT Clear the To field — that empties the input beside the chip and leaves "
        "the old recipient attached, so the mail would go to both. Leave the subject "
        "and body exactly as they are. Then propose sending again."
    )


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
    stale = _stale_fields(before, after)
    if not stale:
        return (
            "Do not send yet. Nothing in the draft actually changed — the compose window is "
            "already correct. Propose sending again."
        )

    mail = getattr(state.observation, "mail", None)
    where = {
        "subject": getattr(mail, "subject_index", None),
        "body": getattr(mail, "body_index", None),
    }
    lines = []
    for field in stale:
        index = where.get(field)
        at = f" [{index}]" if index is not None else ""
        lines.append(
            f"  - {field.capitalize()}{at}: Clear it, then Type the new {field} exactly as "
            "it appears above."
        )

    return (
        "Do not send yet. The draft above has changed, and the compose window still holds "
        "the OLD text.\n"
        "Replace exactly these fields and nothing else:\n"
        + "\n".join(lines)
        + "\nThese fields will still show as FILLED — that means stale, not correct, "
        "so overwrite them. Leave the recipient alone. Then propose sending again."
    )


def build_approval_gate_node(
    surface: EmailSurface,
    emitter: EventEmitter,
    *,
    timeout_seconds: int = 600,
    revise: Reviser | None = None,
    feedback: FeedbackStore | None = None,
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

        # Derived from the payload, not random: this node re-executes when the run is
        # resumed, and a fresh id each time would present one pending decision as several.
        request_id = f"ap-{approval_fingerprint(call)[:16]}"
        # RESOLVED, from the executor: a human cannot verify "send to P17".
        preview = await surface.preview(call)
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

            # The human retyped the draft themselves — use their words, exactly.
            #
            # No model call, and that is the point. Asked to fix a duplicated greeting, the
            # reviser returned a body with the greeting deleted and the sign-off reworded;
            # the user's complaint was that correcting one sentence rewrote the email. When
            # somebody has typed the exact text they want, the only correct thing to do with
            # it is use it. Falls through to the instruction path if the text does not parse,
            # so a mangled preview never becomes a silently wrong draft.
            # A recipient change is not a draft change, and has to be handled before the
            # reviser is asked to rewrite words that are not what the human wanted altered.
            if new_recipient := _recipient_change(instruction):
                logger.info("recipient change requested: %s", new_recipient)
                updated = state.intent
                if updated is not None:
                    updated = updated.with_slots(recipient_identity=new_recipient)
                return {
                    **delta,
                    **({"intent": updated} if updated is not None else {}),
                    "messages": [
                        Message(role="user", content=_replace_recipient(state, new_recipient))
                    ],
                }

            typed = decision.edited_preview.strip()
            if typed and typed != request.preview.strip():
                revised = draft_from_preview(
                    typed,
                    tone=state.draft.tone if isinstance(state.draft, Draft) else "professional",
                )
                if revised is not None:
                    before = state.draft if isinstance(state.draft, Draft) else None
                    logger.info(
                        "draft replaced with the human's own text; stale: %s",
                        ", ".join(_stale_fields(before, revised)) or "nothing",
                    )
                    return {
                        **delta,
                        "draft": revised,
                        "messages": [
                            Message(
                                role="user",
                                content=_apply_revision(state, before, revised),
                            )
                        ],
                    }

            # Revise the DRAFT rather than hand the instruction to the loop.
            #
            # Told "change the last sentence", the worker retyped the whole body from
            # scratch, and it never came back the same: a greeting the human had already
            # approved would quietly acquire new punctuation, a sentence they liked would be
            # "improved". They then had to re-read the entire email to find an edit they
            # never asked for. Revising against the existing text changes what was asked and
            # returns everything else byte for byte.
            if revise is not None and isinstance(state.draft, Draft) and instruction:
                before = state.draft
                revised = await revise(before, instruction)
                delta["draft"] = revised
                delta["messages"] = [
                    Message(
                        role="user",
                        content=(
                            f"The human asked: {instruction}\n"
                            + _apply_revision(state, before, revised)
                        ),
                    )
                ]
                return delta

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
