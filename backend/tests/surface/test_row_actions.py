"""Mail row actions land on the row they were aimed at.

**The gap these close.** `_perform` dispatches to `_do_<verb>`, and `Archive`, `MarkRead`,
`Label`, `Snooze` and `DeleteForever` had no handler at all — the same hole that made every
approved `Send` fail with "Send has no handler". They were bindable, gated, and not
performable.

The interaction is awkward for a good reason: a row's action buttons do not exist until the
pointer is over the row, so they are absent from the observation the agent picked an index
from. Coordinates cannot address them. The row's y is known, so the button is found by
tooltip and then matched back to its row — and the band check is what stops "Archive"
archiving the row above.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

INBOX = """
<div style="position:relative">
  <!-- A page-level toolbar carrying the SAME tooltip. Without the row-band check this is
       the nearest match for a row near the top, and the action lands on the wrong thread. -->
  <div data-tooltip="Archive" style="position:absolute;top:0;left:0;width:40px;height:24px">
    toolbar
  </div>
  <!-- Buttons are positioned RELATIVE TO THEIR ROW, as Gmail's are: the row is the
       positioned ancestor, so a child at top:8px sits 8px into that row. -->
  <div id="row1" style="position:absolute;top:200px;left:0;width:600px;height:40px">
    Row one
    <div data-tooltip="Archive"
         style="position:absolute;top:8px;left:500px;width:30px;height:24px">a1</div>
    <div data-tooltip="Delete forever"
         style="position:absolute;top:8px;left:540px;width:30px;height:24px">d1</div>
  </div>
  <div id="row2" style="position:absolute;top:300px;left:0;width:600px;height:40px">
    Row two
    <div data-tooltip="Archive"
         style="position:absolute;top:8px;left:500px;width:30px;height:24px">a2</div>
  </div>
</div>
"""


@pytest.fixture
async def page():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome")
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


async def _find(page, y: float, tooltips: list[str], band: int | None = None):
    from app.surface.playwright_surface import _ROW_ACTION_JS, ROW_BAND_PX

    await page.set_content(f"<body>{INBOX}</body>")
    return await page.evaluate(
        _ROW_ACTION_JS,
        {"y": y, "tooltips": tooltips, "band": ROW_BAND_PX if band is None else band},
    )


async def _label_at(page, spot) -> str:
    return await page.evaluate(
        "p => (document.elementFromPoint(p.x, p.y) || {}).textContent || ''", spot
    )


async def test_the_action_lands_on_the_row_it_was_aimed_at(page):
    spot = await _find(page, 220, ["Archive"])
    assert spot is not None
    assert (await _label_at(page, spot)).strip() == "a1"


async def test_a_different_row_gets_its_own_button(page):
    """The property that matters most: archiving row two must not archive row one."""
    spot = await _find(page, 320, ["Archive"])
    assert (await _label_at(page, spot)).strip() == "a2"


async def test_a_page_toolbar_with_the_same_tooltip_is_not_used(page):
    """Without the band check the page-level Archive button wins for a row near the top,
    and the action silently hits the wrong thread."""
    spot = await _find(page, 600, ["Archive"])
    assert spot is None, "matched a control that belongs to no row"


async def test_a_row_with_no_such_control_reports_nothing(page):
    """Row two has no Delete forever. Returning None is what produces a typed refusal
    rather than a click on whatever happened to be nearest."""
    assert await _find(page, 320, ["Delete forever"]) is None


async def test_delete_forever_is_matched_narrowly(page):
    """Gmail's plain "Delete" moves to Trash and is reversible; "Delete forever" only exists
    inside Trash. Matching the former would report a permanent deletion that never happened
    — on the one verb where the human approved something irreversible."""
    spot = await _find(page, 220, ["Delete forever"])
    assert (await _label_at(page, spot)).strip() == "d1"


async def test_zero_sized_controls_are_ignored(page):
    """A button that is present but not rendered cannot be clicked; treating it as a match
    would produce a click into nothing and a success that did not happen."""
    html = INBOX.replace(
        'style="position:absolute;top:8px;left:500px;width:30px;height:24px">a1',
        'style="position:absolute;top:8px;left:500px;width:0;height:0">a1',
    )
    from app.surface.playwright_surface import _ROW_ACTION_JS, ROW_BAND_PX

    await page.set_content(f"<body>{html}</body>")
    spot = await page.evaluate(
        _ROW_ACTION_JS, {"y": 220, "tooltips": ["Archive"], "band": ROW_BAND_PX}
    )
    assert spot is None


async def test_aria_label_is_matched_as_well_as_data_tooltip(page):
    """Gmail uses both, and which one appears varies by control and by version."""
    html = INBOX.replace(
        'data-tooltip="Archive" style="position:absolute;top:8px;left:500px',
        'aria-label="Archive" style="position:absolute;top:8px;left:500px',
    )
    from app.surface.playwright_surface import _ROW_ACTION_JS, ROW_BAND_PX

    await page.set_content(f"<body>{html}</body>")
    spot = await page.evaluate(
        _ROW_ACTION_JS, {"y": 220, "tooltips": ["Archive"], "band": ROW_BAND_PX}
    )
    assert spot is not None
    assert (await _label_at(page, spot)).strip() == "a1"
