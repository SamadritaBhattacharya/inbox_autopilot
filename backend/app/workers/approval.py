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

from inbox_contracts import ActionCall
from pydantic import BaseModel, ConfigDict

from app.surface.dispatch import GATED_VERBS, approval_fingerprint


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


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)

    verdict: Verdict
    #: For EDIT: what the human wants instead.
    edit: str = ""
    reason: str = ""

    @property
    def approved(self) -> bool:
        return self.verdict is Verdict.APPROVE


def is_gated(call: ActionCall | None) -> bool:
    return call is not None and call.name in GATED_VERBS


def build_request(
    call: ActionCall,
    *,
    request_id: str,
    preview: str,
    timeout_seconds: int,
) -> ApprovalRequest:
    """Describe one irreversible action for a human to decide on."""
    kind = KIND_FOR_VERB.get(call.name, "bulk")
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
        fingerprint=approval_fingerprint(call),
        expires_at=datetime.now(UTC) + timedelta(seconds=timeout_seconds),
    )


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
                reason=str(payload.get("reason") or ""),
            )
    if isinstance(payload, str) and payload.lower() in tuple(Verdict):
        return Decision(verdict=Verdict(payload.lower()))
    return Decision(verdict=Verdict.REJECT, reason="no valid decision was returned")
