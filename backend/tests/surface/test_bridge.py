"""The bridge: RPC to a browser we do not own, and the auth that guards it.

A bridge socket is not like a cockpit socket. A cockpit socket watches a run; a bridge socket
**is a mailbox** — whoever holds it can ask the extension to read, draft, and (with an
approval it must still obtain) send. So the tests that matter most here are the ones about
refusing, not the ones about working.
"""
from __future__ import annotations

import asyncio

import pytest
from inbox_contracts import ActionCall

from app.surface.base import SurfaceUnavailable
from app.surface.bridge import (
    BridgeConnection,
    BridgeError,
    BridgeRegistry,
    BridgeUnavailable,
)
from app.surface.extension_surface import ExtensionEmailSurface


class FakeSocket:
    """Records what was sent, and answers on demand."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail_with: Exception | None = None

    async def __call__(self, payload: dict) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(payload)

    @property
    def last_id(self) -> str:
        return str(self.sent[-1]["id"])

    @property
    def methods(self) -> list[str]:
        return [frame.get("method") for frame in self.sent]


def connection(timeout: float = 5.0) -> tuple[BridgeConnection, FakeSocket]:
    socket = FakeSocket()
    return BridgeConnection(socket, call_timeout=timeout), socket


async def answer(bridge: BridgeConnection, socket: FakeSocket, result: object) -> None:
    """Let the call reach the socket, then reply to it."""
    await asyncio.sleep(0)
    bridge.resolve({"type": "result", "id": socket.last_id, "ok": True, "result": result})


# ── the RPC ─────────────────────────────────────────────────────────────────


async def test_a_call_returns_its_matching_reply():
    bridge, socket = connection()

    task = asyncio.create_task(bridge.call("observe"))
    await answer(bridge, socket, {"elements": []})

    assert await task == {"elements": []}


async def test_replies_are_matched_by_id_not_by_arrival():
    """Two calls in flight must not swap answers. The failure is silent and produces an
    agent acting on a result from a different question."""
    bridge, socket = connection()

    first = asyncio.create_task(bridge.call("observe"))
    await asyncio.sleep(0)
    first_id = socket.last_id
    second = asyncio.create_task(bridge.call("preview"))
    await asyncio.sleep(0)
    second_id = socket.last_id

    # Answer out of order, on purpose.
    bridge.resolve({"type": "result", "id": second_id, "ok": True, "result": "second"})
    bridge.resolve({"type": "result", "id": first_id, "ok": True, "result": "first"})

    assert await first == "first"
    assert await second == "second"


async def test_a_failure_from_the_extension_is_typed_not_fatal():
    bridge, socket = connection()

    task = asyncio.create_task(bridge.call("act"))
    await asyncio.sleep(0)
    bridge.resolve(
        {
            "type": "error",
            "id": socket.last_id,
            "ok": False,
            "error": {"message": "stale index", "code": "STALE_INDEX"},
        }
    )

    with pytest.raises(BridgeError) as exc:
        await task
    assert exc.value.code == "STALE_INDEX"


async def test_an_unreachable_browser_is_distinct_from_a_failed_action():
    """Collapsing the two makes a closed laptop lid look like a misclick."""
    bridge, socket = connection()

    task = asyncio.create_task(bridge.call("observe"))
    await asyncio.sleep(0)
    bridge.resolve(
        {
            "type": "error",
            "id": socket.last_id,
            "ok": False,
            "error": {"message": "tab closed", "code": "SURFACE_UNAVAILABLE"},
        }
    )

    with pytest.raises(BridgeUnavailable):
        await task


async def test_a_dead_socket_fails_everything_in_flight_at_once():
    """Otherwise the graph waits the full timeout on a connection already known to be dead,
    and the user watches a run do nothing for ninety seconds."""
    bridge, _ = connection(timeout=60.0)

    first = asyncio.create_task(bridge.call("observe"))
    second = asyncio.create_task(bridge.call("preview"))
    await asyncio.sleep(0)

    bridge.close("the Gmail tab was closed")

    for task in (first, second):
        with pytest.raises(BridgeUnavailable):
            await task


async def test_a_call_that_is_never_answered_times_out_by_name():
    bridge, _ = connection(timeout=0.05)

    with pytest.raises(BridgeUnavailable, match="observe"):
        await bridge.call("observe")


async def test_a_late_reply_is_dropped_rather_than_raised():
    """It means a call that already timed out; turning it into an error would replace one
    failure with two."""
    bridge, socket = connection(timeout=0.05)

    with pytest.raises(BridgeUnavailable):
        await bridge.call("observe")

    bridge.resolve({"type": "result", "id": socket.last_id, "ok": True, "result": "late"})


async def test_calling_a_closed_bridge_says_so_immediately():
    bridge, _ = connection()
    bridge.close()

    with pytest.raises(BridgeUnavailable):
        await bridge.call("observe")


# ── the registry ────────────────────────────────────────────────────────────


class TestRegistry:
    def test_a_reconnect_replaces_the_old_connection(self):
        """Chrome suspends MV3 workers constantly, so reconnects are routine. Two live
        bridges for one mailbox would interleave clicks."""
        registry = BridgeRegistry()
        first, _ = connection()
        second, _ = connection()

        registry.register("default", first)
        registry.register("default", second)

        assert registry.get("default") is second
        assert first.closed, "the stale connection must be torn down, not left dangling"

    def test_a_late_close_does_not_evict_the_live_connection(self):
        # The old socket's cleanup often arrives AFTER its replacement registered.
        registry = BridgeRegistry()
        first, _ = connection()
        second, _ = connection()
        registry.register("default", first)
        registry.register("default", second)

        registry.unregister("default", first)

        assert registry.get("default") is second

    def test_a_closed_connection_is_not_handed_out(self):
        registry = BridgeRegistry()
        bridge, _ = connection()
        registry.register("default", bridge)
        bridge.close()

        assert registry.get("default") is None

    def test_requiring_a_missing_bridge_explains_what_to_do(self):
        with pytest.raises(BridgeUnavailable, match="extension"):
            BridgeRegistry().require("default")


# ── the surface ─────────────────────────────────────────────────────────────


class TestExtensionSurface:
    async def test_it_starts_a_session_with_its_bound_verbs(self):
        bridge, socket = connection()
        surface = ExtensionEmailSurface(bridge, bound_verbs={"Click", "Type"})

        task = asyncio.create_task(surface.start())
        await answer(bridge, socket, {"tabId": 7})
        await task

        assert socket.methods == ["start"]
        assert socket.sent[0]["params"]["boundVerbs"] == ["Click", "Type"]

    async def test_an_action_refusal_comes_back_as_a_typed_result(self):
        """A refusal is information the agent can act on, not an exception that kills the
        run."""
        bridge, socket = connection()
        surface = ExtensionEmailSurface(bridge)
        surface._started = True  # noqa: SLF001 - start() is covered above

        task = asyncio.create_task(surface.act(ActionCall(name="Click", args={"index": 9})))
        await asyncio.sleep(0)
        bridge.resolve(
            {
                "type": "error",
                "id": socket.last_id,
                "ok": False,
                "error": {"message": "[9] is not on screen", "code": "STALE_INDEX"},
            }
        )
        result = await task

        assert result.success is False
        assert result.error_code == "STALE_INDEX"

    async def test_a_dead_bridge_ends_the_run_typed(self):
        bridge, _ = connection()
        surface = ExtensionEmailSurface(bridge)
        surface._started = True  # noqa: SLF001
        bridge.close()

        with pytest.raises(SurfaceUnavailable):
            await surface.observe()

    async def test_an_unreadable_observation_names_the_drift(self):
        """The extension validates its own output, so this means the contracts are out of
        step — and the message has to say which side to rebuild."""
        bridge, socket = connection()
        surface = ExtensionEmailSurface(bridge)
        surface._started = True  # noqa: SLF001

        task = asyncio.create_task(surface.observe())
        await answer(bridge, socket, {"nonsense": True})

        with pytest.raises(SurfaceUnavailable, match="out of step"):
            await task

    async def test_the_backend_never_gets_a_way_to_resolve_a_token(self):
        """The security model, as a test: this surface has no vault and no geometry."""
        bridge, _ = connection()
        surface = ExtensionEmailSurface(bridge)

        assert not hasattr(surface, "vault")
        assert not hasattr(surface, "_vault")
        assert not hasattr(surface, "_geometry")

    async def test_approving_ships_the_decision_without_blocking_the_gate(self):
        bridge, socket = connection()
        surface = ExtensionEmailSurface(bridge)

        surface.approve("Send|index=9|content=abc")
        await asyncio.sleep(0)

        assert socket.methods == ["approve"]
        assert socket.sent[0]["params"]["fingerprint"] == "Send|index=9|content=abc"

    async def test_a_lost_approval_fails_closed(self):
        """The extension simply holds no authorization, so the send is refused and the gate
        asks again — never the other way round."""
        bridge, socket = connection()
        socket.fail_with = RuntimeError("socket gone")
        surface = ExtensionEmailSurface(bridge)

        surface.approve("Send|index=9")
        await asyncio.sleep(0)

        assert socket.sent == []
