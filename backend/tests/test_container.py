"""The composition root is the ONLY place concretes are built.

Its contract is narrow: every dependency can be overridden by injection, and nothing it
builds requires a network, a browser, or a key. That is what keeps the graph testable as
it grows — each milestone adds a field and an override, never a branch in a node.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.config.container import AppContainer, build_container
from app.config.settings import Settings
from app.llm.usage import UsageTracker
from app.telemetry.store import InMemoryTrajectoryStore
from tests.fakes.fake_llm import FakeLLMClient, ok

NO_ENV = {"_env_file": None}


def test_builds_with_no_arguments():
    container = build_container()
    assert isinstance(container, AppContainer)
    assert container.settings is not None


def test_injected_settings_win_over_the_default():
    assert build_container(settings=Settings(**NO_ENV, max_steps=7)).settings.max_steps == 7


def test_building_requires_no_key_and_no_network():
    """M1 wiring must stay inert: importing and building must never reach outside."""
    container = build_container(settings=Settings(**NO_ENV))
    assert container.settings.configured_providers() == ()
    assert container.llm is None


def test_container_is_immutable():
    """State belongs in AgentState, not on the container."""
    with pytest.raises(FrozenInstanceError):
        build_container().settings = None  # type: ignore[misc]


# ── the LLM ─────────────────────────────────────────────────────────────────


def test_a_keyed_provider_produces_a_chain():
    container = build_container(settings=Settings(**NO_ENV, groq_api_key="g"))
    assert container.require_llm() is container.llm


def test_missing_configuration_explains_itself():
    """Better than an opaque 401 three nodes into a run."""
    container = build_container(settings=Settings(**NO_ENV))
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        container.require_llm()


def test_an_injected_llm_wins_over_configuration():
    """A test must never accidentally reach a real provider because a key is in .env."""
    fake = FakeLLMClient([ok()])
    container = build_container(settings=Settings(**NO_ENV, groq_api_key="g"), llm=fake)
    assert container.require_llm() is fake


async def test_metering_is_wired_at_construction():
    """There is no later point at which every attempt is still visible."""
    tracker = UsageTracker()
    container = build_container(
        settings=Settings(**NO_ENV, groq_api_key="g"), usage=tracker, llm=None
    )
    # The real chain is built; drive it through a fake provider swapped into the chain.
    container.llm._providers[0] = FakeLLMClient([ok(tokens=11)], name="groq")  # type: ignore[union-attr]

    await container.require_llm().complete(role="executor", messages=[])

    assert tracker.call_count == 1
    assert tracker.totals.input_tokens == 11


# ── per-session security ────────────────────────────────────────────────────


def test_each_session_gets_its_own_vault():
    """A shared vault would make tokens stable across runs — a pseudonym, not a token."""
    container = build_container(settings=Settings(**NO_ENV))
    first, second = container.new_session_security(), container.new_session_security()

    assert first.vault is not second.vault
    first.tokenizer.tokenize("priya@corp.com")
    assert second.vault.size == 0


def test_session_tokenizer_follows_the_name_setting():
    off = build_container(settings=Settings(**NO_ENV, pii_tokenize_names=False))
    assert off.new_session_security().tokenizer.register_person("Priya Nair") is None

    on = build_container(settings=Settings(**NO_ENV, pii_tokenize_names=True))
    assert on.new_session_security().tokenizer.register_person("Priya Nair") == "C1"


def test_addresses_are_tokenized_even_with_names_disabled():
    container = build_container(settings=Settings(**NO_ENV, pii_tokenize_names=False))
    out = container.new_session_security().tokenizer.tokenize("mail priya@corp.com")
    assert "priya@corp.com" not in out


# ── other ports ─────────────────────────────────────────────────────────────


def test_trajectory_store_is_injectable():
    store = InMemoryTrajectoryStore()
    assert build_container(settings=Settings(**NO_ENV), trajectory=store).trajectory is store


def test_redaction_is_installed_at_build():
    import logging

    from app.security.redaction import RedactionFilter

    logging.getLogger().filters.clear()
    build_container(settings=Settings(**NO_ENV))
    assert any(isinstance(f, RedactionFilter) for f in logging.getLogger().filters)
