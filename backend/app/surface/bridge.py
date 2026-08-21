"""The bridge — request/response RPC over one extension's socket.

The extension is the executor now: it holds the DOM, the vault, and the geometry map, and it
is the only party that can resolve a token or a coordinate. This module is the plumbing that
lets the graph call it as if it were local.

**Why request/response and not a second event stream.** The backend already has an event
channel to the cockpit. A second, differently-ordered stream would mean two orderings to
reason about and two places for a frame to go missing. Here every call has an id and exactly
one reply, so a lost reply is a timeout with a name rather than a run that quietly stops.

**The backend is always the caller.** The extension never pushes work; it answers. That is
what bounds the trust relationship in the other direction too — a compromised backend can
ask for the seven methods the port defines and nothing else, because that is all the
extension will parse.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: How long a single RPC may take before it is a failure rather than a slow browser.
#:
#: Generous, because the far end is a real browser doing real work — typing a long body is
#: seconds of legitimate latency. The point of the wall is that a DEAD bridge fails the run
#: instead of hanging it forever; the extension enforces its own, tighter per-verb walls.
DEFAULT_CALL_TIMEOUT = 90.0


class BridgeUnavailable(RuntimeError):
    """No extension is connected, or the one that was has gone away.

    Distinct from an action that failed: a failed action is information the agent can reason
    about, whereas an unreachable bridge ends the run typed. Collapsing the two makes a
    closed laptop lid look like a misclick.
    """


class Sender(Protocol):
    async def __call__(self, payload: dict[str, Any]) -> None: ...


class BridgeConnection:
    """One connected extension, exposed as awaitable method calls.

    Owns the id → future map. Every pending call is failed explicitly when the socket goes
    away: without that, a graph awaiting a reply waits for the full timeout on a connection
    already known to be dead, and the user watches a run do nothing for ninety seconds.
    """

    def __init__(self, send: Sender, *, call_timeout: float = DEFAULT_CALL_TIMEOUT) -> None:
        self._send = send
        self._timeout = call_timeout
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._closed = False
        #: Set once the extension has said hello and been accepted.
        self.session_id = uuid.uuid4().hex[:12]
        self.extension_version = ""

    # ── calling the extension ───────────────────────────────────────────────

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Invoke one method and wait for its reply."""
        if self._closed:
            raise BridgeUnavailable("the browser extension disconnected")

        call_id = uuid.uuid4().hex[:12]
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[call_id] = future

        try:
            await self._send(
                {"type": "call", "id": call_id, "method": method, "params": params or {}}
            )
        except Exception as exc:
            self._pending.pop(call_id, None)
            raise BridgeUnavailable(f"could not reach the extension: {exc}") from exc

        try:
            return await asyncio.wait_for(future, timeout=self._timeout)
        except TimeoutError as exc:
            raise BridgeUnavailable(
                f"the extension did not answer {method!r} within {self._timeout:.0f}s. "
                "The Gmail tab may be closed, or Chrome may have suspended the extension."
            ) from exc
        finally:
            self._pending.pop(call_id, None)

    # ── receiving from the extension ────────────────────────────────────────

    def resolve(self, frame: dict[str, Any]) -> None:
        """Hand a reply to whoever is waiting for it.

        An unknown id is dropped rather than raised: it means a call that already timed out,
        and turning a late reply into an error would replace one failure with two.
        """
        call_id = str(frame.get("id") or "")
        future = self._pending.get(call_id)
        if future is None or future.done():
            return

        if frame.get("ok"):
            future.set_result(frame.get("result"))
            return

        error = frame.get("error") or {}
        message = str(error.get("message") or "the extension reported a failure")
        code = error.get("code")
        if code == "SURFACE_UNAVAILABLE":
            future.set_exception(BridgeUnavailable(message))
        else:
            future.set_exception(BridgeError(message, code))

    def close(self, reason: str = "the browser extension disconnected") -> None:
        """Fail everything in flight, loudly and at once."""
        self._closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(BridgeUnavailable(reason))
        self._pending.clear()

    @property
    def closed(self) -> bool:
        return self._closed


class BridgeError(RuntimeError):
    """The extension refused or failed a call, in a way the agent can reason about."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class BridgeRegistry:
    """Which extension belongs to which owner.

    Keyed by owner rather than by socket so a run can find its browser after the extension
    reconnects — which it does routinely, because Chrome suspends MV3 service workers. A
    registry keyed on the connection would lose the binding every thirty seconds.
    """

    def __init__(self) -> None:
        self._by_owner: dict[str, BridgeConnection] = {}

    def register(self, owner: str, connection: BridgeConnection) -> None:
        # One browser per owner. A second connection REPLACES the first rather than joining
        # it: two extensions driving one mailbox would interleave clicks, and the older
        # socket is almost always a stale worker Chrome has not finished killing.
        previous = self._by_owner.get(owner)
        if previous is not None and previous is not connection:
            previous.close("replaced by a newer connection from the same browser")
        self._by_owner[owner] = connection
        logger.info("bridge registered for %s (session %s)", owner, connection.session_id)

    def unregister(self, owner: str, connection: BridgeConnection) -> None:
        # Only if it is still ours: a slow close arriving after a reconnect must not evict
        # the live connection that replaced it.
        if self._by_owner.get(owner) is connection:
            del self._by_owner[owner]
            logger.info("bridge unregistered for %s", owner)

    def get(self, owner: str) -> BridgeConnection | None:
        connection = self._by_owner.get(owner)
        if connection is not None and connection.closed:
            self._by_owner.pop(owner, None)
            return None
        return connection

    def require(self, owner: str) -> BridgeConnection:
        connection = self.get(owner)
        if connection is None:
            raise BridgeUnavailable(
                "No browser is connected. Install the Inbox Autopilot bridge extension, "
                "open Gmail, and pair it — the agent drives the browser you are signed into."
            )
        return connection

    def owners(self) -> list[str]:
        return list(self._by_owner)

    async def shutdown(self) -> None:
        for connection in list(self._by_owner.values()):
            connection.close("the server is shutting down")
        self._by_owner.clear()
