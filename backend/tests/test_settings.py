"""Settings are configuration, and secrets are secrets.

Two rules get tests here because both are guardrails, not preferences:
  - model slugs come from config (never hardcoded) -> they must be overridable
  - provider keys must not leak through repr/str, which is how they end up in logs
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config.settings import Settings


def test_defaults_are_safe():
    s = Settings(_env_file=None)
    # The fake surface is the default: nothing should touch a real mailbox by accident.
    assert s.email_surface == "fake"
    # Addresses/phones are always tokenized; this flag only widens coverage to names.
    assert s.pii_tokenize_names is True
    assert s.max_steps > 0
    assert 0.0 < s.context_confidence_threshold <= 1.0


def test_model_slugs_are_configuration(monkeypatch):
    monkeypatch.setenv("LLM_MODEL_EXECUTOR", "some/other-model")
    s = Settings(_env_file=None)
    assert s.llm_model_executor == "some/other-model"


def test_provider_keys_do_not_leak_through_repr():
    """A Settings object gets logged eventually. The keys must not ride along."""
    s = Settings(
        _env_file=None,
        groq_api_key="gsk-super-secret",
        openrouter_api_key="sk-or-super-secret",
        gemini_api_key="gm-super-secret",
    )
    blob = f"{s!r} {s} {s.model_dump()}"
    assert "super-secret" not in blob
    # …but the value is still reachable where it is actually needed.
    assert s.groq_api_key.get_secret_value() == "gsk-super-secret"


def test_unknown_email_surface_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, email_surface="imap")


def test_configured_providers_reports_only_keyed_ones():
    s = Settings(_env_file=None, groq_api_key="x", gemini_api_key="y")
    assert s.configured_providers() == ("groq", "gemini")


def test_no_configured_providers_when_unkeyed():
    assert Settings(_env_file=None).configured_providers() == ()
