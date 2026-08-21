"""The approval gate node.

Lives beside the rest of approval rather than in `graph.py`, because a graph module should
read as *topology* — which nodes exist and how they connect. A reader auditing "can anything
send without a human?" should be able to answer it from the edge list in twelve lines, and
that only works if the edges are not buried under node bodies.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from langgraph.types import interrupt

from app.agent.state import AgentState
from app.events.emitter import EventEmitter
from app.llm.base import Message
from app.manager.draft import Draft
from app.surface.base import EmailSurface
from app.surface.dispatch import approval_fingerprint
from app.telemetry.records import ErrorCode
from app.workers.approval import Verdict, build_request, decision_from, is_gated

logger = logging.getLogger(__name__)

#: Applies ONE human correction to an existing draft, changing only what was asked.
#: Built by `build_reviser` in `manager.writer`; a plain callable here so the gate depends on
#: the capability rather than on the writer module.
Reviser = Callable[[Draft, str], Awaitable[Draft]]


def build_approval_gate_node(
    surface: EmailSurface,
    emitter: EventEmitter,
    *,
    timeout_seconds: int = 600,
    revise: Reviser | None = None,
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
            delta: dict = {"last_action": None}  # nothing is dispatched; the loop re-decides
            instruction = decision.edit

            # Revise the DRAFT rather than hand the instruction to the loop.
            #
            # Told "change the last sentence", the worker retyped the whole body from
            # scratch, and it never came back the same: a greeting the human had already
            # approved would quietly acquire new punctuation, a sentence they liked would be
            # "improved". They then had to re-read the entire email to find an edit they
            # never asked for. Revising against the existing text changes what was asked and
            # returns everything else byte for byte.
            if revise is not None and isinstance(state.draft, Draft) and instruction:
                delta["draft"] = await revise(state.draft, instruction)
                delta["messages"] = [
                    Message(
                        role="user",
                        content=(
                            f"Do not send yet. The human asked: {instruction}\n"
                            "The draft above has been updated for you. Retype only the "
                            "field that changed, leave the others alone, then propose "
                            "sending again."
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
