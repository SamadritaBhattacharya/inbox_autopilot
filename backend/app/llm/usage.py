"""Token and latency accounting across the fallback chain.

The constraint this project runs under is **rate and quota, not dollars**. That makes usage
an operational signal rather than a finance one: "how much headroom is left on Groq before
this run starts falling through to a slower provider" is a question you need answered
during a run, not in a monthly report.

Two things are counted that a naive tracker misses:

- **Failed attempts.** A rejected call still consumed a request against the allowance. A
  tracker that counts only successes understates usage exactly when headroom is tightest.
- **Cached input tokens.** Prompt caching is a primary free-tier lever, and a lever whose
  effect you cannot see is a lever you cannot tune.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.llm.fallback import Attempt
from app.telemetry.records import Role, StepRecord, Usage


@dataclass
class ProviderTotals:
    """Everything worth knowing about one provider's share of a run."""

    calls: int = 0
    failures: int = 0
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0

    @property
    def success_rate(self) -> float:
        return 1.0 if self.calls == 0 else (self.calls - self.failures) / self.calls

    @property
    def avg_latency_ms(self) -> float:
        return 0.0 if self.calls == 0 else self.latency_ms / self.calls


class UsageTracker:
    """Accumulates attempts; converts them into `StepRecord`s on demand.

    Deliberately does NOT write to the trajectory itself. Only a graph node knows which
    step and which node an LLM call belongs to, so the tracker holds attempts until a node
    drains them with `drain_step_records(...)`. Letting this class guess at step numbers
    would put a plausible-but-wrong number into the permanent record.
    """

    def __init__(self) -> None:
        self._pending: list[Attempt] = []
        self._by_provider: dict[str, ProviderTotals] = defaultdict(ProviderTotals)
        self._by_role: dict[Role, ProviderTotals] = defaultdict(ProviderTotals)

    # ── collection ──────────────────────────────────────────────────────────

    def record(self, attempt: Attempt) -> None:
        """Wire this to `FallbackLLMClient(on_attempt=...)`."""
        self._pending.append(attempt)
        for bucket in (self._by_provider[attempt.provider], self._by_role[attempt.role]):
            bucket.calls += 1
            bucket.failures += 0 if attempt.ok else 1
            bucket.usage = bucket.usage + attempt.usage
            bucket.latency_ms += attempt.latency_ms

    # ── reporting ───────────────────────────────────────────────────────────

    def drain_step_records(
        self, *, step: int, node: str, worker: str | None = None
    ) -> list[StepRecord]:
        """Turn everything collected since the last drain into trajectory rows.

        Draining rather than copying means an attempt is recorded exactly once, even when
        several nodes share one client.
        """
        records = [
            StepRecord(
                step=step,
                node=node,
                worker=worker,
                success=attempt.ok,
                provider=attempt.provider,
                role=attempt.role,
                usage=attempt.usage,
                latency_ms=attempt.latency_ms,
            )
            for attempt in self._pending
        ]
        self._pending.clear()
        return records

    @property
    def totals(self) -> Usage:
        total = Usage()
        for bucket in self._by_provider.values():
            total = total + bucket.usage
        return total

    @property
    def call_count(self) -> int:
        return sum(bucket.calls for bucket in self._by_provider.values())

    def by_provider(self) -> dict[str, ProviderTotals]:
        return dict(self._by_provider)

    def by_role(self) -> dict[Role, ProviderTotals]:
        return dict(self._by_role)

    def summary(self) -> str:
        """One line for a run's end. Cache hit-rate is included because tuning needs it."""
        total = self.totals
        cached = total.cached_tokens
        ratio = f" ({100 * cached / total.input_tokens:.0f}% cached)" if total.input_tokens else ""
        providers = ", ".join(
            f"{name}:{b.calls}" + ("" if b.failures == 0 else f"/{b.failures}f")
            for name, b in sorted(self._by_provider.items())
        )
        return (
            f"{self.call_count} calls [{providers}] "
            f"in={total.input_tokens}{ratio} out={total.output_tokens}"
        )
