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
OBSERVE = "observe"
REASON = "reason"
ACT = "act"
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
    """Into the loop, unless dispatch already resolved the run."""
    return FINALIZE if state.finished or state.is_terminal else OBSERVE


def route_after_observe(state: AgentState) -> str:
    """A surface that died mid-run ends things; otherwise think about what we see."""
    return FINALIZE if state.is_terminal else REASON


def route_after_reason(state: AgentState) -> str:
    """Act on the chosen tool, or finish.

    A turn with no `last_action` is the nudge path — the model produced prose instead of a
    tool call and gets one more chance. Sending it to `act` would dispatch whatever action
    the PREVIOUS turn chose, which is how an agent silently repeats itself.
    """
    if state.is_terminal or state.finished:
        return FINALIZE
    return ACT if state.last_action is not None else REASON


def route_after_act(state: AgentState) -> str:
    """Re-observe from scratch, unless the run is over.

    Always back to `observe`, never straight to `reason`. Acting on a stale observation is
    the single most reliable way to click the wrong thing.
    """
    return FINALIZE if state.finished or state.is_terminal else OBSERVE
