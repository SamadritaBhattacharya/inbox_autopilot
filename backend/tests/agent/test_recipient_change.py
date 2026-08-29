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


def test_it_says_to_CLEAR_the_field_first():
    """**This assertion is the reverse of what it used to be, and deliberately so.**

    It used to require "Do NOT Clear the To field", because Clear was `Ctrl+A, Delete` in
    the input — which empties the loose text and leaves the committed chip attached, so the
    next address is added alongside and the mail goes to both. The instruction worked around
    a missing capability by sending the agent to click the × on the chip instead.

    That × is not in the observation: a chip has no accessible name and does not survive
    the funnel. Told to click something invisible, the agent scrolled six times, ran Extract
    twice, and asked the human for an index number. `_clear_recipients` now removes the
    chips itself, so the instruction can name a verb that exists.
    """
    text = _replace_recipient(_state(), "P5")

    assert "Clear the To field" in text
    assert "Do NOT Clear" not in text, "the workaround outlived the thing it worked around"


def test_it_never_sends_the_agent_hunting_for_a_chip():
    """The × is not in the element list, and no amount of looking will put it there.

    Naming chips as something Clear takes care of is fine — that is reassurance. Naming
    them as something to FIND and CLICK is the bug: it points at an element that did not
    survive the funnel, and the only moves left after that are scrolling and asking.
    """
    text = _replace_recipient(_state(), "P5")

    assert "×" not in text
    assert "on its chip" not in text
    assert "in the list below" not in text


def test_it_still_forbids_typing_on_top_of_the_old_recipient():
    """The hazard the old wording protected against is real and must survive the fix."""
    text = _replace_recipient(_state(), "P5")

    assert "goes to both" in text


def test_the_subject_and_body_are_left_alone():
    text = _replace_recipient(_state(), "P5")
    assert "Leave the subject" in text


def test_a_missing_index_still_gives_a_usable_instruction():
    text = _replace_recipient(_state(to_index=None), "P5")
    assert "P5" in text
    assert "[None]" not in text
