"""`/ws/bridge` — where a browser extension connects, and where it is authenticated.

Transport only, like `ws.py`: this file turns a socket into a `BridgeConnection` and does
nothing else with it.

**Authentication is not optional here, and it is not the same question as on `/ws/run`.** A
cockpit socket watches a run. A bridge socket *is* a mailbox: whoever holds it can ask the
extension to read, draft, and — with an approval it must still obtain — send. So a bridge
proves who it belongs to before a single frame is served, and a failure closes with a code
the extension knows not to retry: a wrong secret retried forever is a password oracle with a
progress bar.

**Every bridge binds to a real user.** It used to be one shared secret, which meant every
extension registered under the same owner and a second user's browser *replaced* the first's
in the registry — so user A's next run drove user B's mailbox. Not a degraded experience: a
data breach. Now a signed-in cockpit issues a single-use code, the extension trades it for a
durable bridge token, and `owner` is the person Google identified.

With `AUTH_MODE=off` there is exactly one user (`local`), which is what keeps a laptop setup
working without a Google project.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from app.api.auth_routes import PAIRING
from app.auth.pairing import NotPaired
from app.auth.tokens import BRIDGE_AUDIENCE, InvalidToken, mint_bridge, verify
from app.config.settings import Settings, get_settings
from app.surface.bridge import BridgeConnection, BridgeRegistry

logger = logging.getLogger(__name__)

#: Process-level, like `RUNS`: a bridge outlives the run that used it, because the extension
#: stays connected between runs.
BRIDGES = BridgeRegistry()

#: Close codes the extension recognises. 4401 means "your code was wrong" and 4403 means
#: "this server accepts no bridges"; both tell it to stop retrying and ask the human.
CLOSE_UNAUTHORIZED = 4401
CLOSE_DISABLED = 4403

#: The protocol version this backend speaks. A mismatch is a refusal, not a warning: a
#: bridge that half-understands the frames is worse than one that will not connect.
BRIDGE_PROTOCOL_VERSION = "1"

#: The owner used when `AUTH_MODE=off`. Matches what the run socket reports, so a localhost
#: setup pairs and runs without a Google project.
LOCAL_OWNER = "local"


def _resolve_owner(hello: dict[str, Any], settings: Settings) -> tuple[str, str, str] | None:
    """Who this extension belongs to, and a durable token for next time.

    Two ways in, and the order matters:

    1. **A bridge token** — what an extension presents on every connection after the first.
       Stateless and long-lived, so a backend restart does not unpair anybody. MV3 suspends
       service workers constantly, and an extension that had to be re-paired on every
       reconnect would be unusable.
    2. **A pairing code** — the one-time hand-off from a signed-in cockpit. Single use, and
       redeeming it mints the token above.

    Returns `(user_id, email, bridge_token)`, or `None` if neither checked out.
    """
    secret = settings.auth_secret.get_secret_value()

    token = str(hello.get("bridgeToken") or "").strip()
    if token:
        try:
            session = verify(token, secret, audience=BRIDGE_AUDIENCE)
        except InvalidToken:
            return None
        # Re-issued on every connect so a long-lived pairing rolls forward rather than
        # expiring out from under someone who uses it daily.
        return session.user_id, session.email, mint_bridge(session.user_id, session.email, secret)

    code = str(hello.get("pairingCode") or "").strip()
    if not code:
        return None
    try:
        user_id, email = PAIRING.redeem(code)
    except NotPaired:
        return None
    return user_id, email, mint_bridge(user_id, email, secret)


async def ws_bridge(websocket: WebSocket, settings: Settings | None = None) -> None:
    settings = settings or get_settings()

    await websocket.accept()

    if not settings.auth_secret.get_secret_value().strip():
        # Refusing outright beats accepting everyone. Without a signing secret there is no
        # way to mint or check a bridge token, and the failure mode of guessing "they
        # probably meant open" is somebody else's mailbox.
        await websocket.close(
            code=CLOSE_DISABLED,
            reason="this server has no AUTH_SECRET set, so it accepts no bridges",
        )
        return

    hello = await _read_hello(websocket)
    if hello is None:
        return

    if hello.get("protocolVersion") != BRIDGE_PROTOCOL_VERSION:
        await websocket.close(
            code=CLOSE_DISABLED,
            reason=(
                f"bridge protocol {hello.get('protocolVersion')!r} does not match this "
                f"server's {BRIDGE_PROTOCOL_VERSION!r}; rebuild the extension"
            ),
        )
        return

    resolved = _resolve_owner(hello, settings)
    if resolved is None:
        # Deliberately does not say whether the code was empty, expired, already used, or
        # merely wrong: distinguishing them is a free hint to anyone probing, and the fix —
        # get a fresh code from the cockpit — is the same either way.
        logger.warning("bridge rejected: neither a valid bridge token nor a live pairing code")
        await websocket.close(
            code=CLOSE_UNAUTHORIZED,
            reason="pair this browser from the cockpit to get a fresh code",
        )
        return
    owner, email, bridge_token = resolved

    async def send(payload: dict[str, Any]) -> None:
        await websocket.send_json(payload)

    connection = BridgeConnection(send)
    connection.extension_version = str(hello.get("extensionVersion") or "")

    BRIDGES.register(owner, connection)
    # The token goes back on every connect, not only on first pairing: the extension stores
    # whatever it is given, so re-issuing is what keeps a daily user from ever expiring.
    await send(
        {
            "type": "welcome",
            "sessionId": connection.session_id,
            "bridgeToken": bridge_token,
            "account": email,
        }
    )
    logger.info(
        "bridge connected for %s (extension %s, session %s)",
        email,
        connection.extension_version or "unknown",
        connection.session_id,
    )

    try:
        while True:
            frame = await websocket.receive_json()
            kind = frame.get("type")
            if kind in ("result", "error"):
                connection.resolve(frame)
            elif kind == "detached":
                # The tab went away mid-run. Fail everything in flight now rather than let
                # each call discover it separately, ninety seconds apart.
                reason = str(frame.get("reason") or "the Gmail tab was closed")
                logger.info("bridge detached: %s", reason)
                connection.close(reason)
            # Anything else is ignored: a newer extension talking to an older server should
            # degrade, not disconnect.
    except WebSocketDisconnect:
        logger.info("bridge disconnected (session %s)", connection.session_id)
    except Exception:
        logger.exception("bridge socket failed (session %s)", connection.session_id)
    finally:
        connection.close()
        BRIDGES.unregister(owner, connection)


async def _read_hello(websocket: WebSocket) -> dict[str, Any] | None:
    """The first frame must be `hello`. Anything else is not a bridge."""
    try:
        frame = await websocket.receive_json()
    except (WebSocketDisconnect, ValueError):
        return None
    if not isinstance(frame, dict) or frame.get("type") != "hello":
        await websocket.close(code=CLOSE_DISABLED, reason="expected a hello frame")
        return None
    return frame
