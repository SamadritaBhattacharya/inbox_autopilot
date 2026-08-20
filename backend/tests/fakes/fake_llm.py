"""A scripted `LLMClient`. No provider, no key, no network.

This is a TEST DOUBLE and must never appear on a real path — the composition root builds
it only when a test injects it. It satisfies exactly the `LLMClient` port, which is what
makes the whole graph testable without a model.
"""
from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from app.llm.base import LLMResult, Message, ProviderError
from app.telemetry.records import Role, Usage


class FakeLLMClient:
    """Returns canned results in order; records every request it was asked to serve.

    `script` entries are either an `LLMResult` (returned) or an exception (raised), so a
    single fake covers both the happy path and every failure mode of the chain.
    """

    def __init__(
        self,
        script: Sequence[LLMResult | BaseException] | None = None,
        *,
        name: str = "fake",
        model: str = "fake-model",
    ) -> None:
        self.name = name
        self.model = model
        self._script: list[LLMResult | BaseException] = list(script or [])
        #: Every call, in order: (role, messages, tools). Assert against this.
        self.requests: list[tuple[Role, list[Message], tuple[str, ...]]] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    async def complete(
        self,
        *,
        role: Role,
        messages: Sequence[Message],
        tools: Sequence[type[BaseModel]] | None = None,
    ) -> LLMResult:
        self.requests.append((role, list(messages), tuple(t.__name__ for t in tools or ())))

        if not self._script:
            # An unscripted call is a test bug, not a provider outage — say so loudly
            # rather than returning a plausible-looking empty result.
            raise AssertionError(
                f"FakeLLMClient({self.name!r}) received an unscripted call #{self.call_count}"
            )

        nxt = self._script.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def ok(text: str = "thinking", *, provider: str = "fake", tokens: int = 10) -> LLMResult:
    """A successful completion, for readable test scripts."""
    return LLMResult(
        text=text,
        provider=provider,
        model=f"{provider}-model",
        usage=Usage(input_tokens=tokens, output_tokens=tokens),
        latency_ms=1,
    )


def boom(kind: type[ProviderError], provider: str = "fake", **kwargs) -> ProviderError:
    """A provider failure, for readable test scripts."""
    return kind(provider, "scripted failure", **kwargs)
