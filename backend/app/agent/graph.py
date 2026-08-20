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
    ASK,
    DISPATCH,
    FINALIZE,
    LINEAR,
    PLANNER,
    ROUTER,
    route_after_gate,
    route_after_router,
)
from app.agent.state import AgentState
from app.llm.base import LLMClient
from app.manager.nodes import (
    build_context_gate_node,
    build_intake_node,
    build_planner_node,
    build_router_node,
)
from app.rules.store import InMemoryRulesStore, RulesStore
from app.telemetry.records import ErrorCode, StepRecord

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


async def _dispatch_placeholder(state: AgentState) -> dict:
    """Where workers attach (M3+).

    Explicit and typed rather than absent: an unimplemented path that silently succeeds is
    how a demo passes and a product does not.
    """
    return {
        "active_worker": (state.route.topology if state.route else None),
        "finished": True,
        "reason": "worker dispatch lands in M3",
    }


def build_manager_graph(
    *,
    llm: LLMClient,
    rules: RulesStore | None = None,
    threshold: float = 0.85,
    checkpointer=None,
):
    """Compile the PRE-phase graph. Concretes come from the composition root."""
    rules = rules or InMemoryRulesStore()

    graph = StateGraph(AgentState)
    graph.add_node("intake", build_intake_node(llm))
    graph.add_node("context_gate", build_context_gate_node(threshold=threshold))
    graph.add_node(ASK, build_ask_node())
    graph.add_node(ROUTER, build_router_node(llm, rules))
    graph.add_node(PLANNER, build_planner_node(llm))
    graph.add_node(DISPATCH, _dispatch_placeholder)
    graph.add_node(FINALIZE, build_finalize_node())

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
    graph.add_edge(DISPATCH, FINALIZE)
    graph.add_edge(FINALIZE, END)

    return graph.compile(checkpointer=checkpointer or InMemorySaver())


__all__ = ["build_manager_graph", "LINEAR"]
