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
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agent.routing import (
    ACT,
    ASK,
    DISPATCH,
    FINALIZE,
    LINEAR,
    OBSERVE,
    PLANNER,
    REASON,
    ROUTER,
    route_after_act,
    route_after_dispatch,
    route_after_gate,
    route_after_observe,
    route_after_reason,
    route_after_router,
)
from app.agent.state import AgentState
from app.events.emitter import EventEmitter
from app.events.sink import NullSink
from app.feedback.store import FeedbackStore
from app.llm.base import LLMClient
from app.manager.intent import Action
from app.manager.nodes import (
    build_context_gate_node,
    build_intake_node,
    build_planner_node,
    build_router_node,
)
from app.rules.store import InMemoryRulesStore, RulesStore
from app.surface.base import EmailSurface
from app.telemetry.records import ErrorCode, StepRecord
from app.workers.loop import build_act_node, build_observe_node, build_reason_node
from app.workers.registry import worker_for

logger = logging.getLogger(__name__)


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
    feedback: FeedbackStore | None = None,
    threshold: float = 0.85,
    max_steps: int = 40,
    checkpointer=None,
):
    """Compile the graph. Concretes come from the composition root.

    Without a `surface` the worker loop is omitted and `dispatch` terminates — which is what
    the PRE-phase tests want, and keeps "can this run without a browser?" an honest yes
    rather than a mock-shaped lie.
    """
    rules = rules or InMemoryRulesStore()
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
            DISPATCH, route_after_dispatch, {OBSERVE: OBSERVE, FINALIZE: FINALIZE}
        )
        graph.add_conditional_edges(
            OBSERVE, route_after_observe, {REASON: REASON, FINALIZE: FINALIZE}
        )
        graph.add_conditional_edges(
            REASON, route_after_reason, {ACT: ACT, REASON: REASON, FINALIZE: FINALIZE}
        )
        graph.add_conditional_edges(
            ACT, route_after_act, {OBSERVE: OBSERVE, FINALIZE: FINALIZE}
        )

    graph.add_edge(FINALIZE, END)

    return graph.compile(checkpointer=checkpointer or InMemorySaver())


__all__ = ["build_manager_graph", "LINEAR"]
