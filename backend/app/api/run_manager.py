"""Run registry — a run is a server-side object; a socket is just a view onto it.

**The run outlives the cockpit watching it**, and this is a product requirement rather than
an optimisation. Runs last minutes and contain human pauses that outlast a browser tab. If a
refresh killed the run — and its browser — the durable-interrupt design would be pointless:
there is no value in a checkpointed pause that a page reload destroys.

So a disconnect **detaches the view**. The run, its browser, and any pending interrupt keep
going. Reconnecting with the same `thread_id` replays the buffered events and goes live.

**Replay is the same code path as live.** The buffer is a sink alongside the socket, so a
reconnecting cockpit receives exactly the frames it would have received had it never left.
Two code paths would drift, and the reattach path is the one nobody tests by hand.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.events.emitter import EventEmitter
from app.events.protocol import AgentEvent
from app.events.sink import BufferSink, EventSink, FanoutSink

logger = logging.getLogger(__name__)

Sender = Callable[[dict], Awaitable[None]]

#: How long a finished run stays attachable. Long enough that a refresh right at the end
#: still shows the result, short enough that memory does not grow without bound.
TERMINAL_TTL_SECONDS = 600


class SocketSink:
    """Sends events to one cockpit socket."""

    def __init__(self, send: Sender) -> None:
        self._send = send

    async def emit(self, event: AgentEvent) -> None:
        await self._send(event.to_wire())


@dataclass
class Run:
    """One agent run, and everything needed to watch or stop it."""

    thread_id: str
    buffer: BufferSink = field(default_factory=BufferSink)
    fanout: FanoutSink = field(default_factory=FanoutSink)
    task: asyncio.Task | None = None
    cleanup: Callable[[], Awaitable[None]] | None = None
    #: This run's PII vault, borrowed from the surface. Held so that human text arriving
    #: OUT OF BAND — a mid-run correction typed into the cockpit — can be tokenized against
    #: the same session as everything else. Without it, "also add alex@corp.com" reaches the
    #: model in the clear and carries no token the dispatcher would accept.
    vault: Any | None = None
    #: Resolved when a human answers a question or decides an approval.
    _answer: asyncio.Future | None = None
    finished_at: float | None = None

    def __post_init__(self) -> None:
        # The buffer is always a subscriber, so replay and live are the same stream.
        self.fanout.add(self.buffer)

    @property
    def emitter(self) -> EventEmitter:
        return EventEmitter(self.fanout)

    @property
    def is_finished(self) -> bool:
        return self.finished_at is not None

    # ── viewers ─────────────────────────────────────────────────────────────

    async def attach(self, send: Sender) -> SocketSink:
        """Replay what has happened, then go live.

        Replay first, subscribe second, and in that order: subscribing first would let a
        live event arrive mid-replay and appear out of sequence.
        """
        for event in list(self.buffer.events):
            await send(event.to_wire())
        if self.buffer.dropped:
            await send(
                AgentEvent(
                    event="status",
                    data={
                        "phase": "replay",
                        "message": f"{self.buffer.dropped} earlier events were trimmed",
                    },
                ).to_wire()
            )

        sink = SocketSink(send)
        self.fanout.add(sink)
        return sink

    def detach(self, sink: EventSink) -> None:
        """Stop sending to this viewer. The run continues."""
        self.fanout.remove(sink)

    # ── human replies ───────────────────────────────────────────────────────

    def expect_answer(self) -> asyncio.Future:
        self._answer = asyncio.get_running_loop().create_future()
        return self._answer

    def answer(self, value: object) -> bool:
        """Deliver a human reply. False when nothing was waiting for one."""
        if self._answer is None or self._answer.done():
            return False
        self._answer.set_result(value)
        return True

    # ── lifecycle ───────────────────────────────────────────────────────────

    def mark_finished(self) -> None:
        self.finished_at = time.monotonic()

    async def stop(self) -> None:
        """Cancel the run and tear down whatever it owns."""
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        if self.cleanup is not None:
            with contextlib.suppress(Exception):
                await self.cleanup()
        self.mark_finished()


class RunManager:
    """Process-level registry of live runs, keyed by `thread_id`."""

    def __init__(self, *, ttl_seconds: float = TERMINAL_TTL_SECONDS) -> None:
        self._runs: dict[str, Run] = {}
        self._ttl = ttl_seconds

    def get(self, thread_id: str) -> Run | None:
        return self._runs.get(thread_id)

    def create(self, thread_id: str) -> Run:
        run = Run(thread_id=thread_id)
        self._runs[thread_id] = run
        return run

    async def remove(self, thread_id: str) -> None:
        run = self._runs.pop(thread_id, None)
        if run is not None:
            await run.stop()

    async def gc(self) -> int:
        """Drop finished runs past their TTL.

        Opportunistic rather than scheduled — called when there is activity anyway, so an
        idle process does no work. Returns how many were freed.
        """
        now = time.monotonic()
        # `>=`, so a TTL of zero means "collect immediately" rather than "never" — which is
        # what a strict `>` would silently give you.
        stale = [
            thread_id
            for thread_id, run in self._runs.items()
            if run.finished_at is not None and now - run.finished_at >= self._ttl
        ]
        for thread_id in stale:
            await self.remove(thread_id)
        if stale:
            logger.info("garbage-collected %d finished run(s)", len(stale))
        return len(stale)

    def thread_ids(self) -> list[str]:
        return list(self._runs)

    async def shutdown(self) -> None:
        for thread_id in list(self._runs):
            await self.remove(thread_id)
