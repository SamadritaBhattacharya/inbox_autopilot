"""`toFilled` must mean "a recipient is entered", not "this dialog contains an address".

**The live failure this pins.** A brand-new compose window reported `To: FILLED` before
anything had been typed, because the chip search ran against the whole dialog and Gmail's
FROM row — the signed-in user's own address — carries the same markup as a recipient chip.
The agent then did exactly what the prompt tells it to ("fill only the empty ones"), skipped
the recipient, wrote subject and body, and proposed sending an email addressed to nobody.

Asserted against the extractor's real JavaScript, run in a real browser over a synthetic
DOM that reproduces Gmail's structure. A Python reimplementation of the selector logic would
prove only that two copies of my own reasoning agree with each other.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

COMPOSE_DIALOG = """
<div role="dialog" style="width:500px;height:400px">
  <div class="header">
    <!-- Gmail's FROM row. The user's OWN address, marked up exactly like a recipient
         chip. This is what the unscoped selector was matching. -->
    <span data-hovercard-id="me@corp.com" email="me@corp.com">me@corp.com</span>
  </div>
  <div class="recipients">
    <div class="wrap"><div class="inner">
      {chips}
      <input name="to" aria-label="To recipients" value="{to_value}">
    </div></div>
  </div>
  <input name="subjectbox" value="">
  <div g_editable="true" contenteditable="true"></div>
</div>
"""

CHIP = '<div data-hovercard-id="priya@corp.com" email="priya@corp.com" class="afV">Priya</div>'


def _page_html(chips: str = "", to_value: str = "") -> str:
    return COMPOSE_DIALOG.format(chips=chips, to_value=to_value)


async def _meta(page, html: str) -> dict:
    from app.surface.extract import EXTRACT_JS

    await page.set_content(f"<body>{html}</body>")
    return (await page.evaluate(EXTRACT_JS))["meta"]


async def _to_filled(page, html: str) -> bool:
    return bool((await _meta(page, html))["toFilled"])


@pytest.fixture
async def page():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        # The installed Chrome, not Playwright's bundled chromium — the same browser the
        # product actually drives, and the one guaranteed present on a machine that can run
        # this app at all. `playwright install` is a separate, easily-skipped setup step.
        browser = await p.chromium.launch(channel="chrome")
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


async def test_an_empty_compose_reports_the_recipient_as_EMPTY(page):
    """THE regression. The From row must not be mistaken for a recipient."""
    assert await _to_filled(page, _page_html()) is False


async def test_a_committed_chip_reports_FILLED(page):
    """The case the chip search was added for, which must keep working."""
    assert await _to_filled(page, _page_html(chips=CHIP)) is True


async def test_text_typed_but_not_yet_committed_reports_FILLED(page):
    """Typed-but-uncommitted still counts: the agent must not type it a second time."""
    assert await _to_filled(page, _page_html(to_value="priya@corp.com")) is True


async def test_no_to_input_at_all_reports_EMPTY_not_filled(page):
    """When the field cannot be found, guess the RECOVERABLE way.

    "Empty" when it is full costs a duplicate the agent can see and fix next turn. "Full"
    when it is empty costs a silently unaddressed email and nothing downstream notices.
    """
    html = '<div role="dialog" style="width:500px;height:400px">' + CHIP + "</div>"
    assert await _to_filled(page, html) is False


# ── one field's content must never answer for another's ─────────────────────


TEXTAREA_DIALOG = """
<div role="dialog" style="width:500px;height:400px">
  <div class="recipients"><div class="wrap"><div class="inner">
    <!-- Gmail's recipient field IS a textarea in some versions. A bare `textarea`
         selector for the BODY matches it. -->
    <textarea name="to">{to}</textarea>
  </div></div></div>
  <input name="subjectbox" value="{subject}">
  <div g_editable="true" contenteditable="true">{body}</div>
</div>
"""


async def test_a_recipient_in_a_textarea_does_not_make_the_BODY_look_written(page):
    """The second unscoped-selector bug, same family as the From-row one.

    `bodyFilled` used a bare `textarea` selector against the whole dialog. With Gmail's
    recipient field being a textarea, typing the recipient made the body report FILLED —
    and the agent, told to fill only the empty ones, would skip writing the body entirely
    and propose sending an empty email.
    """
    meta = await _meta(page, TEXTAREA_DIALOG.format(to="priya@corp.com", subject="", body=""))

    assert meta["toFilled"] is True
    assert meta["bodyFilled"] is False, "the recipient is not the body"
    assert meta["subjectFilled"] is False


async def test_a_real_body_still_reports_filled_in_the_same_layout(page):
    """The counterfactual, so the exclusion above cannot silently blind the body check."""
    meta = await _meta(page, TEXTAREA_DIALOG.format(to="", subject="", body="Good evening."))

    assert meta["bodyFilled"] is True
    assert meta["toFilled"] is False


async def test_an_email_address_written_in_the_BODY_is_not_a_recipient(page):
    """The case raised directly: prose that happens to contain an address.

    A body reading "you can reach me at me@corp.com" must not make the To field look
    filled. Nothing about writing an address makes it a recipient — only the recipients
    row does.
    """
    html = _page_html().replace(
        '<div g_editable="true" contenteditable="true"></div>',
        '<div g_editable="true" contenteditable="true">'
        "Reach me at me@corp.com any time.</div>",
    )
    meta = await _meta(page, html)

    assert meta["toFilled"] is False
    assert meta["bodyFilled"] is True


async def test_an_email_address_in_the_SUBJECT_is_not_a_recipient(page):
    html = _page_html().replace(
        '<input name="subjectbox" value="">',
        '<input name="subjectbox" value="Invoice for alex@corp.com">',
    )
    meta = await _meta(page, html)

    assert meta["toFilled"] is False
    assert meta["subjectFilled"] is True


async def test_a_to_field_directly_under_the_dialog_is_still_read(page):
    """`querySelectorAll` never returns its own root, so when the walk-up stops at the
    input itself its value has to be read directly or a filled field reads as empty."""
    html = (
        '<div role="dialog" style="width:500px;height:400px">'
        '<input name="to" value="priya@corp.com">'
        '<input name="subjectbox" value="">'
        '<div g_editable="true" contenteditable="true"></div>'
        "</div>"
    )
    assert await _to_filled(page, html) is True


async def test_several_committed_chips_still_report_filled(page):
    """The multi-recipient case: two chips is still "the To field has people in it"."""
    two = CHIP + CHIP.replace("priya@corp.com", "alex@corp.com").replace("Priya", "Alex")
    assert await _to_filled(page, _page_html(chips=two)) is True
