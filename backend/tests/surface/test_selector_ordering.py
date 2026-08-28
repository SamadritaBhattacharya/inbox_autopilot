"""Selector order must survive contact with the DOM.

**The bug this pins, in one sentence:** `querySelector('a, b')` returns whichever element
comes first in the DOCUMENT, not whichever selector was listed first — so an ordered list of
selectors that gets comma-joined has had its order silently thrown away.

Two failures came from that single mistake, and both were reported by the user:

  * Gmail puts a "To - Select contacts" LINK above the real recipient field. A loose To
    fallback matched the link, `recipientArea()` walked up from it into a region containing
    the FROM row, the sender's own address was found there, and a brand-new compose window
    reported its recipient as already entered — so the agent skipped it and addressed
    nothing.
  * Gmail lays the subject out above the body. A body selector that also matched the subject
    focused the subject, and the whole message text went into the subject line.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

# The decoy LINK comes first in document order, exactly as Gmail lays it out.
COMPOSE = """
<div role="dialog" style="width:600px;height:500px">
  <div class="header">
    <span data-hovercard-id="me@corp.com" email="me@corp.com">me@corp.com</span>
  </div>
  <div class="recipients">
    <a href="#" aria-label="To - Select contacts" id="decoy">To</a>
    <div class="wrap"><div class="inner">
      <div id="realto" role="combobox" aria-label="To recipients"
           contenteditable="true">{to}</div>
    </div></div>
  </div>
  <input id="realsubject" name="subjectbox" aria-label="Subject" value="{subject}">
  <!-- contenteditable, like Gmail's: without it the div is not focusable at all, and the
       test would be asserting against something you could never type into. -->
  <div id="realbody" g_editable="true" role="textbox" aria-label="Message Body"
       contenteditable="true" style="min-height:120px">{body}</div>
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


async def _focused_id(page, field: str) -> str:
    """Which element the surface would actually type into."""
    from app.surface.playwright_surface import _FIELD_SELECTORS

    for selector in _FIELD_SELECTORS[field]:
        try:
            await page.focus(selector, timeout=1000)
            return await page.evaluate("() => document.activeElement.id")
        except Exception:
            continue
    return ""


async def test_typing_the_body_reaches_the_body_not_the_subject(page):
    """THE reported failure: "adding everything in the fucking subject"."""
    await page.set_content(
        f"<body>{COMPOSE.format(to='', subject='', body='')}</body>"
    )
    assert await _focused_id(page, "body") == "realbody"


async def test_typing_the_subject_reaches_the_subject(page):
    await page.set_content(
        f"<body>{COMPOSE.format(to='', subject='', body='')}</body>"
    )
    assert await _focused_id(page, "subject") == "realsubject"


async def test_typing_the_recipient_reaches_the_field_not_the_decoy_link(page):
    """The "To - Select contacts" link is not somewhere you can type."""
    await page.set_content(
        f"<body>{COMPOSE.format(to='', subject='', body='')}</body>"
    )
    assert await _focused_id(page, "to") == "realto"


async def test_a_fresh_compose_does_not_claim_a_recipient(page):
    """The consequence of matching the decoy: the From row got swept into the recipient
    area, and an empty compose reported To as already filled."""
    from app.observation.funnel.pipeline import ObservationFunnel
    from app.security.tokenizer import PiiTokenizer
    from app.security.vault import SessionPiiVault
    from app.surface.extract import EXTRACT_JS, MAX_NODES, parse_elements, parse_meta

    await page.set_content(
        f"<body>{COMPOSE.format(to='', subject='', body='')}</body>"
    )
    raw = await page.evaluate(EXTRACT_JS, MAX_NODES)
    funnel = ObservationFunnel(PiiTokenizer(SessionPiiVault()))
    observation, _g, _r = funnel.run(parse_elements(raw["elements"]), parse_meta(raw["meta"]))

    assert observation.mail.to_filled is False, "an empty To field claimed a recipient"


async def test_a_real_recipient_still_reports_filled(page):
    """The counterfactual, so the fix cannot be a check that never fires."""
    from app.observation.funnel.pipeline import ObservationFunnel
    from app.security.tokenizer import PiiTokenizer
    from app.security.vault import SessionPiiVault
    from app.surface.extract import EXTRACT_JS, MAX_NODES, parse_elements, parse_meta

    await page.set_content(
        f"<body>{COMPOSE.format(to='priya@corp.com', subject='', body='')}</body>"
    )
    raw = await page.evaluate(EXTRACT_JS, MAX_NODES)
    funnel = ObservationFunnel(PiiTokenizer(SessionPiiVault()))
    observation, _g, _r = funnel.run(parse_elements(raw["elements"]), parse_meta(raw["meta"]))

    assert observation.mail.to_filled is True


async def test_the_to_index_points_at_the_field_not_the_link(page):
    """The agent is told "To: FILLED [49]" and types there. If 49 is the link, the recipient
    goes nowhere."""
    from app.observation.funnel.pipeline import ObservationFunnel
    from app.security.tokenizer import PiiTokenizer
    from app.security.vault import SessionPiiVault
    from app.surface.extract import EXTRACT_JS, MAX_NODES, parse_elements, parse_meta

    await page.set_content(
        f"<body>{COMPOSE.format(to='', subject='', body='')}</body>"
    )
    raw = await page.evaluate(EXTRACT_JS, MAX_NODES)
    funnel = ObservationFunnel(PiiTokenizer(SessionPiiVault()))
    observation, _g, _r = funnel.run(parse_elements(raw["elements"]), parse_meta(raw["meta"]))

    by_index = {e.index: e for e in observation.elements}
    assert observation.mail.to_index is not None
    assert by_index[observation.mail.to_index].role == "combobox"
