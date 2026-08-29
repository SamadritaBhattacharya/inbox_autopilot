"""`ExtensionEmailSurface` — the port, implemented over a browser we do not own.

The whole payoff of keeping `EmailSurface` at four methods arrives here: the graph, the
workers, the approval gate and the cockpit are all unchanged, and the mailbox is now the
user's own Chrome on the user's own machine. One line in the composition root chooses which.

**What this class deliberately cannot do.** It has no vault, no geometry map, and no way to
resolve a token. It forwards indices and tokens and receives tokenized observations. That is
not an inconvenience worked around — it is the security model: a backend that *could*
resolve `P17` would be a backend worth attacking.

The approval gate needs one accommodation. It calls `approve(fingerprint)` synchronously,
because on the local surface approving is a set insertion. Here the authorization lives in
the extension, so the fingerprint has to travel — and the gate must be handed a fingerprint
the *extension* computed, since only that side can see the resolved draft the human read.
`fingerprint_for()` exists for exactly that, and `approve()` ships the decision without
blocking the gate.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from inbox_contracts import ActionCall, ActionResult, Observation

from app.surface.base import SurfaceUnavailable
from app.surface.bridge import BridgeConnection, BridgeError, BridgeUnavailable

logger = logging.getLogger(__name__)


class ExtensionEmailSurface:
    """Drives the user's own Chrome through the bridge extension."""

    def __init__(
        self,
        connection: BridgeConnection,
        *,
        bound_verbs: set[str] | None = None,
    ) -> None:
        self._bridge = connection
        self._bound = sorted(bound_verbs or set())
        self._started = False
        #: Fire-and-forget approvals, kept so shutdown can await them rather than cancel a
        #: decision the human already made.
        self._pending_approvals: set[asyncio.Task[Any]] = set()

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind the extension to a Gmail tab and open a session.

        Separate from construction because it can fail for reasons the user must be told
        about — no Gmail tab, DevTools holding the debugger — and a constructor that can
        raise those is a constructor nobody can call safely.
        """
        try:
            await self._bridge.call("start", {"boundVerbs": self._bound})
        except BridgeUnavailable as exc:
            raise SurfaceUnavailable(str(exc)) from exc
        except BridgeError as exc:
            raise SurfaceUnavailable(str(exc)) from exc
        self._started = True

    async def close(self) -> None:
        # Let any in-flight approval finish first: dropping one would leave the extension
        # holding no authorization for a send the human already agreed to, and the next
        # attempt would ask them again.
        if self._pending_approvals:
            await asyncio.gather(*self._pending_approvals, return_exceptions=True)
        if not self._started:
            return
        self._started = False
        # The browser going away IS the stop. Nothing to clean up on this side.
        with contextlib.suppress(BridgeUnavailable, BridgeError):
            await self._bridge.call("stop")

    # ── the port ────────────────────────────────────────────────────────────

    async def observe(self) -> Observation:
        raw = await self._call("observe")
        try:
            return Observation.model_validate(raw)
        except Exception as exc:
            # The extension validates its own output before sending, so this means the two
            # sides have drifted. Say which side, or the next person debugs the wrong one.
            raise SurfaceUnavailable(
                f"the extension sent an observation this backend cannot read ({exc}). "
                "The extension and backend contracts are out of step — rebuild the "
                "extension after `pnpm run gen-contracts`."
            ) from exc

    async def act(self, call: ActionCall) -> ActionResult:
        try:
            raw = await self._call("act", {"call": call.model_dump(mode="json")})
        except BridgeError as exc:
            # A refusal from the far side is information the agent can act on, not a crash.
            return ActionResult(success=False, reason=str(exc), error_code=exc.code)
        return ActionResult.model_validate(raw)

    async def reset(self) -> str:
        """Ask the extension to clear anything a previous run left open.

        Best-effort by design: this drives the user's OWN browser, where a tab may have been
        closed or navigated away between runs. A reset that cannot run is a slightly messier
        starting state, never a reason to refuse the task the human just asked for.
        """
        try:
            raw = await self._call("reset", {})
        except Exception as exc:
            logger.info("extension could not reset the page: %s", exc)
            return ""
        return str(raw or "")

    async def preview(self, call: ActionCall) -> str:
        """The RESOLVED draft, read from the live compose fields.

        This is the one payload that legitimately carries real addresses back to the
        backend, and it goes straight to the authenticated cockpit's approval card. It must
        never re-enter the model's context — the gate is the only caller.
        """
        raw = await self._call("preview", {"call": call.model_dump(mode="json")})
        return str(raw or "")

    def approve(self, fingerprint: str) -> None:
        """Authorize ONE payload, in the extension.

        Synchronous to satisfy the port, but the work is a network call — so it is scheduled
        and tracked rather than awaited. The gate calls this immediately before returning to
        the loop, and the loop's next `act()` is another round trip behind it, so the
        ordering holds without making the gate async.
        """
        task = asyncio.create_task(self._approve(fingerprint))
        self._pending_approvals.add(task)
        task.add_done_callback(self._pending_approvals.discard)

    async def _approve(self, fingerprint: str) -> None:
        try:
            await self._bridge.call("approve", {"fingerprint": fingerprint})
        except (BridgeUnavailable, BridgeError) as exc:
            # Losing an approval fails CLOSED: the extension simply has no authorization, so
            # the send is refused and the gate asks again. Loud, because the user will
            # otherwise see a second approval card with no explanation.
            logger.warning("approval did not reach the extension: %s", exc)

    async def fingerprint_for(self, call: ActionCall) -> str:
        """The fingerprint the extension will check this call against.

        Computed there, not here, because it covers the resolved preview — the exact text
        the human is about to read — and this side cannot see it.
        """
        raw = await self._call("fingerprint", {"call": call.model_dump(mode="json")})
        return str(raw or "")

    # ── plumbing ────────────────────────────────────────────────────────────

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if not self._started and method not in {"start", "stop"}:
            raise SurfaceUnavailable("the bridge session was never started")
        try:
            return await self._bridge.call(method, params)
        except BridgeUnavailable as exc:
            raise SurfaceUnavailable(str(exc)) from exc
