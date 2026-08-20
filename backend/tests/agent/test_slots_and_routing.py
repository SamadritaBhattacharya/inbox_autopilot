"""The slot registry and the routing functions.

Both are pure, so both get exhaustive coverage rather than representative coverage. These
are the two places where "won't start without full context" and "route linear vs decision"
actually live.
"""
from __future__ import annotations

import pytest

from app.agent.routing import (
    ASK,
    DISPATCH,
    FINALIZE,
    PLANNER,
    ROUTER,
    route_after_gate,
    route_after_intake,
    route_after_router,
)
from app.agent.state import AgentState
from app.manager.intent import Action, Route, TaskIntent
from app.manager.slots import (
    REQUIRED_SLOTS,
    confidence,
    is_ready,
    missing_slots,
    question_for,
)


def state(**overrides) -> AgentState:
    return AgentState(task="t", thread_id="run-1", **overrides)


def intent(action: Action, confidence_: float = 1.0, **slots) -> TaskIntent:
    return TaskIntent(action=action, slots=slots, action_confidence=confidence_)


# ── the slot registry ───────────────────────────────────────────────────────


def test_every_action_has_a_declared_requirement():
    """A missing entry would silently mean 'needs nothing' — which is how an action
    dispatches with no context at all."""
    for action in Action:
        assert action in REQUIRED_SLOTS, f"{action} has no slot schema"


def test_send_email_needs_a_recipient_and_something_to_say():
    assert missing_slots(intent(Action.SEND_EMAIL)) == ["recipient_identity", "topic"]


def test_alternatives_are_satisfied_by_either_member():
    """topic OR body_intent — either is enough to write an email."""
    assert missing_slots(intent(Action.SEND_EMAIL, recipient_identity="P1", topic="demo")) == []
    assert missing_slots(
        intent(Action.SEND_EMAIL, recipient_identity="P1", body_intent="say it moved")
    ) == []


def test_blank_slots_do_not_count_as_filled():
    assert "recipient_identity" in missing_slots(
        intent(Action.SEND_EMAIL, recipient_identity="   ", topic="demo")
    )


def test_apply_rules_needs_nothing_because_the_rules_are_the_input():
    assert missing_slots(intent(Action.APPLY_RULES)) == []
    assert is_ready(intent(Action.APPLY_RULES))


def test_unknown_is_never_ready():
    """The gate asks what was meant rather than guessing at an action."""
    assert confidence(intent(Action.UNKNOWN, 1.0)) == 0.0
    assert not is_ready(intent(Action.UNKNOWN, 1.0))


# ── confidence ──────────────────────────────────────────────────────────────


def test_confidence_multiplies_rather_than_averages():
    """A confidently-classified action with nothing filled is NOT half ready.

    Averaging would let a high classifier score paper over missing information — exactly the
    failure the gate exists to prevent.
    """
    half_filled = intent(Action.SEND_EMAIL, 1.0, recipient_identity="P1")
    assert confidence(half_filled) == 0.5
    assert not is_ready(half_filled)


def test_full_slots_and_a_confident_action_clears_the_gate():
    assert is_ready(intent(Action.SEND_EMAIL, 0.95, recipient_identity="P1", topic="demo"))


def test_a_shaky_classification_blocks_even_with_every_slot_filled():
    """If we are not sure what was asked, having answers to the wrong question is no help."""
    assert not is_ready(intent(Action.SEND_EMAIL, 0.4, recipient_identity="P1", topic="demo"))


# ── the question ────────────────────────────────────────────────────────────


def test_the_question_uses_human_words_not_slot_names():
    """A gate that asks for 'recipient_identity' has leaked its schema into the chat."""
    asked = question_for(intent(Action.SEND_EMAIL))
    assert "recipient_identity" not in asked
    assert "who should this go to" in asked


def test_everything_missing_is_asked_at_once():
    """Three sequential questions is three chances for the human to walk away."""
    asked = question_for(intent(Action.SEND_EMAIL))
    assert "who should this go to" in asked
    assert "what the email should be about" in asked
    assert asked.count("?") == 1


def test_a_single_gap_reads_naturally():
    asked = question_for(intent(Action.SEND_EMAIL, 1.0, recipient_identity="P1"))
    assert ", and " not in asked


# ── routing ─────────────────────────────────────────────────────────────────


def test_intake_always_goes_through_the_gate():
    """There is no path around it. That absence IS the guarantee."""
    assert route_after_intake(state()) == "context_gate"


def test_gate_routes_to_ask_when_waiting():
    assert route_after_gate(state(status="awaiting_human")) == ASK


def test_gate_routes_onward_when_clear():
    assert route_after_gate(state(status="running")) == ROUTER


@pytest.mark.parametrize("status", ["done", "failed"])
def test_gate_routes_to_finalize_when_terminal(status):
    """The ask budget ran out; the run ends typed rather than looping."""
    assert route_after_gate(state(status=status)) == FINALIZE


def test_linear_work_skips_the_planner():
    """There is nothing to deliberate about."""
    linear = state(status="running", route=Route(topology="linear"))
    assert route_after_router(linear) == DISPATCH


def test_decision_work_plans_first():
    decision = state(status="running", route=Route(topology="decision"))
    assert route_after_router(decision) == PLANNER


def test_router_finalizes_when_terminal():
    assert route_after_router(state(status="failed")) == FINALIZE


def test_a_missing_route_does_not_silently_dispatch():
    assert route_after_router(state(status="running", route=None)) == PLANNER
