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


def test_the_instruction_asks_for_ONE_verb():
    """**Reversed deliberately: this used to require "Clear it".**

    "Clear it, then Type" is two verbs, and the worker prompt says *call exactly one tool
    per turn*. Handed both, the model spent whole turns reasoning about the conflict — "that
    is two calls in same turn, which violates rule... we need to redo" — lost track of which
    half it had done, and re-cleared a body it had just written correctly. Three times, on
    one edit.

    `Replace` is that intent as a single action, so there is nothing left to interpret.
    """
    after = Draft(subject="Good Evening", body="Old body. best wishes")
    text = _apply_revision(_state(subjectIndex=61, bodyIndex=70), BEFORE, after)

    assert "call Replace" in text
    assert "Clear it, then Type" not in text


def test_the_ending_names_a_verb_rather_than_asking_for_a_proposal():
    """**The bug that killed a run outright.** It used to end "Then propose sending again",
    and the model read "propose" as *say* something: "That is not a tool; it's a textual
    response." It emitted no tool call, the loop got nothing to dispatch, and the run died
    NO_ACTION before the human ever saw the approval card.

    Send IS the proposal — calling it is what opens the gate. Never end an instruction with
    a word the model can satisfy by talking.
    """
    after = Draft(subject="Good Evening", body="Old body. best wishes")
    text = _apply_revision(_state(subjectIndex=61, bodyIndex=70), BEFORE, after)

    assert "call Send again" in text
    assert "propose sending" not in text


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


# ── the text to type travels WITH the instruction ───────────────────────────
#
# **The second live failure, after the first was fixed.** The human added a line in the
# approval box and applied it. The worker cleared the body and retyped it — identically.
# Every mechanical part had worked: the edit parsed, the diff was right, the field was
# named, it was cleared and rewritten. It rewrote the OLD text.
#
# "Type the new body exactly as it appears above" was the whole problem. TWO versions of
# the email were above: the corrected draft rendered at the top of the prompt, and the
# worker's own earlier `Type(text=…)` call carrying the old body, sitting in the history
# immediately before the instruction. It copied the nearer one.
#
# A pointer into a conversation is not a reference when the conversation holds an older
# copy of the same thing.


def test_the_new_body_is_carried_IN_the_instruction():
    after = Draft(subject=BEFORE.subject, body="Good evening.\n\nKeep going.\n\nBest, Sam")
    text = _apply_revision(_state(subjectIndex=61, bodyIndex=70), BEFORE, after)

    assert "Good evening.\n\nKeep going.\n\nBest, Sam" in text


def test_it_never_points_at_something_ELSEWHERE_in_the_conversation():
    after = Draft(subject=BEFORE.subject, body="New body.")
    text = _apply_revision(_state(subjectIndex=61, bodyIndex=70), BEFORE, after)

    assert "as it appears above" not in text
    assert "above" not in text


def test_it_says_the_earlier_version_is_dead():
    """The stale `Type` call is still sitting in the history, nearer than the new draft.
    Naming it as superseded costs a sentence; hoping is what produced the bug."""
    after = Draft(subject=BEFORE.subject, body="New body.")
    text = _apply_revision(_state(subjectIndex=61, bodyIndex=70), BEFORE, after)

    assert "superseded" in text
    assert "earlier Type call" in text


def test_the_markers_are_named_as_not_part_of_the_text():
    """Otherwise "--- begin body ---" gets typed into the email."""
    after = Draft(subject=BEFORE.subject, body="New body.")
    text = _apply_revision(_state(subjectIndex=61, bodyIndex=70), BEFORE, after)

    assert "--- begin body ---" in text
    assert "--- end body ---" in text
    assert "not the markers themselves" in text


def test_a_changed_subject_carries_its_own_text():
    after = Draft(subject="Friday demo — moved", body=BEFORE.body)
    text = _apply_revision(_state(subjectIndex=61, bodyIndex=70), BEFORE, after)

    assert "--- begin subject ---\nFriday demo — moved\n--- end subject ---" in text
    assert "--- begin body ---" not in text, "an unchanged body must not be retyped"


def test_a_multi_line_body_survives_intact():
    """A blank line between paragraphs is content. Losing one silently reformats the email."""
    body = "Hi,\n\nFirst paragraph.\n\nSecond paragraph.\n\nBest,\nSam"
    text = _apply_revision(_state(bodyIndex=70), BEFORE, Draft(subject=BEFORE.subject, body=body))

    assert f"--- begin body ---\n{body}\n--- end body ---" in text


def test_an_unchanged_draft_carries_no_text_at_all():
    text = _apply_revision(_state(subjectIndex=61, bodyIndex=70), BEFORE, BEFORE)

    assert "--- begin" not in text
    assert "superseded" not in text
