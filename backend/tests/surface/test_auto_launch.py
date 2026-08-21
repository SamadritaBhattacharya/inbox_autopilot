"""Starting the browser instead of complaining that it is missing.

Every failure on this path arrives as the same `ECONNREFUSED`, but the human's next move
differs completely between them — start Chrome, sign in, or close the instance squatting
the profile. These tests pin the *distinctions*, because collapsing them is what made the
original error useless.

No real browser here: `_ensure_browser` is the decision, and a decision is testable without
spawning Chrome.
"""
from __future__ import annotations

import json

import pytest

from app.surface import chrome_launcher as launcher
from app.surface.base import SurfaceUnavailable
from app.surface.playwright_surface import _ensure_browser

LOCAL = "http://127.0.0.1:9222"


def _profile(data_dir, name: str, *, size: int) -> None:
    """A profile directory that has browsed, i.e. one with a cookie store."""
    cookies = data_dir / name / "Network" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"x" * size)


class FakeChrome:
    """Records how Chrome would have been started, without starting it."""

    def __init__(self, *, port_opens: bool = True) -> None:
        self.calls: list[dict] = []
        self.port_opens = port_opens

    def spawn(
        self,
        *,
        port,
        data_dir,
        profile_directory=None,
        start_url="https://mail.google.com",
    ):
        self.calls.append(
            {"port": port, "data_dir": data_dir, "profile": profile_directory}
        )
        return object()

    @property
    def ports(self) -> list[int | None]:
        return [call["port"] for call in self.calls]


@pytest.fixture
def chrome(monkeypatch):
    fake = FakeChrome()
    monkeypatch.setattr(launcher, "spawn", fake.spawn)

    async def wait_for_port(host, port, *, timeout=20.0):
        return fake.port_opens

    monkeypatch.setattr(launcher, "wait_for_port", wait_for_port)
    return fake


@pytest.fixture
def port_closed(monkeypatch):
    async def closed(host, port, *, timeout=0.5):
        return False

    monkeypatch.setattr(launcher, "port_is_open", closed)


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setattr(launcher, "signed_in_profile", lambda data_dir: "Profile 5")


async def test_a_live_port_is_left_alone(monkeypatch, chrome):
    """Attaching to the browser the user already has open is the whole design.

    Starting a second one because we did not bother to look would strand them in a window
    that is not the one they signed into.
    """

    async def open_(host, port, *, timeout=0.5):
        return True

    monkeypatch.setattr(launcher, "port_is_open", open_)

    await _ensure_browser(LOCAL, auto_launch=True, profile_dir=None)

    assert chrome.calls == []


async def test_first_run_opens_a_window_with_no_debugging_port(
    monkeypatch, chrome, port_closed
):
    """The one configuration Google's sign-in accepts.

    Launching with the port on a profile that has never signed in would land the agent on
    the login wall, where it can do nothing — and where signing in is refused.
    """
    monkeypatch.setattr(launcher, "signed_in_profile", lambda data_dir: None)

    with pytest.raises(SurfaceUnavailable) as exc:
        await _ensure_browser(LOCAL, auto_launch=True, profile_dir=None)

    assert chrome.ports == [None], "first run must not open a debugging port"
    assert "Sign into Gmail" in str(exc.value)


async def test_a_signed_in_profile_is_launched_with_the_port(
    chrome, port_closed, signed_in
):
    await _ensure_browser(LOCAL, auto_launch=True, profile_dir=None)

    assert chrome.ports == [9222]
    assert chrome.calls[0]["data_dir"] == launcher.ISOLATED_PROFILE


async def test_it_opens_the_profile_that_is_signed_in_not_the_default(
    chrome, port_closed, signed_in
):
    """The bug this pins: signing in through the account picker puts the session in a
    numbered profile and leaves `Default` empty. Launching `Default` then shows the agent a
    login wall inside a browser the human is reading their inbox in."""
    await _ensure_browser(LOCAL, auto_launch=True, profile_dir=None)

    assert chrome.calls[0]["profile"] == "Profile 5"


async def test_an_explicit_profile_directory_wins(chrome, port_closed, signed_in, tmp_path):
    await _ensure_browser(LOCAL, auto_launch=True, profile_dir=str(tmp_path))

    assert chrome.calls[0]["data_dir"] == tmp_path


async def test_a_port_that_never_opens_blames_the_right_thing(
    chrome, port_closed, signed_in
):
    """Chrome silently drops --remote-debugging-port when another instance owns the
    profile. That looks exactly like the flag not working, so name it."""
    chrome.port_opens = False

    with pytest.raises(SurfaceUnavailable) as exc:
        await _ensure_browser(LOCAL, auto_launch=True, profile_dir=None)

    assert "already owns the profile" in str(exc.value)


async def test_auto_launch_off_is_respected(chrome, port_closed, signed_in):
    with pytest.raises(SurfaceUnavailable) as exc:
        await _ensure_browser(LOCAL, auto_launch=False, profile_dir=None)

    assert chrome.calls == []
    assert "CDP_AUTO_LAUNCH" in str(exc.value)


async def test_a_remote_endpoint_is_never_launched_locally(chrome, port_closed, signed_in):
    """Starting a local Chrome because a *remote* one is unreachable would attach the agent
    to the wrong mailbox entirely."""
    with pytest.raises(SurfaceUnavailable) as exc:
        await _ensure_browser(
            "http://10.0.0.5:9222", auto_launch=True, profile_dir=None
        )

    assert chrome.calls == []
    assert "cannot start it" in str(exc.value)


class TestProfileDetection:
    """Chrome moved the cookie store under `Network/` and kept the old path on older
    profiles. Missing either one strands a signed-in user in the first-run branch."""

    def test_modern_cookie_location(self, tmp_path):
        cookies = tmp_path / "Default" / "Network" / "Cookies"
        cookies.parent.mkdir(parents=True)
        cookies.touch()

        assert launcher.profile_is_signed_in(tmp_path)

    def test_legacy_cookie_location(self, tmp_path):
        cookies = tmp_path / "Default" / "Cookies"
        cookies.parent.mkdir(parents=True)
        cookies.touch()

        assert launcher.profile_is_signed_in(tmp_path)

    def test_a_launched_but_never_used_profile_is_not_signed_in(self, tmp_path):
        """Chrome writes the profile skeleton on launch. Treating that as a session is how
        you send someone to a login wall and call it success."""
        (tmp_path / "Default" / "Network").mkdir(parents=True)

        assert not launcher.profile_is_signed_in(tmp_path)

    def test_a_numbered_profile_is_found(self, tmp_path):
        """A user-data-dir contains profiles; it is not one. The account picker routinely
        puts the session somewhere other than `Default`."""
        _profile(tmp_path, "Profile 5", size=4096)
        (tmp_path / "Default" / "Network").mkdir(parents=True)

        assert launcher.signed_in_profile(tmp_path) == "Profile 5"

    def test_last_used_wins_over_a_bigger_cookie_store(self, tmp_path):
        """Reopen what Chrome itself would reopen — that is the window the human signed
        into, regardless of which profile has browsed more over its life."""
        _profile(tmp_path, "Profile 2", size=99_999)
        _profile(tmp_path, "Profile 5", size=1_024)
        (tmp_path / "Local State").write_text(
            json.dumps({"profile": {"last_used": "Profile 5"}}), encoding="utf-8"
        )

        assert launcher.signed_in_profile(tmp_path) == "Profile 5"

    def test_without_local_state_the_largest_store_wins(self, tmp_path):
        """A young profile has no `Local State`, and an unreadable one is not an error worth
        failing a run over."""
        _profile(tmp_path, "Profile 2", size=99_999)
        _profile(tmp_path, "Profile 5", size=1_024)

        assert launcher.signed_in_profile(tmp_path) == "Profile 2"

    def test_a_stale_last_used_falls_back(self, tmp_path):
        """`last_used` can name a profile that has since been deleted."""
        _profile(tmp_path, "Profile 2", size=4096)
        (tmp_path / "Local State").write_text(
            json.dumps({"profile": {"last_used": "Profile 9"}}), encoding="utf-8"
        )

        assert launcher.signed_in_profile(tmp_path) == "Profile 2"

    def test_a_missing_directory_is_not_signed_in(self, tmp_path):
        assert launcher.signed_in_profile(tmp_path / "nope") is None
