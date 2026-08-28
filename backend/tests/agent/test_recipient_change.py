"""Changing the recipient at the approval gate must actually change the recipient.

**The gap.** `Draft` holds subject, body and tone — the recipient lives in `intent.slots`.
So "change the recipient to P5" went to the reviser, which rewrites the WORDS, found nothing
about the words to change, and `_stale_fields` named only subject and body. The instruction
was acknowledged, acted on, and had no effect on who the mail was addressed to.
"""
from __future__ import annotations

import pytest
from inbox_contracts import Element, MailContext

from app.agent.state import AgentState
from app.workers.approval_gate import _recipient_change, _replace_recipient
from tests.fakes.fake_surface import observation


def _state(to_index: int | None = 50) -> AgentState:
    obs = observation(Element(index=50, role="combobox", name="To recipients"), compose_open=True)
    obs = obs.model_copy(
        update={"mail": MailContext(view="compose", composeOpen=True, toIndex=to_index)}
    )
    return AgentState(task="t", thread_id="x", observation=obs)


# ── what counts as a recipient change ───────────────────────────────────────


@pytest.mark.parametrize(
    "instruction,expected",
    [
        ("change the recipient to P5", "P5"),
        ("send it to P5 instead", "P5"),
        ("mail it to P7", "P7"),
        ("change the to field to P2 and P3", "P2, P3"),
        ("the addressee should be P9", "P9"),
    ],
)
def test_a_recipient_change_is_recognised(instruction, expected):
    assert _recipient_change(instruction) == expected


@pytest.mark.parametrize(
    "instruction",
    [
        "mention P5 in the first line",
        "make it shorter",
        "add regards at the end",
        "say hello to P5 in the body",
    ],
)
def test_a_body_edit_that_merely_mentions_a_token_is_not_a_recipient_change(instruction):
    """The dangerous false positive. Changing who an email goes to on the strength of a
    token appearing somewhere in a sentence about the BODY is unrecoverable."""
    assert _recipient_change(instruction) is None


def test_a_recipient_phrase_with_no_token_changes_nothing():
    """"change the recipient" with nobody named is not actionable — better to fall through
    to the ordinary edit path than to invent a recipient."""
    assert _recipient_change("change the recipient") is None


def test_duplicate_tokens_are_collapsed():
    assert _recipient_change("send it to P5, P5") == "P5"


# ── the instruction the worker reads ────────────────────────────────────────


def test_the_instruction_names_the_new_recipient_and_the_field():
    text = _replace_recipient(_state(), "P5")
    assert "P5" in text
    assert "[50]" in text


def test_it_says_to_remove_the_existing_chip():
    """A committed recipient is a chip — a separate node. Typing beside it ADDS a second
    recipient, so the mail would go to both."""
    text = _replace_recipient(_state(), "P5")
    assert "chip" in text.lower()


def test_it_forbids_clearing_the_to_field():
    """Clear empties the input beside the chip and leaves the old recipient attached —
    which is exactly how one changed recipient becomes two."""
    text = _replace_recipient(_state(), "P5")
    assert "Do NOT Clear" in text


def test_the_subject_and_body_are_left_alone():
    text = _replace_recipient(_state(), "P5")
    assert "Leave the subject" in text


def test_a_missing_index_still_gives_a_usable_instruction():
    text = _replace_recipient(_state(to_index=None), "P5")
    assert "P5" in text
    assert "[None]" not in text
