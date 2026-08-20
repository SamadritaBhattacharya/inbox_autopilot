"""The manager graph — PRE phase, compiled.

    START -> intake -> context_gate -+-> ask (interrupt) -> context_gate
                                     +-> router -+-> planner -> finalize
                                     |           +-> (linear) -> finalize
                                     +-> finalize (gave up, typed)

**The topology is the guarantee.** There is no edge from `intake` to `router`, so "the agent
won't start without full context" is not a rule anyone has to remember — it is a path that
does not exist. A reviewer can confirm it by reading twelve lines of edges rather than by
auditing every node for an early return.

Compiled with a checkpointer, which buys three things that would each be substantial to
build: durable pause/resume, human interrupts that survive a process restart, and a
trajectory keyed by `thread_id`.

Workers, approval, verify, and self-heal attach to `dispatch` in later milestones. The PRE
phase is complete and its edges will not change when they do.
"""
from __future__ import annotations

import logging

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agent.recovery_nodes import (
    build_diagnose_node,
    build_options_node,
    build_verify_node,
)
from app.agent.routing import (
    ACT,
    APPROVAL,
    ASK,
    DIAGNOSE,
    DISPATCH,
    FINALIZE,
    LINEAR,
    OBSERVE,
    OPTIONS,
    PLANNER,
    REASON,
    ROUTER,
    VERIFY,
    route_after_act,
    route_after_approval,
    route_after_diagnose,
    route_after_dispatch,
    route_after_gate,
    route_after_observe,
    route_after_options,
    route_after_reason,
    route_after_router,
    route_after_verify,
)
from app.agent.state import AgentState
from app.events.emitter import EventEmitter
from app.events.sink import NullSink
from app.feedback.store import FeedbackStore
from app.llm.base import LLMClient, Message
from app.manager.intent import Action
from app.manager.nodes import (
    build_context_gate_node,
    build_intake_node,
    build_planner_node,
    build_router_node,
)
from app.recovery.registry import CuratedSkillRegistry
from app.rules.store import InMemoryRulesStore, RulesStore
from app.surface.base import EmailSurface
from app.surface.dispatch import approval_fingerprint
from app.telemetry.records import ErrorCode, StepRecord
from app.workers.approval import (
    Verdict,
    build_request,
    decision_from,
    is_gated,
)
from app.workers.loop import build_act_node, build_observe_node, build_reason_node
from app.workers.registry import worker_for
from app.workers.rules_worker import build_linear_node

logger = logging.getLogger(__name__)

#: Custom types the checkpointer is allowed to round-trip.
#:
#: LangGraph refuses unregistered types on deserialization (a warning today, an error
#: tomorrow), and the failure mode is quiet and nasty: state silently loses a field, and the
#: field it loses most often is `error_code` — because a FAILED run is exactly the one being
#: resumed. An untyped failure is the one thing this system is not allowed to produce, so
#: the allowlist is explicit rather than left to a default.
ALLOWED_CHECKPOINT_MODULES = (
    ("app.manager.intent", "Action"),
    ("app.manager.intent", "TaskIntent"),
    ("app.manager.intent", "Route"),
    ("app.manager.intent", "Plan"),
    ("app.telemetry.records", "StepRecord"),
    ("app.telemetry.records", "ErrorCode"),
    ("app.telemetry.records", "Usage"),
    ("app.llm.base", "Message"),
    ("app.llm.base", "ToolCall"),
    ("app.recovery.causes", "Cause"),
    ("app.recovery.causes", "Diagnosis"),
    ("inbox_contracts.models", "Observation"),
    ("inbox_contracts.models", "Element"),
    ("inbox_contracts.models", "Viewport"),
    ("inbox_contracts.models", "MailContext"),
    ("inbox_contracts.models", "ActionCall"),
    ("inbox_contracts.models", "ActionResult"),
)


def default_checkpointer() -> InMemorySaver:
    """A saver that can actually round-trip this graph's state."""
    return InMemorySaver(
        serde=JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_CHECKPOINT_MODULES)
    )


def build_ask_node():
    """The AskUser interrupt.

    `interrupt()` raises out of the node and is caught by the runtime, which checkpoints and
    stops. Resuming with `Command(resume=answer)` re-enters this node and `interrupt()`
    returns that answer. The run is genuinely paused — no coroutine parked in memory, no
    socket held open — so it survives a restart and a cockpit reconnect.
    """

    async def ask(state: AgentState) -> dict:
        answer = interrupt(
            {
                "question": state.pending_question or "Could you clarify?",
                "missing": state.missing_slots,
                "task": state.task,
            }
        )
        return {"answers": [str(answer)], "status": "gathering"}

    return ask


def build_finalize_node():
    """Resolve the terminal state in ONE place, and make sure it is typed.

    Centralised because "every terminal state carries an `ErrorCode`" is only true if there
    is a single exit. Scatter finalisation across nodes and one of them eventually returns
    without a code — and an untyped exit cannot be counted, diagnosed, or turned into a
    ranked remedy.
    """

    async def finalize(state: AgentState) -> dict:
        if state.is_terminal:
            delta: dict = {}
        elif state.route is not None:
            delta = {
                "status": "done",
                "success": True,
                "reason": state.reason or f"planned via the {state.route.topology} route",
            }
        else:
            # Reaching the end with no route means something skipped the router.
            delta = {
                "status": "failed",
                "success": False,
                "error_code": ErrorCode.NO_ACTION,
                "reason": "the run ended before any work was routed",
            }

        error = delta.get("error_code") or state.error_code
        logger.info(
            "finalize: %s%s",
            delta.get("status", state.status),
            f" ({error})" if error else "",
        )
        return {
            **delta,
            "finished": True,
            "history": [StepRecord(step=state.step, node="finalize", error_code=error)],
        }

    return finalize


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


def build_dispatch_node(emitter: EventEmitter):
    """Hand the run to a worker.

    Deliberately does NOT touch `active_worker`: the gate chose it from the intent, and it
    is the worker's *capability set*. `route.topology` is how that worker runs — linear or
    decision. Two different questions, and overwriting one with the other would hand a
    `summarize` request the triage tool set.
    """

    async def dispatch(state: AgentState) -> dict:
        worker = worker_for(state.intent.action if state.intent else Action.ANSWER)
        await emitter.status(
            "running",
            f"{worker.name} worker · {worker.purpose}"
            + (" · read-only" if worker.read_only else ""),
        )
        return {"active_worker": worker.name, "status": "running"}

    return dispatch


def tools_for_state(state: AgentState) -> tuple:
    """The capability set for this run, resolved at reason time.

    One loop serves every worker, so the binding has to come from state. It comes from the
    INTENT — fixed in PRE, before any message content was read — which is what stops a
    hostile email from widening what the agent can do mid-run.
    """
    return worker_for(state.intent.action if state.intent else Action.ANSWER).tools


def build_manager_graph(
    *,
    llm: LLMClient,
    surface: EmailSurface | None = None,
    emitter: EventEmitter | None = None,
    rules: RulesStore | None = None,
    registry: CuratedSkillRegistry | None = None,
    feedback: FeedbackStore | None = None,
    threshold: float = 0.85,
    max_steps: int = 40,
    approval_timeout_seconds: int = 600,
    checkpointer=None,
):
    """Compile the graph. Concretes come from the composition root.

    Without a `surface` the worker loop is omitted and `dispatch` terminates — which is what
    the PRE-phase tests want, and keeps "can this run without a browser?" an honest yes
    rather than a mock-shaped lie.
    """
    rules = rules or InMemoryRulesStore()
    registry = registry or CuratedSkillRegistry()
    emitter = emitter or EventEmitter(NullSink())

    graph = StateGraph(AgentState)
    graph.add_node("intake", build_intake_node(llm, emitter))
    graph.add_node("context_gate", build_context_gate_node(threshold=threshold))
    graph.add_node(ASK, build_ask_node())
    graph.add_node(ROUTER, build_router_node(llm, rules, emitter))
    graph.add_node(PLANNER, build_planner_node(llm, emitter))
    graph.add_node(DISPATCH, build_dispatch_node(emitter))
    graph.add_node(FINALIZE, build_finalize_node())

    if surface is not None:
        graph.add_node(OBSERVE, build_observe_node(surface, emitter))
        graph.add_node(
            REASON,
            build_reason_node(
                llm,
                emitter,
                tools=tools_for_state,
                max_steps=max_steps,
                feedback=feedback,
            ),
        )
        graph.add_node(ACT, build_act_node(surface, emitter, tools=tools_for_state))
        graph.add_node(LINEAR, build_linear_node(surface, emitter, rules))
        graph.add_node(VERIFY, build_verify_node(emitter))
        graph.add_node(DIAGNOSE, build_diagnose_node(emitter, registry))
        graph.add_node(OPTIONS, build_options_node(emitter, registry))
        graph.add_node(
            APPROVAL,
            build_approval_gate_node(
                surface, emitter, timeout_seconds=approval_timeout_seconds
            ),
        )

    graph.add_edge(START, "intake")
    # No intake -> router edge exists. That absence IS the 100%-context rule.
    graph.add_edge("intake", "context_gate")

    graph.add_conditional_edges(
        "context_gate",
        route_after_gate,
        {ASK: ASK, ROUTER: ROUTER, FINALIZE: FINALIZE},
    )
    # An answer returns to the gate, which re-evaluates. The loop is the point: it runs
    # until the context is complete or the ask budget is spent.
    graph.add_edge(ASK, "context_gate")

    graph.add_conditional_edges(
        ROUTER,
        route_after_router,
        {DISPATCH: DISPATCH, PLANNER: PLANNER, FINALIZE: FINALIZE},
    )
    graph.add_edge(PLANNER, DISPATCH)

    if surface is None:
        graph.add_edge(DISPATCH, FINALIZE)
    else:
        # observe -> reason -> act -> observe. `act` NEVER returns to `reason`: acting on a
        # stale observation is the most reliable way to click the wrong thing.
        graph.add_conditional_edges(
            DISPATCH,
            route_after_dispatch,
            {OBSERVE: OBSERVE, LINEAR: LINEAR, FINALIZE: FINALIZE},
        )
        # Deterministic work verifies too: 'the rule ran' and 'the rule worked' are
        # different claims, and only one of them is worth reporting.
        graph.add_edge(LINEAR, VERIFY)
        graph.add_conditional_edges(
            OBSERVE, route_after_observe, {REASON: REASON, VERIFY: VERIFY}
        )
        graph.add_conditional_edges(
            REASON,
            route_after_reason,
            {ACT: ACT, APPROVAL: APPROVAL, REASON: REASON, VERIFY: VERIFY},
        )
        graph.add_conditional_edges(
            APPROVAL,
            route_after_approval,
            {ACT: ACT, REASON: REASON, FINALIZE: FINALIZE},
        )
        graph.add_conditional_edges(
            ACT, route_after_act, {OBSERVE: OBSERVE, VERIFY: VERIFY}
        )
        # A run that thinks it is done is verified before it is believed; a verified
        # failure earns a diagnosis, and a diagnosis earns ranked options.
        graph.add_conditional_edges(
            VERIFY, route_after_verify, {FINALIZE: FINALIZE, DIAGNOSE: DIAGNOSE}
        )
        graph.add_conditional_edges(
            DIAGNOSE, route_after_diagnose, {OPTIONS: OPTIONS, FINALIZE: FINALIZE}
        )
        graph.add_conditional_edges(
            OPTIONS, route_after_options, {OBSERVE: OBSERVE, FINALIZE: FINALIZE}
        )

    graph.add_edge(FINALIZE, END)

    return graph.compile(checkpointer=checkpointer or default_checkpointer())


__all__ = ["build_manager_graph", "LINEAR"]
