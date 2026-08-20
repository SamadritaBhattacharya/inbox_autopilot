"""Metering — every call, every role, successes and failures alike."""
from __future__ import annotations

from app.llm.base import Message, ProviderQuotaExhausted
from app.llm.fallback import Attempt, FallbackLLMClient
from app.llm.usage import UsageTracker
from app.telemetry.records import StepRecord, Usage
from app.telemetry.store import InMemoryTrajectoryStore
from tests.fakes.fake_llm import FakeLLMClient, boom, ok

MESSAGES = [Message(role="user", content="triage the inbox")]


def attempt(provider="groq", role="executor", ok_=True, tokens=10, cached=0, latency=5) -> Attempt:
    return Attempt(
        provider=provider,
        role=role,
        ok=ok_,
        usage=Usage(input_tokens=tokens, output_tokens=tokens, cached_tokens=cached),
        latency_ms=latency,
        error=None if ok_ else "scripted",
    )


# ── accounting ──────────────────────────────────────────────────────────────


def test_failed_attempts_are_counted_too():
    """A rejected call still spent a request against the free-tier allowance."""
    tracker = UsageTracker()
    tracker.record(attempt(ok_=False))
    tracker.record(attempt(provider="openrouter"))

    assert tracker.call_count == 2
    assert tracker.by_provider()["groq"].failures == 1
    assert tracker.by_provider()["groq"].success_rate == 0.0
    assert tracker.by_provider()["openrouter"].success_rate == 1.0


def test_totals_sum_across_providers():
    tracker = UsageTracker()
    tracker.record(attempt(tokens=100))
    tracker.record(attempt(provider="gemini", tokens=50))

    assert tracker.totals.input_tokens == 150
    assert tracker.totals.output_tokens == 150


def test_cached_tokens_are_tracked_separately():
    """Prompt caching is a primary free-tier lever; an invisible lever cannot be tuned."""
    tracker = UsageTracker()
    tracker.record(attempt(tokens=1000, cached=800))
    assert tracker.totals.cached_tokens == 800
    assert "80% cached" in tracker.summary()


def test_roles_are_bucketed_independently():
    """The cheap classifier should carry the volume; that has to be visible."""
    tracker = UsageTracker()
    for _ in range(9):
        tracker.record(attempt(role="classifier", tokens=5))
    tracker.record(attempt(role="executor", tokens=500))

    assert tracker.by_role()["classifier"].calls == 9
    assert tracker.by_role()["executor"].usage.input_tokens == 500


def test_average_latency_is_per_call():
    tracker = UsageTracker()
    tracker.record(attempt(latency=100))
    tracker.record(attempt(latency=300))
    assert tracker.by_provider()["groq"].avg_latency_ms == 200


def test_empty_tracker_reports_cleanly():
    tracker = UsageTracker()
    assert tracker.call_count == 0
    assert tracker.totals == Usage()
    assert "0 calls" in tracker.summary()


# ── step records ────────────────────────────────────────────────────────────


def test_drain_produces_one_record_per_attempt():
    tracker = UsageTracker()
    tracker.record(attempt(ok_=False))
    tracker.record(attempt(provider="openrouter"))

    records = tracker.drain_step_records(step=3, node="reason", worker="triage")

    assert [r.provider for r in records] == ["groq", "openrouter"]
    assert [r.success for r in records] == [False, True]
    assert {r.step for r in records} == {3}
    assert {r.node for r in records} == {"reason"}
    assert {r.worker for r in records} == {"triage"}


def test_draining_twice_does_not_duplicate():
    """Several nodes may share one client; an attempt must be recorded exactly once."""
    tracker = UsageTracker()
    tracker.record(attempt())

    assert len(tracker.drain_step_records(step=1, node="reason")) == 1
    assert tracker.drain_step_records(step=2, node="reason") == []
    # Draining is about the pending queue, not the running totals.
    assert tracker.call_count == 1


# ── wired to the chain ──────────────────────────────────────────────────────


async def test_the_chain_pushes_every_attempt_to_the_tracker():
    tracker = UsageTracker()
    chain = FallbackLLMClient(
        [
            FakeLLMClient([boom(ProviderQuotaExhausted, "groq")], name="groq"),
            FakeLLMClient([ok("done", provider="openrouter", tokens=42)], name="openrouter"),
        ],
        on_attempt=tracker.record,
    )

    await chain.complete(role="executor", messages=MESSAGES)

    assert tracker.call_count == 2, "the failed groq attempt must be metered too"
    assert tracker.totals.input_tokens == 42


async def test_a_broken_meter_never_fails_a_run():
    """Losing a usage row is a reporting gap. Raising would make it a failed task."""

    def explode(_attempt):
        raise RuntimeError("metering backend down")

    chain = FallbackLLMClient([FakeLLMClient([ok("fine")], name="groq")], on_attempt=explode)
    assert (await chain.complete(role="executor", messages=MESSAGES)).text == "fine"


async def test_recent_window_is_bounded():
    """A long-lived shared client must not accumulate a run's history."""
    chain = FallbackLLMClient(
        [FakeLLMClient([ok("x")] * 20, name="groq")], recent_window=5
    )
    for _ in range(20):
        await chain.complete(role="executor", messages=MESSAGES)

    assert len(chain.attempts) == 5


# ── trajectory store ────────────────────────────────────────────────────────


async def test_trajectory_is_ordered_and_keyed_by_thread():
    store = InMemoryTrajectoryStore()
    tracker = UsageTracker()
    tracker.record(attempt())
    tracker.record(attempt(provider="gemini"))

    await store.save_many("run-1", tracker.drain_step_records(step=1, node="reason"))
    await store.save("run-2", StepRecord(step=1, node="observe"))

    assert [r.provider for r in await store.load("run-1")] == ["groq", "gemini"]
    assert [r.node for r in await store.load("run-2")] == ["observe"]
    assert set(store.thread_ids()) == {"run-1", "run-2"}


async def test_unknown_thread_loads_empty():
    assert await InMemoryTrajectoryStore().load("never-ran") == []
