"""Attaching to a browser the user already signed into.

This is the only path that reaches real Gmail. Google refuses its sign-in flow inside an
automation-controlled browser, so the agent never signs in — it joins a session a human
already authenticated. See `docs/RUNNING.md`.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from app.surface.playwright_surface import (
    SurfaceUnavailable,
    connect_surface,
    resolve_chromium,
)

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        resolve_chromium() is None,
        reason="no Chromium build found; run `playwright install chromium`",
    ),
]

FIXTURE = (Path(__file__).resolve().parents[1] / "fixtures" / "inbox.html").as_uri()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def debuggable_browser(tmp_path):
    """A browser started the way a user would start theirs, with a debugging port."""
    port = _free_port()
    binary = shutil.which("chrome") or resolve_chromium(headless=True)
    proc = subprocess.Popen(
        [
            binary,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={tmp_path / 'profile'}",
            "--headless=new",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(4)
    try:
        yield port, proc
    finally:
        proc.terminate()


async def test_it_attaches_to_a_running_browser(debuggable_browser):
    port, _ = debuggable_browser
    surface, close = await connect_surface(
        endpoint=f"http://127.0.0.1:{port}", start_url=FIXTURE
    )
    try:
        observation = await surface.observe()
        assert observation.elements
    finally:
        await close()


async def test_closing_the_run_does_not_close_the_users_browser(debuggable_browser):
    """A run ending must not take the human's browser with it.

    They signed in there. Quitting it would mean signing in again every run, which defeats
    the entire reason for attaching.
    """
    port, proc = debuggable_browser
    surface, close = await connect_surface(
        endpoint=f"http://127.0.0.1:{port}", start_url=FIXTURE
    )
    await surface.observe()
    await close()

    assert proc.poll() is None, "disconnecting killed the browser we attached to"


async def test_a_missing_browser_is_explained_not_dumped():
    """Nothing is listening and we were told not to start it: say what to run.

    `auto_launch=False` is the point of the test — with it on, the honest behaviour is to
    start the browser rather than report a problem, which is covered in `test_auto_launch`.
    """
    with pytest.raises(SurfaceUnavailable) as exc:
        await connect_surface(
            endpoint=f"http://127.0.0.1:{_free_port()}", auto_launch=False
        )

    message = str(exc.value)
    assert "scripts/chrome.py serve" in message
    assert "CDP_AUTO_LAUNCH" in message

