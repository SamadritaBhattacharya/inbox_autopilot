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

from app.api.auth_routes import router as auth_router
from app.api.bridge_ws import ws_bridge
from app.api.ws import RUNS, ws_run
from app.auth.tokens import InvalidToken, verify
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
    _log_auth_posture()
    _log_browser_capability()
    yield
    await RUNS.shutdown()


def _log_auth_posture() -> None:
    """Say at startup who can use this server.

    Loud on purpose when it is open. `auth_mode="off"` is the right setting on a laptop and
    a breach on a public URL, and the difference is invisible from the outside — which is
    exactly why it has to be visible from the inside.
    """
    settings = get_settings()
    if settings.auth_mode == "off":
        logger.warning(
            "AUTH IS OFF — anyone who can reach this server can run the agent and drive "
            "whatever mailbox is paired to it. Fine on localhost; set AUTH_MODE=google "
            "before exposing this to a network."
        )
        return
    if not settings.auth_ready():
        logger.error(
            "AUTH_MODE=google but it is not configured: set GOOGLE_CLIENT_ID, "
            "GOOGLE_CLIENT_SECRET and AUTH_SECRET. Sign-in will fail until you do."
        )
        return
    logger.info("auth: Google sign-in (identity only) · origins %s", settings.origins())


def _log_browser_capability() -> None:
    """Say at startup which surface this process drives, and whether it can.

    On Windows only `ProactorEventLoop` can spawn the browser process, and uvicorn's
    `--reload` selects the loop that cannot. That no longer breaks anything — the browser
    falls back to a thread with its own loop — but it costs a thread, and knowing which mode
    a process is in turns a whole class of confusing report into one line at startup.

    **The loop question only applies when THIS process launches the browser.** With the
    bridge extension the browser is the user's own Chrome and no subprocess is spawned, so
    warning about event loops there is noise that sends people to fix the wrong thing.
    """
    from app.config.settings import get_settings
    from app.surface.browser_thread import loop_can_spawn_subprocesses

    surface = get_settings().email_surface
    if surface == "extension":
        logger.info(
            "surface: extension — the browser is the user's own Chrome, reached over "
            "/ws/bridge. This process launches nothing, so any event loop will do."
        )
        return
    if surface == "fake":
        logger.info("surface: fake — no browser is involved.")
        return

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

# The cockpit is deployed separately and connects straight to this host: a serverless
# frontend platform cannot hold a long-lived WebSocket, so there is no proxy in front of us.
# That makes an explicit origin policy load-bearing rather than optional.
#
# `*` was the default here for a long time and is not one any more. With a bearer token in
# play it is the difference between a private API and a public one: any page on the internet
# could otherwise call this on a signed-in user's behalf.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.include_router(auth_router)


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


@app.websocket("/ws/bridge")
async def bridge_socket(websocket: WebSocket) -> None:
    """Where a browser extension connects.

    A separate route from `/ws/run` because it is a different trust question: a cockpit
    socket watches a run, whereas a bridge socket IS a mailbox. It authenticates before it
    serves a single frame.
    """
    await ws_bridge(websocket)


@app.websocket("/ws/run")
async def run_socket(websocket: WebSocket) -> None:
    """The cockpit connection: start a run, watch it, steer it, stop it.

    A disconnect detaches the view; the run continues and can be re-attached by
    `thread_id`. See `app.api.ws` and `docs/WS-PROTOCOL.md`.

    **Authenticated before it is accepted.** A browser WebSocket cannot set headers, so the
    session token rides the handshake as `?token=`. Refusing here rather than after `accept`
    means an unauthenticated client never reaches the run manager at all.
    """
    settings = get_settings()
    owner = "local"

    if settings.auth_mode != "off":
        token = websocket.query_params.get("token", "")
        try:
            session = verify(token, settings.auth_secret.get_secret_value())
        except InvalidToken:
            # 4401 rather than a plain close: the cockpit distinguishes "sign in again" from
            # "the network dropped", and retrying the second forever is how a login loop
            # gets built by accident.
            await websocket.close(code=4401, reason="sign in to continue")
            return
        owner = session.user_id

    await ws_run(websocket, owner=owner)
