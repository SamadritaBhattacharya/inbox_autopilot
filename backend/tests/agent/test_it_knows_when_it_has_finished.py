"""Three ways a finished task failed to end the run.

Asked to open a sent mail, the agent clicked the right row and the thread opened — 162
elements and "431 more below" collapsed to 71 and "2 more below". Then it clicked twice
more, ran Extract, concluded *"we can consider task done... so we can Complete success"* —
and emitted no tool call. The run died `NO_ACTION` with the task already done, and the extra
clicks had collapsed the message it had just opened.

Three independent failures, each sufficient on its own:

1. **Nothing said the thread had opened.** `mail.view` stays `sent` — a thread inside Sent
   is Sent — and compose was never involved, so every rule in `_state_changes` returned
   nothing. `thread_token` had carried exactly this signal since the first milestone and was
   read by nobody.
2. **`Click` reported `clicked [36]` for a click that hit a heading and did nothing** — the
   same words as one that worked. With positive confirmation for a no-op, there was no
   reason to stop.
3. **The nudge did not name the verb.** "You did not call a tool" answers nothing when the
   model believes it decided and said so.
"""
from __future__ import annotations

import pytest
from inbox_contracts import MailContext, Observation, Viewport

from app.agent.guards import has_concluded, no_tool_call_nudge
from app.surface.playwright_surface import _state_changes


def seen(view: str = "inbox", *, thread: str | None = None, compose: bool = False) -> Observation:
    return Observation(
        context_id="T",
        title="Mail",
        viewport=Viewport(width=1280, height=800),
        elements=[],
        mail=MailContext(view=view, composeOpen=compose, threadToken=thread),
    )


# ── 1. opening a thread is narrated ────────────────────────────────────────


def test_opening_a_thread_is_stated_outright():
    """THE regression. The click worked and nothing said so."""
    changes = _state_changes(seen("sent"), seen("sent", thread="C7"))

    assert "a thread is now open" in changes


def test_it_says_so_even_though_the_VIEW_did_not_change():
    """The exact reason every existing rule stayed silent: a thread inside Sent is still
    Sent, so `mail.view` never moved."""
    changes = _state_changes(seen("sent"), seen("sent", thread="C7"))

    assert changes
    assert "view changed" not in changes


def test_going_back_to_the_list_is_narrated_too():
    """The other half of the pair. Without it, closing a thread is another invisible
    absence — and that is the class of bug this whole mechanism exists for."""
    changes = _state_changes(seen("sent", thread="C7"), seen("sent"))

    assert "the thread closed" in changes
    assert "back in the message list" in changes


def test_the_message_says_which_state_you_are_IN():
    """"a thread opened" leaves the agent to work out what that means for its next move.
    Naming the state is what stops it clicking at a list that is no longer there."""
    changes = _state_changes(seen("sent"), seen("sent", thread="C7"))

    assert "one message, not the list" in changes


def test_staying_in_the_same_thread_says_nothing():
    """A `changed:` line every turn is noise, and noise is what gets skimmed past."""
    assert _state_changes(seen("sent", thread="C7"), seen("sent", thread="C7")) == ""


def test_moving_between_two_threads_is_not_a_close():
    """Token to DIFFERENT token is still "a thread is open". Reporting a close here would
    tell the agent it had lost something it still had."""
    changes = _state_changes(seen("inbox", thread="C7"), seen("inbox", thread="C9"))

    assert "closed" not in changes


def test_opening_a_thread_and_changing_folder_reports_both():
    changes = _state_changes(seen("inbox"), seen("sent", thread="C7"))

    assert "a thread is now open" in changes
    assert "the view changed from inbox to sent" in changes


def test_a_thread_that_was_never_open_reports_nothing():
    assert _state_changes(seen("inbox"), seen("inbox")) == ""


def test_the_first_observation_has_nothing_to_compare_against():
    assert _state_changes(None, seen("sent", thread="C7")) == ""


def test_a_compose_transition_still_works():
    """The rules this was added beside must be untouched."""
    changes = _state_changes(seen("compose", compose=True), seen("inbox"))

    assert "the compose window closed" in changes


# ── 3. the nudge names the verb ────────────────────────────────────────────


@pytest.mark.parametrize(
    "reasoning",
    [
        "It seems we are already there. So we can consider task done.",
        "So we can Complete success.",
        "The task is complete; nothing more to do.",
        "The email is already open, so the task appears done.",
        "No further action is required here.",
        "we should now call complete",
        "I will call Complete(success=true)",
        # Observed verbatim, and it matched nothing — so that turn got the generic nudge
        # rather than the one naming the verb.
        "Need to respond with Complete. Provide success.",
    ],
)
def test_a_model_that_says_it_is_finished_is_told_to_call_Complete(reasoning):
    nudge = no_tool_call_nudge(reasoning)

    assert "Complete(success=true" in nudge
    assert "saying it does not end the run" in nudge


@pytest.mark.parametrize(
    "reasoning",
    [
        "We need to click the compose button next.",
        "The subject field is empty, so I will fill it.",
        "Let me scroll down to find the Sent label.",
        "",
    ],
)
def test_a_model_that_is_still_working_gets_the_ordinary_nudge(reasoning):
    """The guard must stay narrow. Telling an agent mid-task that it said it was finished
    would end runs that had barely started."""
    nudge = no_tool_call_nudge(reasoning)

    assert "You did not call a tool" in nudge
    assert "saying it does not end the run" not in nudge


def test_both_nudges_name_a_verb_to_call():
    """Neither branch may leave the model with advice it can satisfy by talking — which is
    the failure all three of these bugs share."""
    for reasoning in ("task is done", "I will click next"):
        assert "Complete(" in no_tool_call_nudge(reasoning)


def test_the_matcher_needs_a_judgement_about_the_WORK():
    """A false positive tells the model to Complete a task it has not done, so the phrases
    are anchored to a conclusion rather than to the word "complete" appearing anywhere."""
    assert has_concluded("the task is done") is True
    assert has_concluded("nothing more to do") is True
    assert has_concluded("I will complete the subject field next") is False
    assert has_concluded("this is a complete list of the folders") is False
