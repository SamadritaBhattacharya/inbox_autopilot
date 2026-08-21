"""Browser-on-its-own-thread: engages only when needed, and forwards faithfully."""
from __future__ import annotations

import asyncio
import sys

import pytest

from app.surface.browser_thread import (
    BrowserLoop,
    ThreadedSurface,
    loop_can_spawn_subprocesses,
)


def test_a_proactor_loop_needs_no_workaround():
    loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    try:
        assert loop_can_spawn_subprocesses(loop)
    finally:
        loop.close()


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows has the broken loop")
def test_a_selector_loop_is_detected_as_incapable():
    """The check must be made up front, not discovered from an empty exception."""
    loop = asyncio.SelectorEventLoop()
    try:
        assert not loop_can_spawn_subprocesses(loop)
    finally:
        loop.close()


async def test_calls_are_forwarded_onto_the_browser_loop():
    """The proxy must actually change threads, or it is doing nothing at all."""
    browser_loop = BrowserLoop()
    server_thread = asyncio.get_running_loop()

    async def where_am_i() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    try:
        ran_on = await browser_loop.call(where_am_i())
        assert ran_on is browser_loop.loop
        assert ran_on is not server_thread
    finally:
        await browser_loop.shutdown()


async def test_frames_are_handed_back_to_the_server_loop():
    """Frames are produced on the browser loop but must be emitted where the socket lives."""
    browser_loop = BrowserLoop()
    server_loop = asyncio.get_running_loop()
    seen: list[asyncio.AbstractEventLoop] = []

    class FakeSurface:
        async def start_screencast(self, on_frame, **kwargs):
            # Stand in for Chrome: emit one frame from the browser loop.
            await on_frame("jpeg-bytes", 1)

        async def stop_screencast(self):
            return None

    async def on_frame(data: str, seq: int) -> None:
        seen.append(asyncio.get_running_loop())

    surface = ThreadedSurface(FakeSurface(), browser_loop)
    try:
        await surface.start_screencast(on_frame)
        assert seen == [server_loop], "the frame callback ran on the wrong loop"
    finally:
        await browser_loop.shutdown()
