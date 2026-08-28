"""The agent is told WHERE each compose field is, not just whether it is filled.

**The run this exists to prevent.** Indices are rebuilt every observation by design, and an
open autocomplete dropdown alone changes the element count — 73, 80, 81, 74 across four
consecutive turns in one observed run. The agent read "Subject textbox at [60]", acted, saw
[60] had become a button, concluded its own action had failed, and then spent four turns on
`Extract` and `Scroll` hunting for the field. It eventually typed the subject into an index
carried over from an earlier turn, then cleared and retyped a field that had been correct.

Every one of those failures is the same missing fact: the observation said `Subject: empty`
without saying *where*. This walks the real extractor and the real funnel over a live page
and asserts the number comes out — end to end, because the value of this fix is entirely in
the two layers agreeing.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

COMPOSE = """
<div role="dialog" style="width:600px;height:500px">
  <div class="header">
    <span data-hovercard-id="me@corp.com" email="me@corp.com">me@corp.com</span>
  </div>
  <div class="recipients"><div class="wrap"><div class="inner">
    <input name="to" aria-label="To recipients" value="{to}">
  </div></div></div>
  <input name="subjectbox" aria-label="Subject" value="{subject}">
  <div g_editable="true" contenteditable="true" aria-label="Message Body">{body}</div>
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


async def _observe(page, html: str):
    """The real path: extractor JS -> parse -> funnel -> Observation."""
    from app.observation.funnel.pipeline import ObservationFunnel
    from app.security.tokenizer import PiiTokenizer
    from app.security.vault import SessionPiiVault
    from app.surface.extract import EXTRACT_JS, MAX_NODES, parse_elements, parse_meta

    await page.set_content(f"<body>{html}</body>")
    raw = await page.evaluate(EXTRACT_JS, MAX_NODES)
    elements = parse_elements(raw["elements"])
    meta = parse_meta(raw["meta"])
    funnel = ObservationFunnel(PiiTokenizer(SessionPiiVault()))
    observation, _geometry, _report = funnel.run(elements, meta)
    return observation


async def test_every_compose_field_reports_an_index(page):
    observation = await _observe(page, COMPOSE.format(to="", subject="", body=""))
    mail = observation.mail

    assert mail.compose_open is True
    for label, index in (
        ("to", mail.to_index),
        ("subject", mail.subject_index),
        ("body", mail.body_index),
    ):
        assert index is not None, f"{label} has no index — the agent would have to hunt"


async def test_a_reported_index_actually_points_at_that_field(page):
    """A number that names the wrong element is worse than no number: the agent would type
    the subject into whatever happens to sit there."""
    observation = await _observe(page, COMPOSE.format(to="", subject="", body=""))
    by_index = {e.index: e for e in observation.elements}

    subject = by_index[observation.mail.subject_index]
    assert "subject" in (subject.name or "").lower()

    body = by_index[observation.mail.body_index]
    assert "body" in (body.name or "").lower()


async def test_reported_indices_are_always_dispatchable(page):
    """An index the model was never shown is refused at dispatch. Reporting one would aim
    the agent at a number that cannot work and teach it the numbers are unreliable."""
    observation = await _observe(page, COMPOSE.format(to="", subject="", body=""))
    listed = {e.index for e in observation.elements}
    mail = observation.mail

    for index in (mail.to_index, mail.subject_index, mail.body_index):
        assert index in listed


async def test_the_indices_survive_the_field_being_filled(page):
    """The exact turn where the old code fell apart: after typing, everything renumbers.
    The reported index must track the field, not a stale position."""
    observation = await _observe(
        page, COMPOSE.format(to="priya@corp.com", subject="Good Evening", body="Hello.")
    )
    mail = observation.mail
    by_index = {e.index: e for e in observation.elements}

    assert mail.to_filled and mail.subject_filled and mail.body_filled
    assert "subject" in (by_index[mail.subject_index].name or "").lower()


async def test_no_compose_open_reports_no_indices(page):
    """An inbox has no compose fields; claiming one would be a number pointing at a mail row."""
    observation = await _observe(page, '<div><a href="#">Some inbox row</a></div>')

    assert observation.mail.compose_open is False
    assert observation.mail.to_index is None
    assert observation.mail.subject_index is None
    assert observation.mail.body_index is None


# ── Gmail's REAL markup, not a simplified stand-in ──────────────────────────


GMAIL_SHAPED = """
<div role="dialog" style="width:600px;height:500px">
  <div class="header">
    <span data-hovercard-id="me@corp.com" email="me@corp.com">me@corp.com</span>
  </div>
  <div class="recipients">
    <!-- A LABEL, which the old selector matched instead of the field. -->
    <div class="label">To</div>
    <div class="wrap"><div class="inner">
      <!-- The real field: a combobox DIV, not an <input>. `input[aria-label*="To"]`
           required the tag and therefore missed it entirely. -->
      <div role="combobox" aria-label="To recipients" contenteditable="true">{to}</div>
    </div></div>
  </div>
  <input name="subjectbox" placeholder="Subject" value="{subject}">
  <!-- min-height matters: an empty div is zero-height and the visibility filter drops
       it. Real Gmail's writing area always has height. -->
  <div g_editable="true" role="textbox" aria-label="Message Body"
       style="min-height:120px">{body}</div>
  <div role="button" data-tooltip="Send">Send</div>
</div>
"""


async def test_the_to_field_is_found_when_it_is_a_combobox_not_an_input(page):
    """THE regression. Gmail's To field is `div[role="combobox"]`; the old selector required
    an `<input>`, missed it, and reported the To field at the index of a nearby LABEL — so
    typing aimed at a label and the subject was never located at all."""
    observation = await _observe(page, GMAIL_SHAPED.format(to="", subject="", body=""))
    mail = observation.mail
    by_index = {e.index: e for e in observation.elements}

    assert mail.to_index is not None, "the To field was not found at all"
    found = by_index[mail.to_index]
    assert found.role == "combobox", f"pointed at a {found.role}, not the field"
    assert "to" in (found.name or "").lower()


async def test_the_subject_is_found_in_gmail_shaped_markup(page):
    """"Subject: (not on screen)" was what sent the agent scrolling until the stuck guard
    fired. The field was on screen the whole time."""
    observation = await _observe(page, GMAIL_SHAPED.format(to="", subject="", body=""))

    assert observation.mail.subject_index is not None


async def test_the_body_is_found_in_gmail_shaped_markup(page):
    observation = await _observe(page, GMAIL_SHAPED.format(to="", subject="", body=""))
    by_index = {e.index: e for e in observation.elements}

    assert observation.mail.body_index is not None
    assert "body" in (by_index[observation.mail.body_index].name or "").lower()


async def test_a_combobox_recipient_still_reports_filled(page):
    """The chip/filled logic has to work against the same real markup."""
    observation = await _observe(
        page, GMAIL_SHAPED.format(to="priya@corp.com", subject="", body="")
    )

    assert observation.mail.to_filled is True
    assert observation.mail.body_filled is False, "the recipient is not the body"


async def test_the_to_combobox_is_not_mistaken_for_the_body(page):
    """`[contenteditable="true"]` matches Gmail's To combobox too. Without excluding it, a
    typed recipient would make the body report FILLED and the message never get written."""
    observation = await _observe(
        page, GMAIL_SHAPED.format(to="priya@corp.com", subject="", body="")
    )
    by_index = {e.index: e for e in observation.elements}

    body = by_index[observation.mail.body_index]
    assert body.role != "combobox"
