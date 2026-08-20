"""The fallback chain — free-tier survival, and the rules that keep it honest.

The guardrail under test throughout: **fallback happens BETWEEN attempts, never inside a
retry.** A retry re-sends the same request to the same provider and the same model. Only
once a provider is genuinely done do we move to the next one. Silently swapping models
mid-retry would make a run's behaviour unreproducible and its trajectory a lie.
"""
from __future__ import annotations

import pytest

from app.llm.base import (
    AllProvidersExhausted,
    Message,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderUnavailable,
)
from app.llm.fallback import FallbackLLMClient
from tests.fakes.fake_llm import FakeLLMClient, boom, ok

MESSAGES = [Message(role="user", content="archive the newsletters")]


class RecordingSleep:
    """Captures backoff delays instead of actually waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


async def complete(chain: FallbackLLMClient):
    return await chain.complete(role="executor", messages=MESSAGES)


# ── the happy path ──────────────────────────────────────────────────────────


async def test_first_healthy_provider_wins_and_others_are_untouched():
    primary = FakeLLMClient([ok("primary", provider="groq")], name="groq")
    backup = FakeLLMClient([ok("backup")], name="openrouter")

    result = await complete(FallbackLLMClient([primary, backup], sleep=RecordingSleep()))

    assert result.text == "primary"
    assert primary.call_count == 1
    assert backup.call_count == 0, "a healthy primary must not cost a second provider call"


async def test_empty_chain_is_rejected_at_construction():
    """Failing at wiring time beats failing on the first real turn."""
    with pytest.raises(ValueError, match="at least one provider"):
        FallbackLLMClient([])


# ── retry stays on the SAME provider ────────────────────────────────────────


async def test_transient_failure_retries_the_same_provider_before_falling_through():
    primary = FakeLLMClient(
        [boom(ProviderUnavailable, "groq"), ok("recovered", provider="groq")], name="groq"
    )
    backup = FakeLLMClient([ok("backup")], name="openrouter")

    chain = FallbackLLMClient([primary, backup], max_retries=2, sleep=RecordingSleep())
    result = await complete(chain)

    assert result.text == "recovered"
    assert primary.call_count == 2, "the retry must go to the SAME provider"
    assert backup.call_count == 0


async def test_retries_are_bounded_then_the_chain_falls_through():
    primary = FakeLLMClient([boom(ProviderUnavailable, "groq")] * 5, name="groq")
    backup = FakeLLMClient([ok("backup", provider="openrouter")], name="openrouter")

    chain = FallbackLLMClient([primary, backup], max_retries=2, sleep=RecordingSleep())
    result = await complete(chain)

    assert result.text == "backup"
    # 1 initial attempt + 2 retries, and then it gives up on this provider.
    assert primary.call_count == 3


async def test_the_same_request_is_replayed_on_fallback():
    """The next provider must see the identical conversation — no mutation in between."""
    primary = FakeLLMClient([boom(ProviderQuotaExhausted, "groq")], name="groq")
    backup = FakeLLMClient([ok("backup")], name="openrouter")

    await complete(FallbackLLMClient([primary, backup], sleep=RecordingSleep()))

    assert primary.requests[0][1] == backup.requests[0][1] == MESSAGES
    assert primary.requests[0][0] == backup.requests[0][0] == "executor"


# ── which failures retry, which fall through, which stop ────────────────────


async def test_quota_exhaustion_does_not_waste_retries():
    """A daily cap will not clear in 200ms. Move on immediately."""
    primary = FakeLLMClient([boom(ProviderQuotaExhausted, "groq")], name="groq")
    backup = FakeLLMClient([ok("backup")], name="openrouter")
    sleep = RecordingSleep()

    await complete(FallbackLLMClient([primary, backup], max_retries=3, sleep=sleep))

    assert primary.call_count == 1
    assert sleep.delays == [], "no backoff should be spent on a non-retryable failure"


async def test_auth_failure_falls_through_without_retrying():
    """A bad key for one provider must not take the whole run down."""
    primary = FakeLLMClient([boom(ProviderAuthError, "groq")], name="groq")
    backup = FakeLLMClient([ok("backup")], name="openrouter")

    chain = FallbackLLMClient([primary, backup], sleep=RecordingSleep())
    assert (await complete(chain)).text == "backup"
    assert primary.call_count == 1


async def test_our_own_bad_request_does_not_fall_through():
    """A malformed request fails identically everywhere. Surface OUR bug, don't mask it."""
    primary = FakeLLMClient([boom(ProviderBadRequest, "groq")], name="groq")
    backup = FakeLLMClient([ok("backup")], name="openrouter")

    with pytest.raises(ProviderBadRequest):
        await complete(FallbackLLMClient([primary, backup], sleep=RecordingSleep()))
    assert backup.call_count == 0, "trying every provider would just triple the latency"


# ── exhaustion is typed ─────────────────────────────────────────────────────


async def test_all_providers_failing_raises_a_typed_error():
    chain = FallbackLLMClient(
        [
            FakeLLMClient([boom(ProviderQuotaExhausted, "groq")], name="groq"),
            FakeLLMClient([boom(ProviderQuotaExhausted, "openrouter")], name="openrouter"),
            FakeLLMClient([boom(ProviderAuthError, "gemini")], name="gemini"),
        ],
        sleep=RecordingSleep(),
    )

    with pytest.raises(AllProvidersExhausted) as excinfo:
        await complete(chain)

    # Every failure is preserved: "all providers failed" without saying HOW is unactionable.
    assert len(excinfo.value.failures) == 3
    assert {f.provider for f in excinfo.value.failures} == {"groq", "openrouter", "gemini"}


# ── backoff ─────────────────────────────────────────────────────────────────


async def test_retry_after_is_honoured_verbatim():
    """When a provider states how long to wait, guessing is strictly worse."""
    primary = FakeLLMClient(
        [boom(ProviderRateLimited, "groq", retry_after=2.5), ok("recovered")], name="groq"
    )
    sleep = RecordingSleep()

    await complete(FallbackLLMClient([primary], max_retries=2, sleep=sleep))

    assert sleep.delays == [2.5]


async def test_backoff_grows_when_no_retry_after_is_given():
    primary = FakeLLMClient([boom(ProviderUnavailable, "groq")] * 4, name="groq")
    sleep = RecordingSleep()

    with pytest.raises(AllProvidersExhausted):
        await complete(FallbackLLMClient([primary], max_retries=3, sleep=sleep))

    assert len(sleep.delays) == 3
    assert sleep.delays == sorted(sleep.delays), "backoff must not shrink"
    assert sleep.delays[0] < sleep.delays[-1]


# ── metering ────────────────────────────────────────────────────────────────


async def test_every_attempt_is_metered_including_the_failed_ones():
    """Free-tier headroom is the constraint. A failed call still consumed a request."""
    chain = FallbackLLMClient(
        [
            FakeLLMClient([boom(ProviderQuotaExhausted, "groq")], name="groq"),
            FakeLLMClient([ok("backup", provider="openrouter", tokens=7)], name="openrouter"),
        ],
        sleep=RecordingSleep(),
    )

    await complete(chain)

    assert [a.provider for a in chain.attempts] == ["groq", "openrouter"]
    assert [a.ok for a in chain.attempts] == [False, True]
    assert chain.attempts[-1].usage.input_tokens == 7
