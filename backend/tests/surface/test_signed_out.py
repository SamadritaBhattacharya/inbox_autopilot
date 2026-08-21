"""A sign-in wall is not an inbox.

`detect_view` fell through to `"inbox"` for any URL without a `#`, so Google's
`accounts.google.com/signin/rejected` page was reported as a mailbox. The agent then did
exactly what it was told: it "read the inbox", scrolled, re-read, and finished by summarizing
an inbox it had never seen - six steps and a confident, entirely fictional answer.

A wrong answer delivered fluently is worse than a refusal, so the view is named and the loop
stops on it.
"""
from __future__ import annotations

import pytest
from inbox_contracts import Element

from app.events.emitter import EventEmitter
from app.events.sink import BufferSink
from app.surface.extract import detect_view
from app.telemetry.records import ErrorCode
from app.workers.loop import build_observe_node
from tests.fakes.fake_surface import FakeEmailSurface, observation


@pytest.mark.parametrize(
    "url",
    [
        "https://accounts.google.com/v3/signin/rejected?continue=https://mail.google.com/",
        "https://accounts.google.com/ServiceLogin?service=mail",
        "https://mail.google.com/accounts.google.com/signin",
    ],
)
def test_a_sign_in_page_is_not_reported_as_the_inbox(url):
    assert detect_view(url, compose_open=False) == "signed_out"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://mail.google.com/mail/u/0/#inbox", "inbox"),
        ("https://mail.google.com/mail/u/0/#sent", "sent"),
        ("https://mail.google.com/mail/u/0/#inbox/FMfcgzabcdefghij", "thread"),
    ],
)
def test_real_mailbox_views_are_unaffected(url, expected):
    """The new branch runs first, so it must not swallow ordinary Gmail URLs."""
    assert detect_view(url, compose_open=False) == expected


async def test_the_loop_stops_immediately_when_signed_out():
    """Terminate typed, rather than hand the model a page it cannot act in."""
    surface = FakeEmailSurface(
        [observation(Element(index=1, role="button", name="Try again"), view="signed_out")]
    )
    sink = BufferSink()
    observe = build_observe_node(surface, EventEmitter(sink))

    from app.agent.state import AgentState

    delta = await observe(AgentState(task="summarize my inbox", thread_id="so-1"))

    assert delta["finished"] is True
    assert delta["success"] is False
    assert delta["error_code"] is ErrorCode.NOT_SIGNED_IN
    assert "not signed into Gmail" in delta["reason"]


async def test_a_signed_in_inbox_keeps_going():
    surface = FakeEmailSurface(
        [observation(Element(index=1, role="row", name="A message"), view="inbox")]
    )
    observe = build_observe_node(surface, EventEmitter(BufferSink()))

    from app.agent.state import AgentState

    delta = await observe(AgentState(task="summarize my inbox", thread_id="so-2"))

    assert "finished" not in delta
    assert delta["observation"].elements
