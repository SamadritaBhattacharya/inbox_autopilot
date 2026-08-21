"""`python -m app.api.dev` — the dev server, with reload, that can drive a browser.

A module rather than a line in the README because the plain command is subtly wrong on
Windows (see `app/api/loop.py`) and a wrong default that fails deep inside Playwright costs
far more than a two-line entry point.
"""
from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "app.api.main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=True,
        # Not the default factory: uvicorn pairs --reload with a loop that cannot spawn
        # subprocesses on Windows, and the browser is a subprocess.
        loop="app.api.loop:loop_factory",
    )


if __name__ == "__main__":
    main()
