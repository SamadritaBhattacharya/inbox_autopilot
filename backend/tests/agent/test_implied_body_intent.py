"""Don't ask for what the operator already said.

"Write a good afternoon mail with short motivation and send it to P1" states exactly what
the email should say. Replying "what should the email be about?" is the single most
irritating thing this agent can do, and it happened whenever one sampling of the classifier
filled `recipient_identity` and forgot `body_intent` — a coin flip on every run.

The value is the operator's OWN sentence, and the draft still goes to the approval card
before anything sends. Nothing is invented, which is what the "never invent a slot" rule
actually protects against.
"""
from __future__ import annotations

import pytest

from app.manager.intent import Action, TaskIntent
from app.manager.nodes import implied_body_intent
from app.manager.slots import missing_slots


def intent(action: Action = Action.SEND_EMAIL, **slots) -> TaskIntent:
    return TaskIntent(action=action, slots=slots, action_confidence=0.95)


DESCRIBED = [
    "write a good afternoon mail with short motivation and send email to P1",
    "email P1 saying I'll be late tomorrow",
    "send P1 a quick thank you note for the demo",
    "tell P1 the meeting moved to Friday",
]


@pytest.mark.parametrize("task", DESCRIBED)
def test_a_described_body_is_not_a_missing_slot(task):
    assert implied_body_intent(task, intent(recipient_identity="P1")) == task


BARE = ["send an email to P1", "email P1", "write to P1", "compose a message for P1"]


@pytest.mark.parametrize("task", BARE)
def test_a_bare_request_still_earns_the_question(task):
    """Getting this wrong means writing an email out of nothing. The question is correct
    here and must survive."""
    assert implied_body_intent(task, intent(recipient_identity="P1")) is None


def test_a_slot_the_classifier_filled_is_never_overwritten():
    task = "write a motivational note to P1"

    assert implied_body_intent(task, intent(body_intent="short motivation")) is None
    assert implied_body_intent(task, intent(topic="the Friday demo")) is None


@pytest.mark.parametrize("action", [Action.TRIAGE, Action.ARCHIVE, Action.SUMMARIZE])
def test_actions_with_no_body_are_untouched(action):
    """A triage request has no body to infer. Filling one would be the invention the rule
    exists to prevent."""
    assert implied_body_intent("archive all the newsletters now", intent(action)) is None


def test_it_actually_clears_the_gate():
    """The point of the exercise: the gate must stop asking."""
    task = "write a good afternoon mail with short motivation and send email to P1"
    before = intent(recipient_identity="P1")
    assert missing_slots(before) == ["topic"]

    after = before.with_slots(body_intent=implied_body_intent(task, before))

    assert missing_slots(after) == []
