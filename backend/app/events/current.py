"""The emitter for the run currently executing on this task.

**Why a context variable, and why only for this.** The LLM gateway is built once per process
— it holds the keys and the fallback chain — while an emitter belongs to one run. A provider
failing over is a fact the gateway learns and the cockpit needs, and there is no argument
path between them that does not mean rebuilding the client per run or handing every port an
emitter it has no other use for.

A `ContextVar` is the right tool precisely because the alternative is a module global that
two concurrent runs would share. Async tasks inherit the context they were spawned in, so
each run's nodes see their own emitter and nobody else's.

Deliberately narrow: this carries **notifications about the machinery**, never agent state.
Anything the graph reasons about belongs in `AgentState`, and putting it here instead would
be exactly the "mutable state outside the state" the architecture forbids.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar

from app.events.emitter import EventEmitter

logger = logging.getLogger(__name__)

_current: ContextVar[EventEmitter | None] = ContextVar("current_emitter", default=None)


def bind(emitter: EventEmitter) -> None:
    """Attach this run's emitter to the current task context."""
    _current.set(emitter)


def note_provider(provider: str, status: str, detail: str) -> None:
    """Tell the cockpit something happened to a model provider.

    Fire-and-forget, and synchronous by design: the callers are inside the retry path of an
    LLM call, where awaiting an emit would mean a slow socket could stall the model request
    that is already struggling.

    Silent when nothing is bound — the gateway is used from scripts, tests and the benchmark
    where there is no run and no cockpit, and none of them should need to care.
    """
    emitter = _current.get()
    if emitter is None:
        return
    import asyncio

    try:
        asyncio.get_running_loop().create_task(
            emitter.provider(provider=provider, status=status, detail=detail)
        )
    except RuntimeError:
        # No loop: a synchronous caller. The log line above the caller already covered it.
        logger.debug("no running loop; provider notice dropped: %s %s", provider, status)
