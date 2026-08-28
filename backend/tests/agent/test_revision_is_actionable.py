"""A revision must tell the worker what to DO, not ask it to work out what changed.

**The live failure.** The human edited the draft in the approval card and hit Revise. The
new draft reached the worker correctly — and the email never changed. Three things collided:

  1. The compose window still held the OLD text; revising updates the draft, not the browser.
  2. The observation reported `Body: FILLED`, and the standing rule is "a FILLED field is
     done" — the guard that stops a recipient being typed twice was now the thing blocking
     the human's own edit.
  3. The instruction said "retype ONLY the fields that changed", which requires comparing
     the draft against the compose window. The worker is never shown field CONTENTS by
     design, so that comparison is impossible from its side.

Cornered, it called `AskUser` twice asking the human to type the body out again.

The gate holds both drafts, so it can do the comparison itself and name the answer.
"""
from __future__ import annotations

from inbox_contracts import Element, MailContext

from app.agent.state import AgentState
from app.manager.draft import Draft
from app.workers.approval_gate import _apply_revision, _stale_fields
from tests.fakes.fake_surface import observation


def _state(**indices) -> AgentState:
    obs = observation(Element(index=61, role="textbox", name="Subject"), compose_open=True)
    obs = obs.model_copy(
        update={
            "mail": MailContext(
                view="compose",
                composeOpen=True,
                toFilled=True,
                subjectFilled=True,
                bodyFilled=True,
                **indices,
            )
        }
    )
    return AgentState(task="t", thread_id="x", observation=obs)


BEFORE = Draft(subject="Good Evening", body="Old body.")


# ── which fields actually went stale ────────────────────────────────────────


def test_only_the_body_is_stale_when_only_the_body_changed():
    after = Draft(subject="Good Evening", body="Old body. best wishes")
    assert _stale_fields(BEFORE, after) == ["body"]


def test_only_the_subject_is_stale_when_only_the_subject_changed():
    after = Draft(subject="Evening!", body="Old body.")
    assert _stale_fields(BEFORE, after) == ["subject"]


def test_both_are_stale_when_both_changed():
    after = Draft(subject="Evening!", body="New body.")
    assert _stale_fields(BEFORE, after) == ["subject", "body"]


def test_nothing_is_stale_when_nothing_changed():
    assert _stale_fields(BEFORE, BEFORE) == []


def test_no_previous_draft_means_everything_needs_writing():
    assert _stale_fields(None, BEFORE) == ["subject", "body"]


def test_whitespace_alone_is_not_a_change():
    """Retyping a whole field because a trailing newline moved is churn on the one surface
    where every extra keystroke risks corrupting a draft."""
    after = Draft(subject="  Good Evening  ", body="Old body.\n")
    assert _stale_fields(BEFORE, after) == []


# ── the instruction the worker actually reads ───────────────────────────────


def test_the_instruction_names_the_field_and_its_index():
    """Without the index the worker has to find the field by name in a list that renumbers
    every turn — which is the search this whole change exists to remove."""
    after = Draft(subject="Good Evening", body="Old body. best wishes")
    text = _apply_revision(_state(subjectIndex=61, bodyIndex=70), BEFORE, after)

    assert "Body [70]" in text
    assert "Clear it" in text


def test_the_instruction_does_not_mention_fields_that_did_not_change():
    """Naming the subject too would have it retyped for no reason — and every unnecessary
    retype is a chance to corrupt text the human already approved."""
    after = Draft(subject="Good Evening", body="Old body. best wishes")
    text = _apply_revision(_state(subjectIndex=61, bodyIndex=70), BEFORE, after)

    assert "Subject [61]" not in text


def test_the_instruction_overrides_the_FILLED_rule_explicitly():
    """THE blocker. "Fill only the empty ones" is what stopped the edit being applied, so
    the licence to overwrite has to be stated, not implied."""
    after = Draft(subject="Good Evening", body="New.")
    text = _apply_revision(_state(subjectIndex=61, bodyIndex=70), BEFORE, after)

    assert "FILLED" in text
    assert "stale, not correct" in text


def test_the_recipient_is_explicitly_left_alone():
    """A revision is about the words. Touching the To field risks a duplicate chip."""
    after = Draft(subject="Good Evening", body="New.")
    text = _apply_revision(_state(subjectIndex=61, bodyIndex=70), BEFORE, after)

    assert "Leave the recipient alone" in text


def test_an_unchanged_draft_says_so_rather_than_asking_for_a_rewrite():
    text = _apply_revision(_state(subjectIndex=61, bodyIndex=70), BEFORE, BEFORE)

    assert "Nothing in the draft actually changed" in text
    assert "Clear" not in text


def test_a_missing_index_still_produces_a_usable_instruction():
    """A field scrolled off-screen has no index this turn. Naming it without a number is
    still better than silence — the worker can scroll to it."""
    after = Draft(subject="Good Evening", body="New.")
    text = _apply_revision(_state(), BEFORE, after)

    assert "Body:" in text
    assert "[None]" not in text


def test_the_instruction_never_tells_the_worker_to_diff_anything():
    """The regression in words: the old text asked for a comparison the worker cannot make."""
    after = Draft(subject="Evening!", body="New.")
    text = _apply_revision(_state(subjectIndex=61, bodyIndex=70), BEFORE, after)

    assert "fields that changed" not in text
    assert "which changed" not in text.lower()
