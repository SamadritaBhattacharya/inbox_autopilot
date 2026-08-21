"""The provider adapter — request shape, response parsing, and failure classification.

Every test here is hermetic: `httpx.MockTransport` serves the responses, so nothing
reaches a network and no key is needed.

The classification tests carry the most weight. `FallbackLLMClient` does the right thing
only if it is told the right thing, and the difference between "wait 2 seconds" and "this
provider is done until tomorrow" is invisible in the status code alone.
"""
from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel, Field

from app.llm.base import (
    Message,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderUnavailable,
    ToolCall,
)
from app.llm.openai_compatible import (
    OpenAICompatibleClient,
    message_to_openai,
    tool_to_openai_schema,
)

MESSAGES = [Message(role="user", content="archive the newsletters")]


class Archive(BaseModel):
    """Archive the thread at the given index."""

    index: int = Field(description="The element index to archive")


def completion(
    content: str = "reasoning", tool_calls: list | None = None, usage: dict | None = None
):
    return {
        "choices": [{"message": {"content": content, "tool_calls": tool_calls or []}}],
        "usage": usage or {"prompt_tokens": 120, "completion_tokens": 30},
    }


def client_for(handler, **kwargs) -> OpenAICompatibleClient:
    """An adapter wired to a mock transport. `handler(request) -> httpx.Response`."""
    captured: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    adapter = OpenAICompatibleClient(
        name=kwargs.pop("name", "groq"),
        base_url=kwargs.pop("base_url", "https://api.example/v1"),
        api_key=kwargs.pop("api_key", "test-key"),
        models=kwargs.pop(
            "models",
            {"classifier": "small-model", "executor": "big-model", "validator": "small-model"},
        ),
        http=httpx.AsyncClient(transport=httpx.MockTransport(wrapped)),
        **kwargs,
    )
    adapter.captured = captured  # type: ignore[attr-defined]
    return adapter


def json_response(status: int, payload: dict, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers or {})


# ── request shape ───────────────────────────────────────────────────────────


async def test_sends_the_model_configured_for_the_role():
    adapter = client_for(lambda r: json_response(200, completion()))

    await adapter.complete(role="classifier", messages=MESSAGES)
    assert json.loads(adapter.captured[0].content)["model"] == "small-model"

    await adapter.complete(role="executor", messages=MESSAGES)
    assert json.loads(adapter.captured[1].content)["model"] == "big-model"


async def test_an_unconfigured_role_is_a_wiring_bug_not_a_provider_failure():
    adapter = client_for(lambda r: json_response(200, completion()), models={"executor": "big"})
    with pytest.raises(ProviderBadRequest):
        await adapter.complete(role="validator", messages=MESSAGES)


async def test_authorises_and_targets_the_chat_completions_endpoint():
    adapter = client_for(lambda r: json_response(200, completion()))
    await adapter.complete(role="executor", messages=MESSAGES)

    request = adapter.captured[0]
    assert str(request.url) == "https://api.example/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-key"


async def test_applies_the_configured_generation_limits():
    adapter = client_for(
        lambda r: json_response(200, completion()), temperature=0.7, max_output_tokens=512
    )
    await adapter.complete(role="executor", messages=MESSAGES)

    body = json.loads(adapter.captured[0].content)
    assert body["temperature"] == 0.7
    assert body["max_tokens"] == 512


async def test_extra_headers_are_sent():
    adapter = client_for(
        lambda r: json_response(200, completion()), extra_headers={"X-Title": "Inbox Autopilot"}
    )
    await adapter.complete(role="executor", messages=MESSAGES)
    assert adapter.captured[0].headers["x-title"] == "Inbox Autopilot"


async def test_tools_are_sent_as_schemas_not_prose():
    adapter = client_for(lambda r: json_response(200, completion()))
    await adapter.complete(role="executor", messages=MESSAGES, tools=[Archive])

    body = json.loads(adapter.captured[0].content)
    assert body["tool_choice"] == "auto"
    fn = body["tools"][0]["function"]
    assert fn["name"] == "Archive"
    assert fn["description"] == "Archive the thread at the given index."
    assert "index" in fn["parameters"]["properties"]


async def test_no_tools_key_when_none_are_bound():
    """A worker with no gated verbs must not be handed an empty tool array to reason about."""
    adapter = client_for(lambda r: json_response(200, completion()))
    await adapter.complete(role="executor", messages=MESSAGES)
    assert "tools" not in json.loads(adapter.captured[0].content)


async def test_replays_assistant_tool_calls_and_their_results():
    """Providers reject a tool result with no matching call — as an opaque 400, turns later."""
    adapter = client_for(lambda r: json_response(200, completion()))
    history = [
        Message(role="user", content="clear the noise"),
        Message(
            role="assistant",
            content="Archiving [4].",
            tool_calls=[ToolCall(id="call_1", name="Archive", args={"index": 4})],
        ),
        Message(role="tool", content="archived", tool_call_id="call_1", name="Archive"),
    ]

    await adapter.complete(role="executor", messages=history)

    sent = json.loads(adapter.captured[0].content)["messages"]
    assert sent[1]["tool_calls"][0]["id"] == "call_1"
    assert json.loads(sent[1]["tool_calls"][0]["function"]["arguments"]) == {"index": 4}
    assert sent[2]["tool_call_id"] == "call_1"


# ── response parsing ────────────────────────────────────────────────────────


async def test_parses_text_tool_calls_and_usage():
    payload = completion(
        content="I'll archive it.",
        tool_calls=[
            {"id": "call_9", "function": {"name": "Archive", "arguments": '{"index": 12}'}}
        ],
        usage={
            "prompt_tokens": 800,
            "completion_tokens": 42,
            "prompt_tokens_details": {"cached_tokens": 640},
        },
    )
    adapter = client_for(lambda r: json_response(200, payload))

    result = await adapter.complete(role="executor", messages=MESSAGES)

    assert result.text == "I'll archive it."
    assert result.tool_calls == [ToolCall(id="call_9", name="Archive", args={"index": 12})]
    assert result.usage.input_tokens == 800
    assert result.usage.cached_tokens == 640, "cache hits must be visible to be tuned"
    assert result.provider == "groq"
    assert result.model == "big-model"


async def test_unparseable_tool_arguments_keep_the_call():
    """Dropping it would read as 'the model did nothing' and trip the wrong guard."""
    payload = completion(
        tool_calls=[{"id": "c1", "function": {"name": "Archive", "arguments": "{not json"}}]
    )
    adapter = client_for(lambda r: json_response(200, payload))

    result = await adapter.complete(role="executor", messages=MESSAGES)
    assert result.tool_calls[0].name == "Archive"
    assert result.tool_calls[0].args == {}


async def test_null_content_becomes_empty_text():
    """A pure tool-call turn has `content: null`; think-before-act judges it, not a crash."""
    payload = {"choices": [{"message": {"content": None}}]}
    adapter = client_for(lambda r: json_response(200, payload))
    result = await adapter.complete(role="executor", messages=MESSAGES)
    assert result.text == ""
    assert result.has_reasoning is False


# ── reasoning models ────────────────────────────────────────────────────────
#
# These return their thinking in a dedicated field and leave `content` EMPTY on a
# tool-calling turn. A client reading only `content` concludes the model failed to explain
# itself and fails think-before-act on every single turn — while the explanation sits right
# there under another key. Observed live on Groq's gpt-oss models.


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content"])
async def test_reasoning_is_read_from_either_field(field):
    payload = {
        "choices": [
            {
                "message": {
                    "content": None,
                    field: "The sender is a shop and this is a newsletter, so archive it.",
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "Archive", "arguments": '{"index": 7}'}}
                    ],
                }
            }
        ]
    }
    adapter = client_for(lambda r: json_response(200, payload))

    result = await adapter.complete(role="executor", messages=MESSAGES, tools=[Archive])

    assert result.text == "", "these models genuinely return no content"
    assert result.reasoning.startswith("The sender is a shop")
    assert result.has_reasoning is True, "think-before-act must see the explanation"
    assert result.explanation == result.reasoning
    assert result.tool_calls[0].name == "Archive"


async def test_visible_content_is_preferred_when_both_are_present():
    payload = completion(content="Archiving the newsletter.")
    payload["choices"][0]["message"]["reasoning"] = "internal deliberation"
    adapter = client_for(lambda r: json_response(200, payload))

    result = await adapter.complete(role="executor", messages=MESSAGES)

    assert result.text == "Archiving the newsletter."
    assert result.reasoning == "internal deliberation"


async def test_blank_reasoning_does_not_satisfy_think_before_act():
    payload = {"choices": [{"message": {"content": None, "reasoning": "   "}}]}
    adapter = client_for(lambda r: json_response(200, payload))
    assert (await adapter.complete(role="executor", messages=MESSAGES)).has_reasoning is False


async def test_a_200_we_cannot_read_is_the_providers_problem():
    adapter = client_for(lambda r: json_response(200, {"unexpected": "shape"}))
    with pytest.raises(ProviderUnavailable):
        await adapter.complete(role="executor", messages=MESSAGES)


# ── failure classification ──────────────────────────────────────────────────


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failures(status):
    adapter = client_for(lambda r: json_response(status, {"error": {"message": "bad key"}}))
    with pytest.raises(ProviderAuthError) as exc:
        await adapter.complete(role="executor", messages=MESSAGES)
    assert exc.value.retryable is False
    assert exc.value.fall_through is True


async def test_rate_limit_is_retryable_and_carries_retry_after():
    adapter = client_for(
        lambda r: json_response(
            429, {"error": {"message": "Rate limit reached for requests"}}, {"retry-after": "3"}
        )
    )
    with pytest.raises(ProviderRateLimited) as exc:
        await adapter.complete(role="executor", messages=MESSAGES)
    assert exc.value.retryable is True
    assert exc.value.retry_after == 3.0


@pytest.mark.parametrize(
    "message",
    [
        "You exceeded your current quota",
        "free-models-per-day limit reached",
        "Rate limit exceeded: 50 per day",
        "Insufficient credits",
    ],
)
async def test_a_spent_allowance_is_not_treated_as_a_burst(message):
    """A daily cap will not clear by waiting. Retrying it just delays the fallback."""
    adapter = client_for(lambda r: json_response(429, {"error": {"message": message}}))
    with pytest.raises(ProviderQuotaExhausted) as exc:
        await adapter.complete(role="executor", messages=MESSAGES)
    assert exc.value.retryable is False


async def test_payment_required_is_quota_exhaustion():
    adapter = client_for(lambda r: json_response(402, {"error": {"message": "out of credits"}}))
    with pytest.raises(ProviderQuotaExhausted):
        await adapter.complete(role="executor", messages=MESSAGES)


@pytest.mark.parametrize("status", [400, 404, 422])
async def test_our_malformed_request_stops_the_chain(status):
    adapter = client_for(lambda r: json_response(status, {"error": {"message": "bad model"}}))
    with pytest.raises(ProviderBadRequest) as exc:
        await adapter.complete(role="executor", messages=MESSAGES)
    assert exc.value.fall_through is False, "our bug must not be buried under two more failures"


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_server_errors_are_transient(status):
    adapter = client_for(lambda r: json_response(status, {"error": "upstream exploded"}))
    with pytest.raises(ProviderUnavailable) as exc:
        await adapter.complete(role="executor", messages=MESSAGES)
    assert exc.value.retryable is True


async def test_timeouts_are_transient():
    def timeout(_request):
        raise httpx.ReadTimeout("too slow")

    with pytest.raises(ProviderUnavailable, match="timed out"):
        await client_for(timeout).complete(role="executor", messages=MESSAGES)


async def test_connection_failures_are_transient():
    def refused(_request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderUnavailable, match="transport error"):
        await client_for(refused).complete(role="executor", messages=MESSAGES)


async def test_a_non_json_error_body_still_classifies():
    """Gateways return HTML error pages. That must not become an unhandled exception."""
    adapter = client_for(lambda r: httpx.Response(502, text="<html>Bad Gateway</html>"))
    with pytest.raises(ProviderUnavailable):
        await adapter.complete(role="executor", messages=MESSAGES)


# ── tool schema helper ──────────────────────────────────────────────────────


def test_tool_schema_shape():
    schema = tool_to_openai_schema(Archive)
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "Archive"
    assert "title" not in schema["function"]["parameters"]


# ── tool results must name their function ───────────────────────────────────


class TestToolResultNaming:
    """Gemini is reached through its OpenAI-compatible shim, which translates a `tool`
    message into a Gemini `function_response` — where `name` is REQUIRED. OpenAI does not
    ask for it, so the omission was invisible on Groq and OpenRouter and killed every run
    that fell through to Gemini with:

        GenerateContentRequest.contents[3].parts[0].function_response.name:
        Name cannot be empty

    An empty name fails the whole request; a merely imprecise one still replays. So this
    always emits something.
    """

    def test_a_tool_result_carries_a_name(self):
        payload = message_to_openai(
            Message(role="tool", content="archived", tool_call_id="Archive")
        )

        assert payload["name"] == "Archive"

    def test_an_explicit_name_wins(self):
        payload = message_to_openai(
            Message(role="tool", content="ok", tool_call_id="call_7", name="Archive")
        )

        assert payload["name"] == "Archive"

    def test_a_tool_result_with_nothing_to_go_on_still_names_something(self):
        """Never empty. An imprecise name replays; an empty one 400s the request."""
        payload = message_to_openai(Message(role="tool", content="ok"))

        assert payload["name"], "a tool result must never carry an empty name"

    def test_ordinary_messages_gain_no_name(self):
        """The fallback is scoped to tool results. A `name` on a user turn changes how some
        providers attribute the message."""
        for role in ("user", "assistant", "system"):
            payload = message_to_openai(Message(role=role, content="hello"))

            assert "name" not in payload, f"{role} message should carry no name"

    def test_a_tool_call_never_replays_with_an_empty_id(self):
        """Gemini does not return OpenAI-style call ids, so replaying its own reply back to
        it would send `"id": ""` — the same empty-required-field 400, one field over."""
        payload = message_to_openai(
            Message(
                role="assistant",
                tool_calls=[ToolCall(id="", name="Archive", args={"index": 3})],
            )
        )

        assert payload["tool_calls"][0]["id"] == "Archive"

    def test_a_real_call_id_is_preserved(self):
        payload = message_to_openai(
            Message(
                role="assistant",
                tool_calls=[ToolCall(id="call_abc", name="Archive", args={})],
            )
        )

        assert payload["tool_calls"][0]["id"] == "call_abc"
