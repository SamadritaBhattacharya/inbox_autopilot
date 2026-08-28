"""A recipient can be entered once. Entering them twice is made impossible, not discouraged.

**Why this is not a prompt problem.** Indices are rebuilt every observation by design, so
the index the agent typed a recipient into last turn points at something else this turn.
Reading the new list, the agent sees a button where it believes it put an address, concludes
its own action failed, and types it again — leaving a committed chip *and* loose text in the
To field. Observed live, twice.

The agent cannot settle this for itself: it is never shown field CONTENTS, so "is my
recipient already in there?" is unanswerable from its side of the wire. Only the executor
can answer it, so the check lives there — the same reasoning, and the same shape, as
`COMPOSE_ALREADY_OPEN`.

Driven against real Chrome over a Gmail-shaped DOM, because the guard reads the live page.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

DIALOG = """
<div role="dialog" style="width:500px;height:400px">
  <div class="header">
    <!-- The FROM row: the signed-in user's own address, in the same attributes a
         recipient chip uses. Must never count as "already a recipient", or emailing
         yourself becomes impossible. -->
    <span data-hovercard-id="me@corp.com" email="me@corp.com">me@corp.com</span>
  </div>
  <div class="recipients"><div class="wrap"><div class="inner">
    {chips}
    <input name="to" aria-label="To recipients" value="{typed}">
  </div></div></div>
  <input name="subjectbox" value="">
  <div g_editable="true" contenteditable="true"></div>
</div>
"""


def chip(address: str) -> str:
    return f'<div data-hovercard-id="{address}" email="{address}" class="afV">{address}</div>'


@pytest.fixture
async def page():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome")
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


async def _present(page, html: str) -> set[str]:
    """What the guard sees as already-addressed."""
    from app.surface.playwright_surface import _RECIPIENTS_JS

    await page.set_content(f"<body>{html}</body>")
    state = await page.evaluate(_RECIPIENTS_JS)
    found = {a for a in state["present"] if a}
    if state["typed"]:
        found.add(state["typed"])
    return found


async def test_a_committed_chip_is_seen_as_already_present(page):
    found = await _present(page, DIALOG.format(chips=chip("priya@corp.com"), typed=""))
    assert "priya@corp.com" in found


async def test_loose_typed_text_counts_too(page):
    """The exact state in the screenshot: typed but not yet committed to a chip. Typing it
    again is what produced a chip AND loose text side by side."""
    found = await _present(page, DIALOG.format(chips="", typed="priya@corp.com"))
    assert "priya@corp.com" in found


async def test_the_senders_OWN_address_is_not_a_recipient(page):
    """The From row must not block emailing yourself — the same scoping bug that made a
    fresh compose report its recipient as already entered."""
    found = await _present(page, DIALOG.format(chips="", typed=""))
    assert found == set(), f"the From row leaked in: {found}"


async def test_an_empty_compose_has_nobody(page):
    assert await _present(page, DIALOG.format(chips="", typed="")) == set()


async def test_a_different_person_is_not_already_present(page):
    """Adding someone new must still work — that is the case the guard must not break."""
    found = await _present(page, DIALOG.format(chips=chip("priya@corp.com"), typed=""))
    assert "alex@corp.com" not in found


async def test_several_chips_are_all_seen(page):
    """The multi-recipient case: both must be recognised, so neither is re-added."""
    two = chip("priya@corp.com") + chip("alex@corp.com")
    found = await _present(page, DIALOG.format(chips=two, typed=""))
    assert {"priya@corp.com", "alex@corp.com"} <= found


async def test_no_dialog_at_all_reports_nobody(page):
    """Failing open: a duplicate check that cannot read the page must never block a
    recipient the user actually asked for."""
    assert await _present(page, "<div>no compose here</div>") == set()


async def test_case_is_ignored(page):
    """Gmail echoes addresses back in whatever case the user typed; a case difference must
    not read as a different person."""
    found = await _present(page, DIALOG.format(chips=chip("Priya@Corp.com"), typed=""))
    assert "priya@corp.com" in found
