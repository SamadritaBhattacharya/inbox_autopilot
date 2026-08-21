"""FastAPI application.

This layer adapts HTTP/WebSocket to services and does nothing else. No agent logic, no
LLM calls, no browser control lives here — those go through ports, wired in
`app.config.container`. A route that starts making decisions is a bug in the wrong file.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from inbox_contracts import PROTOCOL_VERSION

from app.api.ws import RUNS, ws_run
from app.config.settings import get_settings

logger = logging.getLogger(__name__)

# uvicorn configures its own loggers and leaves the root logger alone, so without this every
# `logger.warning` in this codebase goes nowhere and the server looks silent while failing.
if not logging.getLogger().handlers:  # pragma: no cover - process-level setup
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)-8s %(name)s: %(message)s"
    )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Tear down live runs and their browsers on shutdown.

    Without this a redeploy leaks a Chromium process per in-flight run, and nothing else in
    the system is positioned to notice.
    """
    _log_browser_capability()
    yield
    await RUNS.shutdown()


def _log_browser_capability() -> None:
    """Say at startup whether this process can drive a browser at all.

    On Windows only `ProactorEventLoop` can spawn the browser process, and uvicorn's
    `--reload` selects the loop that cannot. That no longer breaks anything — the browser
    falls back to a thread with its own loop — but it costs a thread, and knowing which mode
    a process is in turns a whole class of confusing report into one line at startup.
    """
    from app.surface.browser_thread import loop_can_spawn_subprocesses

    loop = asyncio.get_running_loop()
    if loop_can_spawn_subprocesses(loop):
        logger.info("event loop %s can start a browser directly", type(loop).__name__)
    else:
        logger.info(
            "event loop %s cannot spawn subprocesses, so the browser will run on a "
            "dedicated loop of its own. Runs work; `python -m app.api.dev` avoids the "
            "extra thread.",
            type(loop).__name__,
        )


app = FastAPI(
    title="Inbox Autopilot",
    version="0.1.0",
    description="Browser-driven email agent — the brain.",
    lifespan=lifespan,
)

# The cockpit is deployed separately (Vercel) and connects straight to this host: a
# serverless frontend platform cannot hold a long-lived WebSocket, so there is no proxy
# in front of us. That makes an explicit origin policy load-bearing rather than optional.
# TODO(M3): replace the permissive dev default with an allowlist from settings.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, object]:
    """Liveness plus the handshake facts a client needs before connecting.

    `protocolVersion` is here so a cockpit or executor built against a different contract
    version fails loudly at startup instead of mis-parsing frames later.

    Deliberately says nothing about credentials — not even whether they are present.
    """
    settings = get_settings()
    return {
        "status": "ok",
        "protocolVersion": PROTOCOL_VERSION,
        "emailSurface": settings.email_surface,
        "activeRuns": len(RUNS.thread_ids()),
    }


@app.websocket("/ws/run")
async def run_socket(websocket: WebSocket) -> None:
    """The cockpit connection: start a run, watch it, steer it, stop it.

    A disconnect detaches the view; the run continues and can be re-attached by
    `thread_id`. See `app.api.ws` and `docs/WS-PROTOCOL.md`.
    """
    await ws_run(websocket)
