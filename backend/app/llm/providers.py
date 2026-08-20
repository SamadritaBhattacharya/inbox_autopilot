"""Provider configurations and the chain builder.

Three providers, one adapter. What differs between them is a base URL, a header, and a
model roster — so that is all that lives here.

**No model slug appears in this file.** They come from settings, every one of them. Free
tiers rotate their rosters; a slug baked into code is a time bomb that fires on someone
else's schedule, at which point the failure surfaces as an opaque 404 from a provider
rather than as "your config is stale".
"""
from __future__ import annotations

import logging
from collections.abc import Callable

import httpx

from app.config.settings import Settings
from app.llm.base import LLMClient
from app.llm.fallback import Attempt, FallbackLLMClient
from app.llm.openai_compatible import OpenAICompatibleClient
from app.telemetry.records import Role

logger = logging.getLogger(__name__)

#: Ordered by free-tier throughput. Groq leads because per-step latency compounds across
#: a loop — a slower primary costs multiples of its own latency over a full run.
PROVIDER_ORDER: tuple[str, ...] = ("groq", "openrouter", "gemini")

BASE_URLS: dict[str, str] = {
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    # Gemini's OpenAI-compatibility layer, so it needs no separate adapter.
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}

#: OpenRouter attributes traffic by these; they are courtesy metadata, not credentials.
EXTRA_HEADERS: dict[str, dict[str, str]] = {
    "openrouter": {
        "HTTP-Referer": "https://github.com/inbox-autopilot",
        "X-Title": "Inbox Autopilot",
    },
}


def models_from_settings(settings: Settings) -> dict[Role, str]:
    return {
        "classifier": settings.llm_model_classifier,
        "executor": settings.llm_model_executor,
        "validator": settings.llm_model_validator,
    }


def build_provider(
    name: str,
    settings: Settings,
    *,
    http: httpx.AsyncClient | None = None,
) -> OpenAICompatibleClient:
    if name not in BASE_URLS:
        raise ValueError(f"Unknown provider {name!r}; expected one of {tuple(BASE_URLS)}")

    key = getattr(settings, f"{name}_api_key").get_secret_value()
    return OpenAICompatibleClient(
        name=name,
        base_url=BASE_URLS[name],
        api_key=key,
        models=models_from_settings(settings),
        http=http,
        temperature=settings.llm_temperature,
        max_output_tokens=settings.llm_max_output_tokens,
        timeout=settings.llm_request_timeout,
        extra_headers=EXTRA_HEADERS.get(name),
    )


def build_llm_client(
    settings: Settings,
    *,
    http: httpx.AsyncClient | None = None,
    on_attempt: Callable[[Attempt], None] | None = None,
) -> LLMClient:
    """The gateway: every keyed provider, in fallback order, behind one port.

    Providers without a key are **skipped, not attempted**. Building a chain that includes
    a keyless provider means every run pays a guaranteed 401 and a fallback hop before
    doing any work — a startup-time misconfiguration disguised as a runtime failure.
    """
    configured = settings.configured_providers()
    if not configured:
        raise ValueError(
            "No LLM provider is configured. Set at least one of GROQ_API_KEY, "
            "OPENROUTER_API_KEY, or GEMINI_API_KEY in .env (server-side only)."
        )

    ordered = [name for name in PROVIDER_ORDER if name in configured]
    logger.info("LLM fallback chain: %s", " -> ".join(ordered))

    return FallbackLLMClient(
        [build_provider(name, settings, http=http) for name in ordered],
        max_retries=settings.llm_max_retries,
        on_attempt=on_attempt,
    )
