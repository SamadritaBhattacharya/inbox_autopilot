"""The event loop has to be able to start a browser.

This looks like trivia until it costs an afternoon. Playwright launches Chromium as a child
process; on Windows only `ProactorEventLoop` can spawn one, and uvicorn pairs `--reload`
with `SelectorEventLoop`, which cannot. The resulting `NotImplementedError` carries an empty
message and a traceback that never mentions browsers.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from app.api.loop import loop_factory


def test_the_factory_returns_a_usable_loop():
    loop = loop_factory()
    try:
        assert isinstance(loop, asyncio.AbstractEventLoop)
    finally:
        loop.close()


@pytest.mark.skipif(sys.platform != "win32", reason="the trap is Windows-only")
def test_on_windows_it_returns_the_loop_that_can_spawn_a_browser():
    loop = loop_factory()
    try:
        assert isinstance(loop, asyncio.ProactorEventLoop)
    finally:
        loop.close()


@pytest.mark.skipif(sys.platform != "win32", reason="the trap is Windows-only")
def test_the_selector_loop_failure_is_explained_rather_than_bare():
    """The empty-message `NotImplementedError` must never reach a human as-is.

    It surfaces in the cockpit as "could not start the browser:" followed by nothing, which
    tells the user neither what broke nor what to do.
    """
    from app.surface.playwright_surface import SurfaceUnavailable, launch_surface

    async def boot():
        surface, close = await launch_surface(headless=True)
        await close()

    loop = asyncio.SelectorEventLoop()
    try:
        with pytest.raises(SurfaceUnavailable) as exc:
            loop.run_until_complete(boot())
    finally:
        loop.close()

    message = str(exc.value)
    assert message.strip(), "the diagnosis must not be empty"
    assert "app.api.dev" in message, "it must name the fix, not just the symptom"
