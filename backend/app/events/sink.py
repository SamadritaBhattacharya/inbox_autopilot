"""`EventSink` — the port everything observable flows through.

The graph never touches a WebSocket. It emits into this port, and the composition root
decides whether that means a socket, a buffer, or nothing. That is what lets the whole
streaming story be tested without a client connected.

**A sink must never fail a run.** A cockpit that disconnects mid-action is normal; a
dropped frame is a cosmetic loss, while an exception propagating out of an emit would turn
it into a failed task. Every implementation here swallows and logs.
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from app.events.protocol import AgentEvent

logger = logging.getLogger(__name__)


@runtime_checkable
class EventSink(Protocol):
    async def emit(self, event: AgentEvent) -> None: ...


class BufferSink:
    """Collects events in memory. The default in tests and the replay buffer in prod."""

    def __init__(self, *, capacity: int = 2000) -> None:
        self._capacity = capacity
        self.events: list[AgentEvent] = []
        #: Events discarded to stay under capacity. Reported rather than hidden — a replay
        #: that silently starts mid-run would look like a run that began strangely.
        self.dropped = 0

    async def emit(self, event: AgentEvent) -> None:
        self.events.append(event)
        if len(self.events) > self._capacity:
            overflow = len(self.events) - self._capacity
            del self.events[:overflow]
            self.dropped += overflow

    def of_type(self, event_type: str) -> list[AgentEvent]:
        return [event for event in self.events if event.event == event_type]

    def types(self) -> list[str]:
        return [event.event for event in self.events]

    def clear(self) -> None:
        self.events.clear()
        self.dropped = 0


class NullSink:
    """Discards everything. For paths where nobody is watching."""

    async def emit(self, event: AgentEvent) -> None:  # noqa: D102
        return None


class FanoutSink:
    """Broadcasts to several sinks — typically a replay buffer plus a live socket.

    A failing subscriber is removed rather than retried: the common cause is a closed
    socket, and retrying a closed socket on every event turns one dead cockpit into a
    stalled run.
    """

    def __init__(self, *sinks: EventSink) -> None:
        self._sinks: list[EventSink] = list(sinks)

    def add(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    def remove(self, sink: EventSink) -> None:
        if sink in self._sinks:
            self._sinks.remove(sink)

    async def emit(self, event: AgentEvent) -> None:
        for sink in list(self._sinks):
            try:
                await sink.emit(event)
            except Exception:
                logger.debug("dropping a failed event sink", exc_info=True)
                self.remove(sink)
