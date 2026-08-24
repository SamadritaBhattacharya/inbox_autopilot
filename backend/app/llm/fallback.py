"""`FallbackLLMClient` — an ordered chain of providers behind the single `LLMClient` port.

The free tiers this project runs on are limited by **rate and quota, not dollars**. One
provider will hit a cap mid-run; the chain is what keeps the run alive instead of ending
it with a 429.

Two rules do the real work:

**1. Fallback happens between attempts, never inside a retry.** A retry re-sends the same
request to the same provider and the same model. Only when a provider is genuinely done do
we move to the next. Swapping models mid-retry would make a trajectory unreproducible —
the record would say one model and the behaviour would come from another.

**2. Not every failure deserves the same response.** A 429 is worth waiting for; a spent
daily quota is not; our own malformed request deserves neither a retry nor a fallback,
because every provider will reject it identically and burying that behind three sequential
failures turns a five-second bug into a two-minute mystery. The error taxonomy in
`app.llm.base` encodes this, and the chain simply obeys it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel

from app.events.current import note_provider
from app.llm.base import (
    AllProvidersExhausted,
    LLMClient,
    LLMResult,
    Message,
    ProviderError,
)
from app.telemetry.records import Role, Usage

logger = logging.getLogger(__name__)

SleepFn = Callable[[float], Awaitable[None]]

#: Exponential backoff base, in seconds, when a provider does not state `Retry-After`.
_BACKOFF_BASE = 0.5
#: Ceiling per wait. A single turn should not stall for minutes; the next provider is
#: almost always faster than waiting out a long rate limit.
_BACKOFF_CAP = 8.0


@dataclass(frozen=True)
class Attempt:
    """One call to one provider — successful or not.

    Failed attempts are recorded too, deliberately: a rejected call still consumed a
    request against the free-tier allowance, so a metering view that only counts successes
    understates usage exactly when headroom matters most.
    """

    provider: str
    role: Role
    ok: bool
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    error: str | None = None


#: Longest a provider stays benched. A daily cap can report tens of minutes; honouring that
#: verbatim would keep it out of the chain long after a rollover, so the wait is capped and
#: the provider gets another look.
COOLDOWN_CAP = 300.0


def _plain(exc: ProviderError) -> str:
    """The provider's complaint, trimmed to something a person can act on.

    Groq's 429 body is a paragraph of organisation ids and token accounting; the part that
    matters to a human is that the free tier is spent and roughly for how long.
    """
    text = str(exc)
    for marker in (". Need more tokens", ". Please try again", ", Used"):
        text = text.split(marker)[0]
    return text[:200]


class FallbackLLMClient:
    """Try each provider in order. Satisfies `LLMClient`, composed of `LLMClient`s."""

    def __init__(
        self,
        providers: Sequence[LLMClient],
        *,
        max_retries: int = 2,
        sleep: SleepFn | None = None,
        now: Callable[[], float] | None = None,
        on_attempt: Callable[[Attempt], None] | None = None,
        recent_window: int = 100,
    ) -> None:
        if not providers:
            # Failing at wiring time beats failing on the first real turn, when a user is
            # watching and the cause is three layers away.
            raise ValueError("FallbackLLMClient needs at least one provider")
        self._providers = list(providers)
        self._max_retries = max_retries
        # Injected so tests assert on backoff without spending real time.
        self._sleep: SleepFn = sleep or asyncio.sleep
        # Metering is pushed, not polled: this client is long-lived and shared, so it must
        # not accumulate a run's history. The callback hands each attempt to whoever owns
        # step context (a graph node knows the step number; this client never will).
        self._on_attempt = on_attempt
        #: A bounded recent window, for debugging and tests only — NOT the metering record.
        self.attempts: deque[Attempt] = deque(maxlen=recent_window)
        #: provider name -> monotonic time it may be tried again. Empty is the normal state.
        self._cooling_until: dict[str, float] = {}
        self._now = now or time.monotonic

    async def complete(
        self,
        *,
        role: Role,
        messages: Sequence[Message],
        tools: Sequence[type[BaseModel]] | None = None,
    ) -> LLMResult:
        failures: list[ProviderError] = []

        for provider in self._providers:
            if (until := self._cooling_until.get(provider.name, 0.0)) > self._now():
                # It told us how long it would be unavailable. Asking anyway buys a
                # guaranteed 429 and its full round trip on EVERY turn of EVERY run for the
                # next half hour — pure latency, and it drowns the log in warnings that all
                # say the same thing.
                logger.debug(
                    "skipping %s for another %.0fs", provider.name, until - self._now()
                )
                continue
            try:
                result = await self._attempt_provider(
                    provider, role=role, messages=messages, tools=tools
                )
            except ProviderError as exc:
                failures.append(exc)
                if not exc.fall_through:
                    # Our bug, not theirs. Surface it now rather than trying two more
                    # providers that will fail the same way.
                    raise
                self._cool_down(exc)
                logger.warning("provider %s exhausted, falling through: %s", exc.provider, exc)
                note_provider(exc.provider, "exhausted", _plain(exc))
            else:
                # Success clears any cooldown: a daily cap that has rolled over, or a key
                # that was topped up, should not stay benched until a timer we guessed.
                self._cooling_until.pop(provider.name, None)
                if failures:
                    # Only worth saying when something went wrong first — a healthy primary
                    # answering normally is not news.
                    note_provider(provider.name, "serving", "took over after a failure")
                return result

        raise AllProvidersExhausted(failures)

    def _cool_down(self, exc: ProviderError) -> None:
        """Bench a provider for as long as it said, and no longer.

        Only when it actually told us. A provider that failed for an unstated reason gets
        retried next turn — benching on a guess would drop a working provider out of the
        chain over one blip.
        """
        if not exc.retry_after:
            return
        wait = min(float(exc.retry_after), COOLDOWN_CAP)
        self._cooling_until[exc.provider] = self._now() + wait
        logger.info("benching %s for %.0fs (it asked)", exc.provider, wait)
        note_provider(
            exc.provider,
            "benched",
            f"rate-limited; not retrying for about {wait / 60:.0f} minutes",
        )

    async def _attempt_provider(
        self,
        provider: LLMClient,
        *,
        role: Role,
        messages: Sequence[Message],
        tools: Sequence[type[BaseModel]] | None,
    ) -> LLMResult:
        """Call one provider, retrying only what is worth retrying.

        Raises the final `ProviderError` if this provider cannot serve the request; the
        caller decides whether to move on.
        """
        last: ProviderError | None = None

        for retry in range(self._max_retries + 1):
            started = time.monotonic()
            try:
                result = await provider.complete(role=role, messages=messages, tools=tools)
            except ProviderError as exc:
                self._record(exc.provider, role, ok=False, started=started, error=str(exc))
                last = exc
                if not exc.retryable or retry == self._max_retries:
                    raise
                await self._sleep(self._backoff(retry, exc.retry_after))
                continue

            self._record(result.provider, role, ok=True, started=started, usage=result.usage)
            return result

        # Unreachable: the loop either returns or raises. Kept explicit so a future edit
        # to the loop cannot silently fall out of the function returning None.
        raise last or AllProvidersExhausted([])

    @staticmethod
    def _backoff(retry: int, retry_after: float | None) -> float:
        """Honour `Retry-After` verbatim when given; otherwise back off exponentially.

        When a provider states how long to wait, guessing is strictly worse — guess short
        and you get rate-limited again, guess long and you stall a turn for nothing.
        """
        if retry_after is not None:
            return max(0.0, retry_after)
        return min(_BACKOFF_BASE * (2**retry), _BACKOFF_CAP)

    def _record(
        self,
        provider: str,
        role: Role,
        *,
        ok: bool,
        started: float,
        usage: Usage | None = None,
        error: str | None = None,
    ) -> None:
        attempt = Attempt(
            provider=provider or "unknown",
            role=role,
            ok=ok,
            usage=usage or Usage(),
            latency_ms=int((time.monotonic() - started) * 1000),
            error=error,
        )
        self.attempts.append(attempt)
        if self._on_attempt is not None:
            try:
                self._on_attempt(attempt)
            except Exception:
                # Metering must never take down a run. Losing a usage row is a reporting
                # gap; raising here would turn it into a failed task.
                logger.exception("on_attempt callback failed for provider %s", attempt.provider)
