"""Which tab the agent drives, and when it is safe to look at it.

Pure unit tests on fakes — deliberately NOT in `test_cdp_attach.py`, which is browser-marked
and therefore excluded from the default run. These describe a decision, and a decision does
not need a browser to be wrong.
"""
from __future__ import annotations


class _FakePage:
    def __init__(self, url: str, closed: bool = False) -> None:
        self.url = url
        self._closed = closed

    def is_closed(self) -> bool:
        return self._closed


class _FakeContext:
    def __init__(self, *pages: _FakePage) -> None:
        self.pages = list(pages)


class TestReusingAnOpenTab:
    """Opening a fresh tab cold-booted Gmail on EVERY run — 37 seconds before the agent
    could act, during which the cockpit showed nothing and the run looked hung. It also put
    the agent in a tab the human was not watching.
    """

    def test_it_reuses_a_tab_already_on_the_mail_host(self):
        from app.surface.playwright_surface import _existing_mail_tab

        open_tab = _FakePage("https://mail.google.com/mail/u/0/#inbox")
        context = _FakeContext(_FakePage("https://news.example.com"), open_tab)

        assert _existing_mail_tab(context, "https://mail.google.com") is open_tab

    def test_it_matches_on_host_not_exact_url(self):
        """The human is on #sent or inside a thread. Demanding an exact match would reject
        every tab that is genuinely already there."""
        from app.surface.playwright_surface import _existing_mail_tab

        open_tab = _FakePage("https://mail.google.com/mail/u/0/#sent/FMfcgzQbdRxKlZ")
        context = _FakeContext(open_tab)

        assert _existing_mail_tab(context, "https://mail.google.com/") is open_tab

    def test_it_ignores_a_closed_tab(self):
        from app.surface.playwright_surface import _existing_mail_tab

        context = _FakeContext(_FakePage("https://mail.google.com/", closed=True))

        assert _existing_mail_tab(context, "https://mail.google.com") is None

    def test_it_opens_its_own_when_nothing_matches(self):
        from app.surface.playwright_surface import _existing_mail_tab

        context = _FakeContext(_FakePage("https://news.example.com"))

        assert _existing_mail_tab(context, "https://mail.google.com") is None

    def test_no_start_url_means_no_reuse(self):
        from app.surface.playwright_surface import _existing_mail_tab

        assert _existing_mail_tab(_FakeContext(_FakePage("https://mail.google.com")), None) is None

    def test_a_sign_in_wall_counts_as_rendered(self):
        """Waiting for a mail row on the login page would burn the whole readiness timeout
        before the loop could name `NOT_SIGNED_IN`."""
        from app.surface.playwright_surface import READY_SELECTORS

        assert any("identifier" in selector for selector in READY_SELECTORS)
