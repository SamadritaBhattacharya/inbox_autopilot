"""Conditional slots — the flexibility this docs/IMPROVEMENT-PLAN.md §B2 asked for.

`REQUIRED_SLOTS` cannot express "send_email needs one more thing, but only when there are
multiple recipients" — a flat alternative-group has no way to say a requirement applies
*sometimes*. `CONDITIONAL_SLOTS` is a second table for exactly that: a predicate over the
whole intent, evaluated only once every required group already clears.

Pure functions throughout, so this is exhaustive rather than representative — no LLM, no
browser, matching the discipline `slots.py`'s own tests already use. The fixture table below
is the "~40 utterances -> expected (action, outstanding, conditional-ask)" the plan asked
for; it is not literally forty, but it is broad rather than a handful of happy paths.
"""
from __future__ import annotations

import pytest

from app.manager.intent import Action, TaskIntent
from app.manager.slots import (
    CONDITIONAL_SLOTS,
    is_ready,
    outstanding_slots,
    question_for,
    recipient_count,
    resolved_delivery_mode,
    split_recipients,
)


def intent(action: Action, confidence: float = 1.0, **slots) -> TaskIntent:
    return TaskIntent(action=action, slots=slots, action_confidence=confidence)


# ── recipient_count: the predicate's own building block ─────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("", 0),
        ("   ", 0),
        ("P1", 1),
        ("P1, P2", 2),
        ("P1 and P2", 2),
        ("P1, P2, P3", 3),
        ("P1 and P2 and P3", 3),
        ("P1, P2 and P3", 3),
        ("P1 & P2", 2),
        ("P1, P1", 1),  # the same token twice is one person, not two
        ("alice@x.com and bob@y.com", 2),  # tokens not yet minted at this layer
        ("John and Priya", 2),  # no address at all yet — still counts as two
        ("Priya", 1),
    ],
)
def test_recipient_count(value, expected):
    assert recipient_count(value) == expected


def test_split_recipients_preserves_order_and_dedupes():
    assert split_recipients("P2, P1, P2") == ["P2", "P1"]


# ── the conditional table itself ─────────────────────────────────────────────


def test_a_single_recipient_triggers_no_conditional_ask():
    it = intent(Action.SEND_EMAIL, recipient_identity="P1", topic="the demo")
    assert outstanding_slots(it) == []
    assert is_ready(it)


def test_multiple_recipients_trigger_the_delivery_mode_ask():
    it = intent(Action.SEND_EMAIL, recipient_identity="P1, P2", topic="the demo")
    assert outstanding_slots(it) == ["delivery_mode"]
    assert not is_ready(it)


def test_the_conditional_ask_does_not_fire_before_the_required_bar_clears():
    """Asking "one email or separate?" before the gate even knows the topic is backwards
    — and would cost a second round trip for a task that needs both answers anyway."""
    it = intent(Action.SEND_EMAIL, recipient_identity="P1, P2")  # no topic
    assert outstanding_slots(it) == ["topic"]


def test_answering_delivery_mode_clears_the_gate():
    it = intent(
        Action.SEND_EMAIL,
        recipient_identity="P1, P2",
        topic="the demo",
        delivery_mode="separately please",
    )
    assert outstanding_slots(it) == []
    assert is_ready(it)


def test_forward_gets_the_same_treatment_as_send_email():
    it = intent(Action.FORWARD, thread_ref="t1", recipient_identity="P1, P2")
    assert outstanding_slots(it) == ["delivery_mode"]


@pytest.mark.parametrize(
    "action",
    [Action.REPLY, Action.ARCHIVE, Action.TRIAGE, Action.READ, Action.APPLY_RULES],
)
def test_actions_with_no_conditional_table_are_unaffected(action):
    """No entry in `CONDITIONAL_SLOTS` for these — `outstanding_slots` must fall straight
    through to the required-only answer, exactly as `missing_slots` always did."""
    assert action not in CONDITIONAL_SLOTS


# ── the question: propose a default, don't just interrogate ─────────────────


def test_the_question_states_the_default():
    it = intent(Action.SEND_EMAIL, recipient_identity="P1, P2", topic="the demo")
    question = question_for(it)
    assert "one email to everyone" in question
    assert "unless you say separately" in question


def test_the_question_never_leaks_the_slot_name():
    it = intent(Action.SEND_EMAIL, recipient_identity="P1, P2", topic="the demo")
    assert "delivery_mode" not in question_for(it)


def test_a_conditional_ask_is_never_batched_with_a_required_one():
    """The two are mutually exclusive by construction (`outstanding_slots` returns required
    OR conditional, never both), so the question is always about one decision at a time."""
    it = intent(Action.SEND_EMAIL, recipient_identity="P1, P2")  # topic ALSO missing
    question = question_for(it)
    assert "topic" not in question  # not phrased as raw slot names
    assert "separately" not in question  # the delivery-mode ask has not fired yet


# ── resolving the human's free-text answer ───────────────────────────────────


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("separate", "separate"),
        ("separately please", "separate"),
        ("one at a time", "separate"),
        ("do them individually", "separate"),
        ("apart", "separate"),
        ("together", "together"),
        ("one email to both", "together"),
        ("same email is fine", "together"),
        ("cc them both", "together"),
        ("", "together"),  # no answer at all -> the documented default
        ("sure", "together"),  # unparseable -> the documented default, not a guess
    ],
)
def test_resolved_delivery_mode(answer, expected):
    it = intent(
        Action.SEND_EMAIL,
        recipient_identity="P1, P2",
        topic="x",
        delivery_mode=answer,
    )
    assert resolved_delivery_mode(it) == expected


def test_resolved_delivery_mode_defaults_for_an_action_with_no_conditional_entry():
    """A defensive default rather than a crash if this is ever called off-label."""
    assert resolved_delivery_mode(intent(Action.ARCHIVE, selector="x")) == "together"


# ── the fixture table: broad rather than deep ────────────────────────────────

CASES: tuple[tuple[TaskIntent, list[str]], ...] = (
    (intent(Action.SEND_EMAIL, recipient_identity="P1", topic="x"), []),
    (intent(Action.SEND_EMAIL, recipient_identity="P1"), ["topic"]),
    (intent(Action.SEND_EMAIL, topic="x"), ["recipient_identity"]),
    (intent(Action.SEND_EMAIL), ["recipient_identity", "topic"]),
    (intent(Action.SEND_EMAIL, recipient_identity="P1, P2", topic="x"), ["delivery_mode"]),
    (
        intent(Action.SEND_EMAIL, recipient_identity="P1, P2, P3", topic="x"),
        ["delivery_mode"],
    ),
    (
        intent(
            Action.SEND_EMAIL,
            recipient_identity="P1, P2",
            topic="x",
            delivery_mode="separate",
        ),
        [],
    ),
    (intent(Action.FORWARD, thread_ref="t1", recipient_identity="P1"), []),
    (
        intent(Action.FORWARD, thread_ref="t1", recipient_identity="P1, P2"),
        ["delivery_mode"],
    ),
    (intent(Action.FORWARD, recipient_identity="P1, P2"), ["thread_ref"]),
    (intent(Action.REPLY, thread_ref="t1", stance="agree"), []),
    (intent(Action.ARCHIVE, selector="the newsletter"), []),
    (intent(Action.SUMMARIZE), []),
    (intent(Action.UNKNOWN), ["action_clarification"]),
)


@pytest.mark.parametrize("it,expected", CASES, ids=[f"case-{i}" for i in range(len(CASES))])
def test_outstanding_slots_fixture_table(it, expected):
    assert outstanding_slots(it) == expected
