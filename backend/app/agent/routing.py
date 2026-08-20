"""Edge decisions — pure functions over state.

No I/O, no ports, no awaits. Every branch is reachable by constructing a state directly,
which is why routing is the one part of the graph with exhaustive test coverage rather than
representative coverage.

Keeping these pure is also what makes the graph readable: the topology says what CAN
happen, and these say what DOES. Routing logic smeared inside node bodies would put control
flow in two places and make neither of them the truth.
"""
from __future__ import annotations

from app.agent.state import AgentState

ASK = "ask"
ROUTER = "router"
LINEAR = "linear"
PLANNER = "planner"
DISPATCH = "dispatch"
FINALIZE = "finalize"


def route_after_intake(state: AgentState) -> str:
    """Always through the gate. There is no path around it — that IS the guarantee."""
    return "context_gate"


def route_after_gate(state: AgentState) -> str:
    """Ask, give up, or proceed."""
    if state.is_terminal:
        return FINALIZE
    if state.status == "awaiting_human":
        return ASK
    return ROUTER


def route_after_router(state: AgentState) -> str:
    """Linear work skips the planner: there is nothing to deliberate about."""
    if state.is_terminal:
        return FINALIZE
    if state.route is not None and state.route.topology == LINEAR:
        return DISPATCH
    return PLANNER


def route_after_dispatch(state: AgentState) -> str:
    return FINALIZE if state.finished else DISPATCH
