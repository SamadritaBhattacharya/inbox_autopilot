"""What MOVED, not just what is there.

This is how the agent tells a finished task from an unfinished one.

**The live failure.** Told "close the compose box", the agent clicked Save & close. It
worked: the next observation went from `77 elements · compose` to `170 elements · inbox`.
It then spent every remaining turn hunting for the window it had just closed, and finally
asked Extract twice where the compose box was.

The funnel rebuilds from scratch every turn, by design, and that design has one blind spot:
**a thing that APPEARS is salient — it is simply in the new list — while a thing that
DISAPPEARS is nothing at all.** Noticing an absence means comparing two snapshots, and only
this side holds both. So "did my action work?" was unanswerable from the agent's side for
the entire class of actions whose success is something going away.

`Observation.changed` has been in the contract since the first milestone and nothing ever
populated it. This is what it is for.
"""
from __future__ import annotations

import pytest
from inbox_contracts import MailContext, Observation, Viewport

from app.surface.playwright_surface import _state_changes


def seen(
    view: str = "inbox",
    *,
    compose: bool = False,
    to: bool = False,
    subject: bool = False,
    body: bool = False,
) -> Observation:
    return Observation(
        context_id="T",
        title="Mail",
        viewport=Viewport(width=1280, height=800),
        elements=[],
        mail=MailContext(
            view=view,
            composeOpen=compose,
            toFilled=to,
            subjectFilled=subject,
            bodyFilled=body,
        ),
    )


# ── the transition the whole thing exists for ──────────────────────────────


def test_a_compose_window_closing_is_stated_outright():
    """THE regression. The only evidence the task succeeded was an absence."""
    assert "the compose window closed" in _state_changes(
        seen("compose", compose=True), seen("inbox")
    )


def test_a_compose_window_opening_is_stated_too():
    """Cheap, and it confirms the other half of the pair — an agent that is told when a
    window opens can trust the silence when one does not."""
    assert "a compose window opened" in _state_changes(seen("inbox"), seen("compose", compose=True))


def test_a_view_change_is_named_with_both_ends():
    """"the view changed" would leave the agent to work out from where."""
    assert "the view changed from compose to inbox" in _state_changes(
        seen("compose", compose=True), seen("inbox")
    )


def test_closing_a_compose_reports_both_facts():
    changes = _state_changes(seen("compose", compose=True), seen("inbox"))

    assert "the compose window closed" in changes
    assert "the view changed" in changes


# ── silence when nothing moved ─────────────────────────────────────────────


def test_an_unchanged_page_says_nothing():
    """A `changed:` line on every turn is noise, and noise is what gets skimmed past."""
    assert _state_changes(seen("inbox"), seen("inbox")) == ""


def test_the_first_observation_of_a_run_has_nothing_to_compare_against():
    assert _state_changes(None, seen("inbox")) == ""


def test_a_missing_mail_context_is_not_a_change():
    """Some pages are not mail at all. Inventing a transition there would be a lie."""
    bare = seen().model_copy(update={"mail": None})

    assert _state_changes(bare, seen("inbox")) == ""
    assert _state_changes(seen("inbox"), bare) == ""


# ── field transitions, for a window that stayed open ───────────────────────


def test_a_field_becoming_filled_is_reported():
    """"Did my Type land?" is the same question in a different place."""
    changes = _state_changes(
        seen("compose", compose=True), seen("compose", compose=True, subject=True)
    )

    assert "Subject is now filled" in changes


def test_each_field_is_named_separately():
    changes = _state_changes(
        seen("compose", compose=True),
        seen("compose", compose=True, to=True, subject=True, body=True),
    )

    assert "To is now filled" in changes
    assert "Subject is now filled" in changes
    assert "Body is now filled" in changes


def test_a_field_being_cleared_is_reported():
    changes = _state_changes(
        seen("compose", compose=True, body=True), seen("compose", compose=True)
    )

    assert "Body is now empty" in changes


def test_fields_are_NOT_reported_when_the_window_itself_closed():
    """Every field "empties" when the window goes away. Listing three field changes beside
    "the compose window closed" buries the fact that actually matters under its own
    consequences."""
    changes = _state_changes(
        seen("compose", compose=True, to=True, subject=True, body=True), seen("inbox")
    )

    assert "the compose window closed" in changes
    assert "is now empty" not in changes


# ── it reaches the model ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (seen("compose", compose=True), seen("inbox"), "the compose window closed"),
        (seen("inbox"), seen("compose", compose=True), "a compose window opened"),
    ],
)
def test_the_change_line_is_rendered_into_the_prompt(before, after, expected):
    """A narration the model never reads is not a narration."""
    from app.agent.state import AgentState
    from app.workers.rendering import observation_block

    narrated = after.model_copy(update={"changed": _state_changes(before, after)})
    block = observation_block(AgentState(task="t", thread_id="x", observation=narrated))

    assert f"changed: {expected}" in block or expected in block
