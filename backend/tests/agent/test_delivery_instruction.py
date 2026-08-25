"""`task_block` hides the together-vs-separate decision from the worker.

The property this guards: once `context_gate` has resolved `delivery_mode`, the ReAct loop
must never be handed a decision to make about it — only a concrete, unambiguous instruction
to carry out. See `docs/IMPROVEMENT-PLAN.md` §B2, "the ambiguity must never reach the
worker." `slots.py`'s own tests cover the gate; these cover what the worker actually reads.
"""
from __future__ import annotations

from app.agent.state import AgentState
from app.manager.intent import Action, TaskIntent
from app.workers.rendering import task_block


def state_with(intent: TaskIntent) -> AgentState:
    return AgentState(task="t", thread_id="run-1", intent=intent)


def test_a_single_recipient_gets_no_delivery_instruction():
    intent = TaskIntent(
        action=Action.SEND_EMAIL, slots={"recipient_identity": "P1", "topic": "the demo"}
    )
    block = task_block(state_with(intent))
    assert "SEPARATELY" not in block
    assert "TOGETHER" not in block


def test_separate_mode_enumerates_each_recipient_and_forbids_grouping():
    intent = TaskIntent(
        action=Action.SEND_EMAIL,
        slots={
            "recipient_identity": "P1, P2, P3",
            "topic": "the demo",
            "delivery_mode": "separately please",
        },
    )
    block = task_block(state_with(intent))

    assert "3 people SEPARATELY" in block
    assert "1. P1" in block
    assert "2. P2" in block
    assert "3. P3" in block
    assert "never one email with more than one of them in it" in block


def test_together_mode_names_everyone_in_one_field():
    intent = TaskIntent(
        action=Action.SEND_EMAIL,
        slots={
            "recipient_identity": "P1, P2",
            "topic": "the demo",
            "delivery_mode": "one email is fine",
        },
    )
    block = task_block(state_with(intent))

    assert "2 people TOGETHER" in block
    assert "P1, P2" in block


def test_no_answer_yet_defaults_to_together_in_the_instruction():
    """The gate would not have cleared without an answer in practice, but rendering must
    not crash or silently pick 'separate' if it is ever handed an unresolved slot."""
    intent = TaskIntent(
        action=Action.SEND_EMAIL,
        slots={"recipient_identity": "P1, P2", "topic": "the demo"},
    )
    block = task_block(state_with(intent))
    assert "TOGETHER" in block


def test_the_raw_delivery_mode_text_never_reaches_the_worker_twice():
    """The human's own words ("separately please") must not ALSO leak into the generic
    slot dump — the worker gets the resolved instruction, not free text to interpret."""
    intent = TaskIntent(
        action=Action.SEND_EMAIL,
        slots={
            "recipient_identity": "P1, P2",
            "topic": "the demo",
            "delivery_mode": "separately please, thanks",
        },
    )
    block = task_block(state_with(intent))
    assert "separately please, thanks" not in block


def test_actions_with_no_conditional_entry_are_unaffected():
    intent = TaskIntent(action=Action.ARCHIVE, slots={"selector": "the newsletter"})
    block = task_block(state_with(intent))
    assert "SEPARATELY" not in block and "TOGETHER" not in block


def test_forward_gets_the_same_instruction_shape():
    intent = TaskIntent(
        action=Action.FORWARD,
        slots={
            "thread_ref": "t1",
            "recipient_identity": "P1, P2",
            "delivery_mode": "apart",
        },
    )
    block = task_block(state_with(intent))
    assert "2 people SEPARATELY" in block
