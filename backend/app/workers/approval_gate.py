"""The approval gate node.

Lives beside the rest of approval rather than in `graph.py`, because a graph module should
read as *topology* — which nodes exist and how they connect. A reader auditing "can anything
send without a human?" should be able to answer it from the edge list in twelve lines, and
that only works if the edges are not buried under node bodies.
"""
from __future__ import annotations

import logging

from langgraph.types import interrupt

from app.agent.state import AgentState
from app.events.emitter import EventEmitter
from app.llm.base import Message
from app.surface.base import EmailSurface
from app.surface.dispatch import approval_fingerprint
from app.telemetry.records import ErrorCode
from app.workers.approval import Verdict, build_request, decision_from, is_gated

logger = logging.getLogger(__name__)


def build_approval_gate_node(
    surface: EmailSurface,
    emitter: EventEmitter,
    *,
    timeout_seconds: int = 600,
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
        if not is_gated(call):
            return {}

        # Derived from the payload, not random: this node re-executes when the run is
        # resumed, and a fresh id each time would present one pending decision as several.
        request_id = f"ap-{approval_fingerprint(call)[:16]}"
        # RESOLVED, from the executor: a human cannot verify "send to P17".
        preview = await surface.preview(call)
        request = build_request(
            call, request_id=request_id, preview=preview, timeout_seconds=timeout_seconds
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
            return {
                "last_action": None,  # nothing is dispatched; the loop re-decides
                "messages": [
                    Message(
                        role="user",
                        content=(
                            f"Do not send yet. Change it: {decision.edit}\n"
                            "Update the draft, then propose sending again."
                        ),
                    )
                ],
            }

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
