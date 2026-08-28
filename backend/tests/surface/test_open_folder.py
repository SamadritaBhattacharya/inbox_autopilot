"""Navigating to a folder — by NAME, never by URL.

`Navigate(url=...)` exists on this surface and is bound to no worker on purpose: an agent
that reads attacker-controlled email must never be able to load an address that email chose.
Naming a folder keeps the useful half of navigation and gives away none of it — the model
supplies a name, the executor decides where that lives.

Two routes, in order: the sidebar entry a human would click (which also works for a user's
own labels), then an allowlisted Gmail location for when the sidebar is collapsed.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

SIDEBAR = """
<div id="nav">
  <a href="#inbox" aria-label="Inbox 1,234">Inbox</a>
  <a href="#sent">Sent</a>
  <a href="#spam">Spam 4</a>
  <a href="#trash">Trash</a>
  <a href="#label/Work">Work</a>
  <a href="#hidden" style="display:none">Snoozed</a>
</div>
<div id="message">
  <p>Please check your Sent folder for the invoice.</p>
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


async def _find(page, wanted: str):
    from app.surface.playwright_surface import _SIDEBAR_JS

    await page.set_content(f"<body>{SIDEBAR}</body>")
    return await page.evaluate(_SIDEBAR_JS, wanted)


async def _href_at(page, spot) -> str:
    return await page.evaluate(
        "p => { const el = document.elementFromPoint(p.x, p.y); "
        "return (el.closest('a') || {}).getAttribute?.('href') || ''; }",
        spot,
    )


async def test_a_named_folder_is_found_in_the_sidebar(page):
    assert await _href_at(page, await _find(page, "sent")) == "#sent"


async def test_matching_ignores_case(page):
    assert await _href_at(page, await _find(page, "SENT")) == "#sent"


async def test_an_unread_count_does_not_hide_a_folder(page):
    """Gmail writes "Spam 4". Matching the bare name would fail the moment mail arrives —
    the folder would become unreachable exactly when it had something in it."""
    assert await _href_at(page, await _find(page, "spam")) == "#spam"


async def test_a_users_own_label_works_too(page):
    """The reason the sidebar is tried FIRST: no allowlist can know a user's labels."""
    assert await _href_at(page, await _find(page, "Work")) == "#label/Work"


async def test_the_same_word_inside_a_MESSAGE_is_not_a_folder(page):
    """A body reading "check your Sent folder" must not become a navigation target —
    clicking prose does nothing while looking exactly like it worked."""
    spot = await _find(page, "please check your sent folder for the invoice.")
    assert spot is None


async def test_a_hidden_entry_is_not_offered(page):
    """An invisible link cannot be clicked; returning it produces a click into nothing and
    a success that did not happen."""
    assert await _find(page, "snoozed") is None


async def test_an_unknown_name_finds_nothing(page):
    assert await _find(page, "does-not-exist") is None


# ── the allowlist ───────────────────────────────────────────────────────────


def test_every_allowlisted_destination_is_a_gmail_fragment():
    """THE safety property. If any value here could become an absolute URL, an injected
    "open evil.example" turns navigation into a credential-harvest page."""
    from app.surface.playwright_surface import GMAIL_FOLDERS

    for name, destination in GMAIL_FOLDERS.items():
        assert destination.startswith("#"), f"{name!r} is not a same-page fragment"
        assert "//" not in destination
        assert ":" not in destination


def test_the_folders_people_actually_ask_for_are_covered():
    from app.surface.playwright_surface import GMAIL_FOLDERS

    for name in ("inbox", "sent", "drafts", "spam", "trash", "starred", "all mail"):
        assert name in GMAIL_FOLDERS


def test_archive_resolves_to_all_mail():
    """Archive is not a folder in Gmail — archived mail leaves the inbox and stays in All
    Mail. Refusing on that technicality would be pedantry with no upside."""
    from app.surface.playwright_surface import GMAIL_FOLDERS

    assert GMAIL_FOLDERS["archive"] == "#all"
