"""One live call per provider, gated on a real key.

Everything else in this suite is hermetic. This exists because a mock transport proves the
adapter handles the response shape we *believe* providers return — it cannot prove that
belief is correct. Exactly one test per provider, checking the things a mock cannot:
the endpoint is real, the configured model slug still exists, and tool-calling works.

    uv run --project backend pytest -m live backend/tests/llm/test_live_smoke.py -v

Skipped automatically without a key, so it never runs in an automated gate.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from app.config.settings import Settings
from app.llm.base import Message
from app.llm.providers import build_provider

pytestmark = pytest.mark.live

SETTINGS = Settings()


class Archive(BaseModel):
    """Archive the email at the given index."""

    index: int = Field(description="The element index to archive")
    reason: str = Field(description="Why this email can be archived")


def requires(provider: str):
    key = getattr(SETTINGS, f"{provider}_api_key").get_secret_value()
    return pytest.mark.skipif(not key, reason=f"no {provider.upper()}_API_KEY configured")


@requires("groq")
async def test_groq_answers_and_meters():
    provider = build_provider("groq", SETTINGS)

    result = await provider.complete(
        role="classifier",
        messages=[Message(role="user", content="Reply with exactly the word: ready")],
    )

    assert "ready" in result.text.lower()
    assert result.provider == "groq"
    assert result.model == SETTINGS.llm_model_classifier
    # Metering must be populated on every call or the free-tier budget is unmeasurable.
    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens > 0
    assert result.latency_ms > 0


@requires("groq")
async def test_groq_returns_a_structured_tool_call():
    """The loop depends on native tool-calling; free-text parsing is not a fallback."""
    provider = build_provider("groq", SETTINGS)

    result = await provider.complete(
        role="executor",
        messages=[
            Message(
                role="system",
                content="You triage email. Explain your reasoning, then call exactly one tool.",
            ),
            Message(
                role="user",
                content="Element [7] is a newsletter from a shop. Archive it.",
            ),
        ],
        tools=[Archive],
    )

    assert result.tool_calls, "no tool call returned"
    call = result.tool_calls[0]
    assert call.name == "Archive"
    assert call.args["index"] == 7
    assert result.has_reasoning, "think-before-act requires text alongside the call"


@requires("openrouter")
async def test_openrouter_answers():
    provider = build_provider("openrouter", SETTINGS)
    result = await provider.complete(
        role="classifier",
        messages=[Message(role="user", content="Reply with exactly the word: ready")],
    )
    assert "ready" in result.text.lower()


@requires("gemini")
async def test_gemini_answers():
    provider = build_provider("gemini", SETTINGS)
    result = await provider.complete(
        role="classifier",
        messages=[Message(role="user", content="Reply with exactly the word: ready")],
    )
    assert "ready" in result.text.lower()
