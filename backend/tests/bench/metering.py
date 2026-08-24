"""Counting what a run spends, without the graph knowing it is being counted.

A decorator over the `LLMClient` port rather than a change to any node: the thing being
measured must not be modified to make measuring possible, or the benchmark stops describing
the system that actually ships.

**Why this is not simply `UsageTracker`.** `app/llm/usage.py` already knows how to aggregate
by provider and by role, and this reuses it — but the tracker collects `Attempt`s from
`FallbackLLMClient(on_attempt=...)`, which only exists on the real chain. A scripted client
produces no attempts, so this adapts one to the other.

Worth noting while here: `UsageTracker.drain_step_records` is written and **nothing in
`app/` calls it**, so LLM usage never reaches a `StepRecord` on a live run. The metering the
trajectory promises is built and unwired. That is an app-side gap, not a harness one, and
wiring it is deliberately left to its own change.
"""
from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from app.llm.base import LLMResult, Message, ProviderError
from app.llm.fallback import Attempt
from app.llm.usage import UsageTracker
from app.telemetry.records import Role


class MeteringClient:
    """Wraps any `LLMClient`, recording every call into a `UsageTracker`.

    Satisfies the port exactly, so the graph cannot tell the difference — which is the point.
    """

    def __init__(self, inner, tracker: UsageTracker | None = None) -> None:
        self._inner = inner
        self.tracker = tracker or UsageTracker()

    @property
    def call_count(self) -> int:
        return self.tracker.call_count

    async def complete(
        self,
        *,
        role: Role,
        messages: Sequence[Message],
        tools: Sequence[type[BaseModel]] | None = None,
    ) -> LLMResult:
        try:
            result = await self._inner.complete(role=role, messages=messages, tools=tools)
        except ProviderError as exc:
            # Recorded, not swallowed. A rejected call still spent a request against the
            # allowance, and a usage view that counts only successes understates the bill
            # exactly when headroom is tightest.
            self.tracker.record(Attempt(provider=exc.provider, role=role, ok=False))
            raise

        self.tracker.record(
            Attempt(
                provider=result.provider or "unknown",
                role=role,
                ok=True,
                usage=result.usage,
                latency_ms=result.latency_ms,
            )
        )
        return result
