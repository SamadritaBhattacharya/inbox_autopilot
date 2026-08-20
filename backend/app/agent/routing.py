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
APPROVAL = "approval_gate"
OBSERVE = "observe"
REASON = "reason"
ACT = "act"
VERIFY = "verify"
DIAGNOSE = "diagnose"
OPTIONS = "options"
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
    """Into the deterministic worker, or the reasoning loop.

    This is where "zero LLM calls" stops being a claim about routing and becomes one about
    execution. Sending linear work into the observe→reason→act loop would spend a model call
    per item to do something a regex already decided.
    """
    if state.finished or state.is_terminal:
        return FINALIZE
    if state.route is not None and state.route.topology == LINEAR:
        return LINEAR
    return OBSERVE


def route_after_observe(state: AgentState) -> str:
    """Think about what we see — or, if perception itself failed, get it diagnosed."""
    return VERIFY if state.is_terminal else REASON


def route_after_reason(state: AgentState) -> str:
    """Approve, act, or finish.

    A turn with no `last_action` is the nudge path — the model produced prose instead of a
    tool call and gets one more chance. Sending it to `act` would dispatch whatever action
    the PREVIOUS turn chose, which is how an agent silently repeats itself.

    An irreversible verb goes to the gate FIRST. There is no edge from here to `act` for a
    gated verb, so "nothing sends without a human" is a property of the graph rather than a
    rule someone has to remember.

    A terminal turn goes to `verify`, NOT straight to `finalize`. Most failures are
    detected right here — STUCK, MAX_STEPS, REASONING_MISSING, NO_ACTION all end the run
    from inside `reason` — so a shortcut to `finalize` would route the common cases around
    the entire recovery layer and leave self-heal reachable only from the rarer path where
    an action had already been dispatched.
    """
    from app.workers.approval import is_gated  # local: keeps routing free of worker imports

    if state.is_terminal or state.finished:
        return VERIFY
    if state.last_action is None:
        return REASON
    return APPROVAL if is_gated(state.last_action) else ACT


def route_after_approval(state: AgentState) -> str:
    """Dispatch only what a human approved.

    An edit or a rejection clears `last_action`, so there is nothing to dispatch and the
    loop goes back to thinking. Approval leaves it in place — and the surface still checks
    the fingerprint, so this routing is the first of two independent locks, not the only one.
    """
    if state.is_terminal or state.finished:
        return FINALIZE
    return ACT if state.last_action is not None else REASON


def route_after_act(state: AgentState) -> str:
    """Verify a finished run; otherwise re-observe from scratch.

    Mid-run, always back to `observe` and never straight to `reason` — acting on a stale
    observation is the most reliable way to click the wrong thing.

    A run that thinks it is done goes to `verify` rather than straight to `finalize`,
    because "the agent believes it succeeded" and "it succeeded" are different claims and
    only one of them is worth reporting.
    """
    if state.finished or state.is_terminal:
        return VERIFY
    return OBSERVE


def route_after_verify(state: AgentState) -> str:
    """Success ends the run; failure earns a diagnosis."""
    return FINALIZE if state.success else DIAGNOSE


def route_after_diagnose(state: AgentState) -> str:
    """Offer remedies, unless this cause has already been remedied enough."""
    return FINALIZE if state.finished else OPTIONS


def route_after_options(state: AgentState) -> str:
    """A chosen remedy re-enters the loop; nothing left to try ends it.

    Back to `observe`, not `reason`: whatever the remedy was, the page has moved on and the
    indices the model remembers are stale.
    """
    return FINALIZE if state.finished or state.is_terminal else OBSERVE
