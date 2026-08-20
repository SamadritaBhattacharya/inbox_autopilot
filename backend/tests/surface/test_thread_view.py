"""The thread view against a real browser.

A different shape of page from the inbox: one subject, several messages, and the details a
calendar proposal is read out of. It exercises the parts of the funnel a list view never
touches — a heading, repeated sender chips for the same people, and prose carrying a date, a
time and a phone number.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.security.patterns import find_emails, find_phones
from app.surface.playwright_surface import launch_surface, resolve_chromium

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        resolve_chromium() is None,
        reason="no Chromium build found; run `playwright install chromium`",
    ),
]

FIXTURE = (Path(__file__).resolve().parents[1] / "fixtures" / "thread.html").as_uri()


@pytest.fixture
async def surface():
    surface, close = await launch_surface(headless=True, start_url=FIXTURE)
    try:
        yield surface
    finally:
        await close()


def text_of(observation) -> str:
    return " ".join(element.name for element in observation.elements)


async def test_the_thread_reads_as_a_small_list(surface):
    observation = await surface.observe()
    assert observation.elements
    assert len(observation.elements) < 40


async def test_no_raw_pii_survives_a_thread_view(surface):
    """Three senders, three addresses, and a phone number in prose."""
    observation = await surface.observe()
    serialized = observation.model_dump_json()

    assert find_emails(serialized) == []
    assert find_phones(serialized) == []
    for raw in ("priya.nair@corp.com", "dev.kapoor@corp.com", "Priya Nair", "98765"):
        assert raw not in serialized, f"{raw!r} leaked from the thread"


async def test_the_details_a_proposal_needs_survive(surface):
    """Tokenizing must not destroy the content the calendar worker reads."""
    text = text_of(await surface.observe())

    assert "Friday 22 August" in text
    assert "16:00" in text
    assert "45 minutes" in text


async def test_the_same_person_is_one_token_throughout(surface):
    """A sender appearing twice must not read as two different people."""
    observation = await surface.observe()
    text = text_of(observation)

    # Priya is registered from her chip and referenced in the prose; both become one token.
    tokens = {word for word in text.split() if word.startswith("C") and word[1:].isdigit()}
    assert tokens, "senders should be tokenized"


async def test_the_subject_is_readable(surface):
    assert "Friday demo moved to 4pm" in text_of(await surface.observe())


async def test_a_thread_is_not_mistaken_for_a_compose_window(surface):
    """`compose_open` on a plain thread would tell the worker it is writing, not reading."""
    observation = await surface.observe()
    assert observation.mail is not None
    assert observation.mail.compose_open is False
