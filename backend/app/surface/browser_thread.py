"""Run the browser on a loop that can actually start it.

Playwright talks to its Node driver over a **child process**, and on Windows only
`ProactorEventLoop` can spawn one. Which loop the server runs on is not ours to choose —
uvicorn picks it, and it picks `SelectorEventLoop` whenever it manages processes itself,
which `--reload` turns on. An ASGI app cannot change the loop it is already running on.

So the browser gets its own thread with its own loop, and calls are forwarded onto it. The
server loop stays whatever the server wants it to be, and the surface works regardless of
how the process was started — `--reload`, a production server, or a script.

This engages **only when it has to**. On a loop that can already spawn a subprocess the
surface is used directly, with no thread, no forwarding, and no behaviour to go wrong. A
workaround that runs when it is not needed is just a second thing to debug.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import Any

from inbox_contracts import ActionCall, ActionResult, Observation

logger = logging.getLogger(__name__)


def loop_can_spawn_subprocesses(loop: asyncio.AbstractEventLoop | None = None) -> bool:
    """Can this loop start a child process — i.e. can it start a browser?

    Only Windows makes this interesting: every Unix loop can. `SelectorEventLoop` raises a
    bare `NotImplementedError` from `subprocess_exec`, so asking up front is far kinder than
    finding out from an exception with an empty message.
    """
    if sys.platform != "win32":
        return True
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return True
    return isinstance(loop, asyncio.ProactorEventLoop)


class BrowserLoop:
    """A daemon thread owning a subprocess-capable event loop."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        ready = threading.Event()

        def run() -> None:
            if sys.platform == "win32":
                loop = asyncio.ProactorEventLoop()
            else:
                loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            ready.set()
            loop.run_forever()

        self._thread = threading.Thread(target=run, name="browser-loop", daemon=True)
        self._thread.start()
        ready.wait()
        logger.info("browser running on a dedicated %s", type(self._loop).__name__)

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        assert self._loop is not None
        return self._loop

    async def call(self, coro: Any) -> Any:
        """Await `coro` on the browser loop, from the caller's loop."""
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, self.loop))

    async def shutdown(self) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        await asyncio.to_thread(self._thread.join, 5.0)
        self._loop = None


class ThreadedSurface:
    """`EmailSurface` proxy forwarding every call onto the browser's loop.

    Deliberately narrow: it implements the port and nothing more. Anything that reaches
    past it to the wrapped surface would be touching Playwright objects from the wrong
    thread, which fails in ways far more confusing than the problem this solves.
    """

    def __init__(self, surface: Any, browser_loop: BrowserLoop) -> None:
        self._surface = surface
        self._loop = browser_loop

    @property
    def vault(self) -> Any:
        # Plain state, not a Playwright object: safe to read from either thread.
        return self._surface.vault

    def approve(self, fingerprint: str) -> None:
        self._surface.approve(fingerprint)

    async def observe(self) -> Observation:
        return await self._loop.call(self._surface.observe())

    async def act(self, call: ActionCall) -> ActionResult:
        return await self._loop.call(self._surface.act(call))

    async def preview(self, call: ActionCall) -> str:
        """The fourth port method, and the one whose absence broke every send on Windows.

        `preview` reads the LIVE compose fields, so it touches Playwright and has to hop
        onto the browser loop exactly like `observe` and `act`. It was simply never added
        here: the approval gate called it, this proxy did not have it, and every gated
        action died with `AttributeError` at the moment a human was about to be shown what
        they were approving.

        Nothing caught it because the only surface the tests drive is `FakeEmailSurface`,
        which does implement `preview` — and this wrapper exists only on the Windows path
        (a selector event loop cannot spawn subprocesses), which no test exercises.
        `tests/surface/test_browser_thread.py` now checks this proxy against the port
        itself, so a fifth method added to `EmailSurface` fails loudly here rather than at
        the approval gate on somebody's real mailbox.
        """
        return await self._loop.call(self._surface.preview(call))

    async def start_screencast(self, on_frame: Any, **kwargs: Any) -> None:
        """Frames are produced on the browser loop and consumed on the server's.

        `on_frame` emits to the cockpit, so it must run where the WebSocket lives. Hopping
        back explicitly is the whole reason this wrapper exists rather than passing the
        callback straight through.
        """
        server_loop = asyncio.get_running_loop()

        async def bridged(data: str, seq: int) -> None:
            await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(on_frame(data, seq), server_loop)
            )

        await self._loop.call(self._surface.start_screencast(bridged, **kwargs))

    async def stop_screencast(self) -> None:
        await self._loop.call(self._surface.stop_screencast())
