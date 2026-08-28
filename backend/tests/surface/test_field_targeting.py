"""Text lands in the field it was aimed at, even when the page moves underneath.

**The live failure.** The agent was correctly told the subject was at [80]. Between that
observation and the click, committing the recipient closed Gmail's autocomplete dropdown and
the compose dialog reflowed — the element count fell from 80 to 74. The subject had moved;
the body had slid up into the coordinates we were about to click. "Good Evening" went into
the body, the subject stayed empty, and the six confused turns after that all followed from
one silently wrong write.

No settle delay fixes this reliably: the page can reflow at any moment, and a coordinate is
a bet that it will not. A selector names the field itself.

The reflow is simulated explicitly here rather than waited for, because a test that depends
on Gmail's animation timing proves nothing on a slower machine.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

COMPOSE = """
<div role="dialog" style="width:600px;height:500px">
  <div id="dropdown" style="height:120px;background:#eee">autocomplete suggestions</div>
  <div class="recipients"><div class="wrap"><div class="inner">
    <input name="to" aria-label="To recipients" value="priya@corp.com">
  </div></div></div>
  <input name="subjectbox" aria-label="Subject" value="">
  <div g_editable="true" contenteditable="true" aria-label="Message Body"
       style="min-height:120px"></div>
  <button>Send</button>
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


async def _focus(page, field: str) -> bool:
    """Focus a field the way the surface does: each selector IN ORDER, one at a time.

    Comma-joining them would reintroduce the very bug these tests exist to catch — the
    browser would pick by document order and the ordering would mean nothing.
    """
    from app.surface.playwright_surface import _FIELD_SELECTORS

    for selector in _FIELD_SELECTORS[field]:
        try:
            await page.focus(selector, timeout=1000)
            return True
        except Exception:
            continue
    return False


async def _field_filled(page, field: str) -> bool | None:
    """Mirrors `PlaywrightEmailSurface._field_has_content`, including its `None`."""
    from app.surface.playwright_surface import _FIELD_SELECTORS

    seen = False
    for selector in _FIELD_SELECTORS[field]:
        try:
            matches = await page.eval_on_selector_all(
                selector,
                "els => els.map(el => "
                "((el.value !== undefined ? el.value : el.innerText) || '').trim())",
            )
        except Exception:
            continue
        if not matches:
            continue
        seen = True
        if any(matches):
            return True
    return False if seen else None



async def _content(page, selector: str) -> str:
    return await page.eval_on_selector(
        selector, "el => ((el.value !== undefined ? el.value : el.innerText) || '').trim()"
    )


async def test_focus_by_selector_survives_a_reflow(page):
    """THE regression, reproduced. The dropdown collapses after the coordinates are taken;
    typing must still reach the subject."""
    await page.set_content(f"<body>{COMPOSE}</body>")

    # Coordinates as they would have been captured, WITH the dropdown open.
    box = await page.eval_on_selector(
        '[name="subjectbox"]', "el => { const r = el.getBoundingClientRect(); "
        "return {x: r.x + r.width/2, y: r.y + r.height/2}; }"
    )

    # The dropdown closes: everything below it slides up by 120px.
    await page.eval_on_selector("#dropdown", "el => el.remove()")

    # The old way — click the captured point, then type.
    await page.mouse.click(box["x"], box["y"])
    await page.keyboard.type("Good Evening")
    assert await _content(page, '[name="subjectbox"]') == "", (
        "the fixture no longer reproduces the reflow; it must land somewhere else"
    )

    # The new way — focus the field by selector.
    await page.reload()
    await page.set_content(f"<body>{COMPOSE}</body>")
    await page.eval_on_selector("#dropdown", "el => el.remove()")
    await _focus(page, "subject")
    await page.keyboard.type("Good Evening")

    assert await _content(page, '[name="subjectbox"]') == "Good Evening"
    assert await _content(page, '[g_editable="true"]') == "", "the body must be untouched"


async def test_each_selector_reaches_its_own_field(page):
    """A selector that matches the wrong element is worse than a coordinate, because it
    fails identically every time instead of only after a reflow."""
    await page.set_content(f"<body>{COMPOSE}</body>")

    await _focus(page, "subject")
    await page.keyboard.type("SUBJ")
    await _focus(page, "body")
    await page.keyboard.type("BODY")

    assert await _content(page, '[name="subjectbox"]') == "SUBJ"
    assert await _content(page, '[g_editable="true"]') == "BODY"
    assert await _content(page, '[name="to"]') == "priya@corp.com", "To must be untouched"


async def test_the_body_selector_does_not_match_the_recipient_textarea(page):
    """Gmail's To field is a textarea in some versions. A body selector that matched it
    would type the message into the recipient box."""
    html = COMPOSE.replace(
        '<input name="to" aria-label="To recipients" value="priya@corp.com">',
        '<textarea name="to" aria-label="To recipients">priya@corp.com</textarea>',
    )
    await page.set_content(f"<body>{html}</body>")

    await _focus(page, "body")
    await page.keyboard.type("BODY")

    assert await _content(page, '[g_editable="true"]') == "BODY"
    assert await _content(page, 'textarea[name="to"]') == "priya@corp.com"


# ── the verification must not fail a write that succeeded ───────────────────


HIDDEN_TWIN = """
<div role="dialog" style="width:600px;height:500px">
  <!-- Gmail keeps hidden legacy inputs beside its live fields. This one comes FIRST in DOM
       order, so `eval_on_selector` reads it while `page.focus()` types into the visible
       combobox below — different elements, and the check failed a write that worked. -->
  <input name="to" style="display:none" value="">
  <div class="recipients"><div class="wrap"><div class="inner">
    <div role="combobox" aria-label="To recipients" contenteditable="true">{to}</div>
  </div></div></div>
  <input name="subjectbox" aria-label="Subject" value="{subject}">
  <div g_editable="true" role="textbox" aria-label="Message Body"
       contenteditable="true" style="min-height:120px">{body}</div>
</div>
"""


async def _has_content(page, field: str) -> bool:
    return bool(await _field_filled(page, field))


async def test_a_hidden_empty_twin_does_not_fail_a_successful_write(page):
    """THE false alarm. The visible field holds the recipient; a hidden input matching the
    same selector is empty and comes first in DOM order. Checking only the first match
    reported TYPE_DID_NOT_LAND for a write that had plainly worked."""
    await page.set_content(
        f"<body>{HIDDEN_TWIN.format(to='priya@corp.com', subject='', body='')}</body>"
    )

    assert await _has_content(page, "to") is True


async def test_a_genuinely_empty_field_still_reports_empty(page):
    """The counterfactual — this must not become a check that can never fail."""
    await page.set_content(
        f"<body>{HIDDEN_TWIN.format(to='', subject='', body='')}</body>"
    )

    assert await _has_content(page, "to") is False
    assert await _has_content(page, "subject") is False
    assert await _has_content(page, "body") is False


async def test_each_field_is_still_judged_independently(page):
    """Writing the subject must not make the body look written."""
    await page.set_content(
        f"<body>{HIDDEN_TWIN.format(to='', subject='Good Evening', body='')}</body>"
    )

    assert await _has_content(page, "subject") is True
    assert await _has_content(page, "body") is False
    assert await _has_content(page, "to") is False
