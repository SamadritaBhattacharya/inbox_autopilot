"""Event-loop factory — the one that can actually start a browser.

Playwright drives Chromium as a **child process**, and on Windows only `ProactorEventLoop`
can spawn one. `SelectorEventLoop` raises a bare `NotImplementedError` with an empty
message, which is a miserable thing to debug: the traceback names `subprocess_exec` rather
than anything to do with browsers, and the empty string means the error reaches the cockpit
as "could not start the browser:" and then stops.

The trap is that uvicorn selects the *wrong* loop for exactly the configuration developers
use. From `uvicorn/loops/asyncio.py`:

    if sys.platform == "win32" and not use_subprocess:
        return asyncio.ProactorEventLoop
    return asyncio.SelectorEventLoop

`use_subprocess` is true when uvicorn manages processes itself — which `--reload` turns on.
So `uvicorn ... --reload` on Windows yields the one loop that cannot launch Chromium, and
the default dev command is the broken one.

uvicorn's `--loop` accepts a custom import string, so pointing it here fixes the loop
without giving up reload. Everywhere other than Windows this defers to the default.
"""
from __future__ import annotations

import asyncio
import sys


def loop_factory() -> asyncio.AbstractEventLoop:
    """A loop that can spawn the browser. Pass as `--loop app.api.loop:loop_factory`."""
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()
