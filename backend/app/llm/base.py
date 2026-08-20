"""The `LLMClient` port and its error taxonomy.

The graph and every service see ONLY what is in this module. No LangChain type, no
provider SDK type, and no provider-shaped dict escapes into a node — that is what keeps
"swap the provider" a one-line change in the composition root instead of a refactor.

The error taxonomy is the interesting part. Fallback logic needs to distinguish three
situations that all look like "the call failed":

  - **retryable on the SAME provider** — a rate limit or a 5xx; waiting helps
  - **fall through to the NEXT provider** — quota gone for the day, bad key; waiting does
    not help, but a different provider will work
  - **do not fall back at all** — we sent a malformed request; every provider will reject
    it identically, and silently trying all three just triples the latency before the same
    failure

Collapsing these into one exception type is how systems end up hammering a dead provider,
or masking their own bug as an outage.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.telemetry.records import Role, Usage

MessageRole = Literal["system", "user", "assistant", "tool"]


class ToolCall(BaseModel):
    """A structured action the model chose. Never free-text parsed."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """A conversation turn, in OUR vocabulary rather than any provider's.

    Deliberately not a LangChain message: the port must not leak framework types, or every
    consumer becomes coupled to the framework we happen to use for plumbing today.
    """

    model_config = ConfigDict(frozen=True)

    role: MessageRole
    content: str = ""
    # An assistant turn that called tools carries them here; the matching tool result
    # echoes the id in `tool_call_id`. Replaying history needs both halves or the provider
    # rejects the conversation as malformed.
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    # Opt-in marker for a stable, cache-marked prefix. Prompt caching is a primary
    # free-tier lever; it only works if the prefix is byte-stable, so it is explicit.
    cacheable: bool = False


class LLMResult(BaseModel):
    """One completion, plus everything the trajectory needs to account for it."""

    model_config = ConfigDict(frozen=True)

    #: The assistant's visible content.
    text: str = ""
    #: The model's chain of thought, when the provider exposes it separately.
    #:
    #: Reasoning models return their thinking in a dedicated field and leave `content`
    #: EMPTY on a tool-calling turn. Reading only `content` there silently discards the
    #: explanation — which would fail think-before-act on every turn and leave the cockpit
    #: with nothing to show. Both are kept: `text` is what the model said, `reasoning` is
    #: how it got there.
    reasoning: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    provider: str = ""
    model: str = ""
    latency_ms: int = 0

    @property
    def explanation(self) -> str:
        """What to show the human and replay into history. Prefers explicit reasoning."""
        return self.reasoning.strip() or self.text.strip()

    @property
    def has_reasoning(self) -> bool:
        """Think-before-act: a tool call with no explanation is rejected upstream."""
        return len(self.explanation) >= 3


# ── errors ──────────────────────────────────────────────────────────────────


class LLMError(Exception):
    """Base for everything this layer raises."""


class ProviderError(LLMError):
    """A single provider failed. Carries enough for the chain to decide what to do next."""

    #: Would waiting and trying THIS provider again plausibly help?
    retryable: bool = False
    #: Should the chain move on to the next provider?
    fall_through: bool = True

    def __init__(self, provider: str, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.retry_after = retry_after


class ProviderRateLimited(ProviderError):
    """429. Retry the same provider honouring Retry-After, then fall through."""

    retryable = True
    fall_through = True


class ProviderUnavailable(ProviderError):
    """5xx / connection failure. Transient — retry, then fall through."""

    retryable = True
    fall_through = True


class ProviderQuotaExhausted(ProviderError):
    """Daily/monthly free-tier cap hit. Retrying is pointless; move on immediately."""

    retryable = False
    fall_through = True


class ProviderAuthError(ProviderError):
    """Missing or rejected key. A configuration problem for THIS provider only."""

    retryable = False
    fall_through = True


class ProviderBadRequest(ProviderError):
    """We built a malformed request. Every provider will reject it the same way.

    Falling through here would hide our own bug behind three sequential failures and a
    misleading PROVIDER_EXHAUSTED at the end.
    """

    retryable = False
    fall_through = False


class AllProvidersExhausted(LLMError):
    """Every provider in the chain failed. Maps to `ErrorCode.PROVIDER_EXHAUSTED`."""

    def __init__(self, failures: Sequence[ProviderError]) -> None:
        detail = "; ".join(str(f) for f in failures) or "no providers configured"
        super().__init__(f"All LLM providers failed -- {detail}")
        self.failures = list(failures)


# ── the port ────────────────────────────────────────────────────────────────


@runtime_checkable
class LLMClient(Protocol):
    """What the graph is allowed to know about language models.

    One method. A consumer that needs streaming, embeddings, or provider metadata wants a
    different port, not a wider one.
    """

    async def complete(
        self,
        *,
        role: Role,
        messages: Sequence[Message],
        tools: Sequence[type[BaseModel]] | None = None,
    ) -> LLMResult: ...
