"""The approval gate — the guarantee that makes this safe to point at a real mailbox.

**Structural, not advisory.** `Send` has no code path to `EmailSurface.act()` without a
recorded `Decision(verdict="approve")` matched to that exact payload. It is expressed as
graph topology and a dispatch-time check, never as a line in a prompt — because the model
reads attacker-controlled email, and anything expressed only in a prompt is negotiable by
that text.

Four properties, each load-bearing:

- **Payload-bound.** Approving a draft to P3 does not authorize the same verb aimed at P9 a
  turn later. A single "yes" must never become a standing permission.
- **Legible.** The preview shows the *resolved* recipient — a human cannot verify "send to
  P17", and checking the recipient is the entire point of the gate.
- **Bounded.** Approvals expire. A pending approval never becomes an implicit yes.
- **Non-delegable.** No remediation strategy, rule, or prompt can produce a decision. Only a
  human answering the interrupt can.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from inbox_contracts import ActionCall, Observation
from pydantic import BaseModel, ConfigDict

from app.manager.draft import Draft
from app.surface.dispatch import approval_fingerprint
from app.workers.irreversible import (
    IRREVERSIBLE_NAMES,
    SENDING_KEYS,
    is_irreversible,
    target_name,
)


class Verdict(StrEnum):
    APPROVE = "approve"
    #: Replace a field and return to the loop. Explicitly NOT an approval — an edited draft
    #: is a different draft, and it has to be looked at again.
    EDIT = "edit"
    REJECT = "reject"
    #: The deadline passed with no answer. Distinct from a rejection because it is a
    #: different fact about the world — nobody declined, nobody was there — and it earns
    #: its own typed error code.
    EXPIRED = "expired"


#: What kind of irreversible thing is being asked about. Drives how the cockpit renders it.
Kind = Literal["send", "invite", "delete", "bulk"]

KIND_FOR_VERB: dict[str, Kind] = {
    "Send": "send",
    "SendInvite": "invite",
    "DeleteForever": "delete",
}


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    kind: Kind
    #: One line for the cockpit's header: "Send an email to Priya Nair".
    summary: str
    #: The full RESOLVED draft, for human eyes only. Never re-enters `messages`, the
    #: trajectory, or any LLM request — see `EventEmitter.approval_request`.
    preview: str
    #: Identity of the exact payload this authorizes.
    fingerprint: str
    reversible: bool = False
    expires_at: datetime

    @property
    def expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at


def _looks_like_send(call: ActionCall, target: str) -> bool:
    """Does this un-named action put mail beyond recall?

    Narrow on purpose: only a send-shaped target, or the keystroke Gmail treats as send.
    Everything else keeps the honest "bulk" wording rather than claiming to know more than
    it does.
    """
    if SENDING_KEYS.match(str(call.args.get("key", ""))):
        return True
    return bool(IRREVERSIBLE_NAMES.match(target or ""))


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)

    verdict: Verdict
    #: For EDIT: what the human wants instead, in words ("add regards").
    edit: str = ""
    #: For EDIT: the draft as the human RETYPED it, when they edited the preview directly.
    #:
    #: Takes precedence over `edit`, and is applied verbatim — no model call. Asking a model
    #: to "apply" text a human has already written is how a correction turns into a rewrite:
    #: told to fix a duplicated greeting, it returned a body with the greeting deleted and
    #: the sign-off reworded. If the human has typed the exact words they want, the only
    #: correct thing to do with them is use them.
    edited_preview: str = ""
    reason: str = ""

    @property
    def approved(self) -> bool:
        return self.verdict is Verdict.APPROVE


def is_gated(call: ActionCall | None, observation: Observation | None = None) -> bool:
    """Does this action need a human decision before it dispatches?

    Delegates to `is_irreversible`, which asks what the action DOES rather than what it is
    named — a `Click` on Gmail's Send button sends the mail just as surely as the `Send`
    verb, and gating only the verb left that path wide open.
    """
    return is_irreversible(call, observation)


def build_request(
    call: ActionCall,
    *,
    request_id: str,
    preview: str,
    timeout_seconds: int,
    observation: Observation | None = None,
) -> ApprovalRequest:
    """Describe one irreversible action for a human to decide on."""
    # By consequence where the verb alone does not say it. A `Click` on Gmail's Send button
    # is a send, and labelling that card "About to make a BULK CHANGE — Run Click" describes
    # neither what is happening nor how serious it is. The gate already decided this action
    # is irreversible; the card should say the same thing in words a person can act on.
    kind = KIND_FOR_VERB.get(call.name)
    if kind is None:
        target = target_name(observation, call.args.get("index"))
        kind = "send" if _looks_like_send(call, target) else "bulk"
    summaries: dict[Kind, str] = {
        "send": "Send this email",
        "invite": "Send this calendar invite",
        "delete": "Permanently delete this — it cannot be undone",
        "bulk": f"Run {call.name}",
    }
    return ApprovalRequest(
        request_id=request_id,
        kind=kind,
        summary=summaries[kind],
        preview=preview,
        # Includes the preview: consent covers these exact words, not this button.
        fingerprint=approval_fingerprint(call, preview),
        expires_at=datetime.now(UTC) + timedelta(seconds=timeout_seconds),
    )


def draft_from_preview(text: str, *, tone: str = "professional") -> Draft | None:
    """Read a `Draft` back out of the preview format this module renders.

    Parsing our own output, not guessing at arbitrary text: the shape is fixed a few lines
    above in `PlaywrightEmailSurface.preview` — a `To:` line, a `Subject:` line, a blank
    line, then the body. That makes this reliable in a way parsing an email generally is not.

    Returns `None` when the text does not match, so a caller can fall back to the
    instruction path rather than silently sending a draft assembled from a misread.

    The `To:` line is deliberately IGNORED. Recipients are chips in the live compose window
    and are changed by typing tokens, never by editing this text — accepting a recipient
    from here would take an address straight from a text box into a send, which is the one
    path the vault exists to prevent.
    """
    if not text.strip():
        return None

    lines = text.splitlines()
    subject = ""
    body_start = 0
    for position, line in enumerate(lines):
        lowered = line.strip().lower()
        if lowered.startswith("subject:"):
            subject = line.split(":", 1)[1].strip()
            body_start = position + 1
            break
        if lowered.startswith("to:"):
            body_start = position + 1
    else:
        return None

    # Skip the blank line the renderer puts between the headers and the body.
    while body_start < len(lines) and not lines[body_start].strip():
        body_start += 1

    body = "\n".join(lines[body_start:]).strip()
    if not body:
        return None
    if subject == "(empty)":
        subject = ""
    return Draft(subject=subject, body=body, tone=tone)


def recipients_from_preview(text: str) -> str | None:
    """The `To:` line of a preview, or `None` if there is no header to read.

    Separate from `draft_from_preview` because the recipient is not part of the draft: it
    lives in `intent.slots` and, in the browser, it is a CHIP rather than text. Editing it
    therefore needs a different repair — remove a chip, type a token — and merging the two
    would hide that behind a field assignment that cannot work.

    Returns `""` for an explicitly empty To line, which is meaningfully different from
    `None`: the human clearing the recipient is an instruction, the absence of a header is
    a preview this function cannot read.
    """
    for line in text.splitlines():
        if line.strip().lower().startswith("to:"):
            value = line.split(":", 1)[1].strip()
            return "" if value == "(empty)" else value
    return None


def decision_from(payload: object) -> Decision:
    """Parse whatever came back through the interrupt.

    Anything unrecognisable becomes a REJECT. Failing closed is the only safe default: a
    malformed resume must never read as consent, and the cost of being wrong here is an
    email the user did not authorize.
    """
    if isinstance(payload, Decision):
        return payload
    if isinstance(payload, dict):
        raw = str(payload.get("verdict", "")).lower()
        if raw in tuple(Verdict):
            return Decision(
                verdict=Verdict(raw),
                edit=str(payload.get("edit") or ""),
                edited_preview=str(payload.get("editedPreview") or ""),
                reason=str(payload.get("reason") or ""),
            )
    if isinstance(payload, str) and payload.lower() in tuple(Verdict):
        return Decision(verdict=Verdict(payload.lower()))
    return Decision(verdict=Verdict.REJECT, reason="no valid decision was returned")
