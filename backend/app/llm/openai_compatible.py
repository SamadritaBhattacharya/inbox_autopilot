"""One adapter for every provider in the chain.

Groq, OpenRouter, and Gemini all expose an OpenAI-compatible `/chat/completions`. Writing
three near-identical clients would triple the surface where a subtle difference in error
handling could hide; instead there is one implementation and three configurations.

**Why raw `httpx` rather than a framework client.** The error taxonomy in `base.py` is the
load-bearing part of this layer — the chain's whole behaviour depends on correctly telling
"wait and retry" from "move on" from "this is our bug". Framework clients normalise HTTP
errors into their own hierarchy and add their own retry loop, which would both blur that
distinction and fight `FallbackLLMClient` for control of retries. Owning the request means
owning the classification. It also keeps the dependency tree small and makes every test
here hermetic through `httpx.MockTransport`.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping, Sequence

import httpx
from pydantic import BaseModel

from app.llm.base import (
    LLMResult,
    Message,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderUnavailable,
    ToolCall,
)
from app.telemetry.records import Role, Usage

logger = logging.getLogger(__name__)

# A 429 means "too many requests" for both a per-minute burst and a spent daily allowance,
# and the status code alone cannot tell them apart. The distinction matters: one clears in
# seconds, the other not until tomorrow. These markers are a heuristic over the response
# body — wrong occasionally, and the cost of being wrong is only a wasted retry.
_QUOTA_MARKERS = (
    "quota", "daily", "per day", "credits", "billing", "insufficient",
    "exceeded your current", "free-models-per-day",
)


def tool_to_openai_schema(spec: type[BaseModel]) -> dict:
    """A Pydantic tool spec rendered as an OpenAI function tool.

    The model must choose actions through a schema, never free text — that is what makes an
    action validated and observable rather than parsed and hoped for.
    """
    schema = spec.model_json_schema()
    schema.pop("title", None)
    description = (spec.__doc__ or "").strip().splitlines()
    return {
        "type": "function",
        "function": {
            "name": spec.__name__,
            "description": description[0] if description else "",
            "parameters": schema,
        },
    }


def message_to_openai(message: Message) -> dict:
    """Our `Message` in provider vocabulary.

    An assistant turn that called tools must replay WITH those tool calls, and each tool
    result must echo its id — providers reject a conversation where a tool result has no
    matching call, and the failure surfaces as an opaque 400 several turns later.
    """
    payload: dict = {"role": message.role, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.args)},
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.name:
        payload["name"] = message.name
    return payload


class OpenAICompatibleClient:
    """An `LLMClient` over any OpenAI-shaped `/chat/completions` endpoint."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        models: Mapping[Role, str],
        http: httpx.AsyncClient | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 2000,
        timeout: float = 45.0,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._models = dict(models)
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._timeout = timeout
        self._extra_headers = dict(extra_headers or {})
        # Injected in tests via MockTransport; owned here otherwise.
        self._http = http or httpx.AsyncClient(timeout=timeout)

    def model_for(self, role: Role) -> str:
        try:
            return self._models[role]
        except KeyError as exc:  # a wiring mistake, not a runtime condition
            raise ProviderBadRequest(self.name, f"no model configured for role {role!r}") from exc

    async def complete(
        self,
        *,
        role: Role,
        messages: Sequence[Message],
        tools: Sequence[type[BaseModel]] | None = None,
    ) -> LLMResult:
        model = self.model_for(role)
        body: dict = {
            "model": model,
            "messages": [message_to_openai(m) for m in messages],
            "temperature": self._temperature,
            "max_tokens": self._max_output_tokens,
        }
        if tools:
            body["tools"] = [tool_to_openai_schema(t) for t in tools]
            body["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            **self._extra_headers,
        }

        started = time.monotonic()
        try:
            response = await self._http.post(
                f"{self._base_url}/chat/completions",
                json=body,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(
                self.name, f"request timed out after {self._timeout}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(self.name, f"transport error: {exc}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)

        if response.status_code >= 400:
            raise self._classify(response)

        return self._parse(response, model=model, latency_ms=latency_ms)

    # ── failure classification ──────────────────────────────────────────────

    def _classify(self, response: httpx.Response) -> ProviderError:
        status = response.status_code
        detail = self._error_detail(response)
        lowered = detail.lower()

        if status in (401, 403):
            return ProviderAuthError(self.name, f"auth rejected ({status}): {detail}")

        # 402 is OpenRouter's "out of credits" — unambiguous, and not worth retrying.
        if status == 402:
            return ProviderQuotaExhausted(self.name, f"payment required: {detail}")

        if status == 429:
            if any(marker in lowered for marker in _QUOTA_MARKERS):
                return ProviderQuotaExhausted(self.name, f"quota exhausted: {detail}")
            return ProviderRateLimited(
                self.name, f"rate limited: {detail}", retry_after=self._retry_after(response)
            )

        if status in (400, 404, 422):
            # Our request, our bug. Do not let the chain bury it under two more failures.
            return ProviderBadRequest(self.name, f"rejected ({status}): {detail}")

        return ProviderUnavailable(self.name, f"server error ({status}): {detail}")

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            # The HTTP-date form is legal but rare here; a guessed backoff beats crashing.
            return None

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            return response.text[:300]
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or error)[:300]
            if error:
                return str(error)[:300]
        return str(payload)[:300]

    # ── response parsing ────────────────────────────────────────────────────

    def _parse(self, response: httpx.Response, *, model: str, latency_ms: int) -> LLMResult:
        try:
            payload = response.json()
            choice = payload["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            # A 200 we cannot read is the provider's problem, not a malformed request of
            # ours — retryable, then fall through.
            raise ProviderUnavailable(self.name, f"unreadable response: {exc}") from exc

        return LLMResult(
            text=choice.get("content") or "",
            reasoning=self._parse_reasoning(choice),
            tool_calls=self._parse_tool_calls(choice.get("tool_calls") or []),
            usage=self._parse_usage(payload.get("usage") or {}),
            provider=self.name,
            model=model,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _parse_reasoning(message: dict) -> str:
        """Pull the chain of thought out of whichever field this provider uses.

        There is no standard here. Groq's gpt-oss models use `reasoning`; several models
        routed through OpenRouter use `reasoning_content`. On a tool-calling turn these
        models return NO `content` whatsoever, so a client that reads only `content` sees
        an empty response and concludes the model failed to explain itself — when in fact
        the explanation was right there under a different key.
        """
        for field in ("reasoning", "reasoning_content"):
            value = message.get(field)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    def _parse_tool_calls(self, raw: list) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for item in raw:
            fn = item.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                # A model can emit invalid JSON arguments. Dropping the call would look
                # like "the model did nothing" and trip the no-tool-call nudge for the
                # wrong reason, so keep the call and let the dispatcher reject it with a
                # precise error the recovery layer can actually classify.
                logger.warning(
                    "%s returned unparseable tool arguments for %s", self.name, fn.get("name")
                )
                args = {}
            calls.append(ToolCall(id=item.get("id") or "", name=fn.get("name") or "", args=args))
        return calls

    @staticmethod
    def _parse_usage(raw: dict) -> Usage:
        details = raw.get("prompt_tokens_details") or {}
        return Usage(
            input_tokens=int(raw.get("prompt_tokens") or 0),
            output_tokens=int(raw.get("completion_tokens") or 0),
            cached_tokens=int(details.get("cached_tokens") or 0),
        )

    async def aclose(self) -> None:
        await self._http.aclose()
