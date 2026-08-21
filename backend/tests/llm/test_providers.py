"""Chain construction, and the guardrail that model slugs stay configuration."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.llm.fallback import FallbackLLMClient
from app.llm.providers import (
    BASE_URLS,
    EXTRA_HEADERS,
    PROVIDER_ORDER,
    build_llm_client,
    build_provider,
    models_for,
)

APP_DIR = Path(__file__).resolve().parents[2] / "app"
SETTINGS_FILE = APP_DIR / "config" / "settings.py"


def settings_with(**keys) -> Settings:
    return Settings(_env_file=None, **keys)


# ── chain construction ──────────────────────────────────────────────────────


def test_unkeyed_providers_are_skipped_not_attempted():
    """Including a keyless provider means every run pays a 401 and a hop before working."""
    chain = build_llm_client(settings_with(groq_api_key="g", gemini_api_key="x"))

    assert isinstance(chain, FallbackLLMClient)
    assert [p.name for p in chain._providers] == ["groq", "gemini"]


def test_chain_follows_the_declared_fallback_order():
    """Groq leads: per-step latency compounds across a loop."""
    chain = build_llm_client(
        settings_with(gemini_api_key="x", openrouter_api_key="o", groq_api_key="g")
    )
    assert [p.name for p in chain._providers] == ["groq", "openrouter", "gemini"]


def test_no_configured_provider_fails_at_wiring_time():
    with pytest.raises(ValueError, match="No LLM provider is configured"):
        build_llm_client(settings_with())


def test_retry_budget_comes_from_settings():
    chain = build_llm_client(settings_with(groq_api_key="g", llm_max_retries=5))
    assert chain._max_retries == 5


def test_every_ordered_provider_has_a_base_url():
    assert set(PROVIDER_ORDER) == set(BASE_URLS)


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="Unknown provider"):
        build_provider("anthropic", settings_with(groq_api_key="g"))


# ── per-provider configuration ──────────────────────────────────────────────


@pytest.mark.parametrize("name", PROVIDER_ORDER)
def test_provider_uses_its_own_endpoint_and_the_configured_models(name):
    settings = settings_with(**{f"{name}_api_key": "k"}, llm_model_executor="configured/model")
    provider = build_provider(name, settings)

    assert provider._base_url == BASE_URLS[name].rstrip("/")
    assert provider.model_for("executor") == "configured/model"
    assert provider._api_key == "k"


def test_only_openrouter_sends_attribution_headers():
    assert set(EXTRA_HEADERS) == {"openrouter"}
    assert "HTTP-Referer" in EXTRA_HEADERS["openrouter"]


def test_gemini_is_reached_through_its_openai_compatible_layer():
    """Which is why it needs no adapter of its own."""
    assert BASE_URLS["gemini"].endswith("/openai")


# ── the guardrail ───────────────────────────────────────────────────────────


def test_model_slugs_appear_only_in_settings():
    """A slug baked into code is a time bomb — free-tier rosters rotate without notice.

    Asserts the DEFAULT slugs exist in exactly one place. If a node, an adapter, or a
    prompt ever hardcodes one, this fails and names the file.
    """
    defaults = {
        settings_with().llm_model_classifier,
        settings_with().llm_model_executor,
        settings_with().llm_model_validator,
    }

    offenders: list[str] = []
    for path in APP_DIR.rglob("*.py"):
        if path == SETTINGS_FILE:
            continue
        text = path.read_text(encoding="utf-8")
        offenders += [
            f"{path.relative_to(APP_DIR)} contains model slug {slug!r}"
            for slug in defaults
            if slug in text
        ]

    assert not offenders, "; ".join(offenders)


def test_no_free_tier_model_id_is_pinned_anywhere():
    """OpenRouter's `:free` roster rotates; a pinned id breaks silently later."""
    offenders = [
        str(path.relative_to(APP_DIR))
        for path in APP_DIR.rglob("*.py")
        if ":free" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"pinned :free model id in {offenders}"


# ── model ids are not portable ──────────────────────────────────────────────


class TestPerProviderRosters:
    """One roster for three providers was a latent 404.

    Groq and OpenRouter both host `openai/gpt-oss-120b`, so a single set of slugs worked —
    right up until the chain fell through to Gemini, which 404s a model that never existed
    there. That only happens once the two providers ahead are rate-limited: the failure is
    invisible in testing and arrives at the busiest moment of the longest run.
    """

    def test_a_provider_without_overrides_uses_the_generic_roster(self):
        settings = Settings(_env_file=None, groq_api_key="k")

        assert models_for("groq", settings)["executor"] == settings.llm_model_executor

    def test_a_provider_override_wins(self):
        settings = Settings(
            _env_file=None, gemini_api_key="k", gemini_model_executor="gemini-3.6-flash"
        )

        assert models_for("gemini", settings)["executor"] == "gemini-3.6-flash"

    def test_overrides_are_per_role(self):
        """Overriding the executor must not silently drag the classifier with it."""
        settings = Settings(
            _env_file=None, gemini_api_key="k", gemini_model_executor="gemini-3.6-flash"
        )

        models = models_for("gemini", settings)

        assert models["executor"] == "gemini-3.6-flash"
        assert models["classifier"] == settings.llm_model_classifier

    def test_one_providers_override_does_not_leak_to_another(self):
        settings = Settings(
            _env_file=None,
            groq_api_key="k",
            gemini_api_key="k",
            gemini_model_executor="gemini-3.6-flash",
        )

        assert models_for("groq", settings)["executor"] == settings.llm_model_executor

    def test_a_foreign_slug_is_reported_at_startup(self, caplog):
        """Named at boot, or discovered as a 404 three fallbacks deep hours later."""
        settings = Settings(_env_file=None, groq_api_key="k", gemini_api_key="k")

        with caplog.at_level(logging.WARNING):
            build_llm_client(settings)

        assert "namespaced model ids" in caplog.text

    def test_a_correctly_configured_chain_is_quiet(self, caplog):
        """A warning that fires when nothing is wrong gets filtered out, taking the real
        one with it."""
        settings = Settings(
            _env_file=None,
            groq_api_key="k",
            gemini_api_key="k",
            gemini_model_classifier="gemini-3.5-flash-lite",
            gemini_model_executor="gemini-3.6-flash",
            gemini_model_validator="gemini-3.5-flash-lite",
        )

        with caplog.at_level(logging.WARNING):
            build_llm_client(settings)

        assert "namespaced model ids" not in caplog.text
