"""The composition root is the ONLY place concretes are built.

Its contract is narrow: every dependency can be overridden by injection, and nothing it
builds requires a network, a browser, or a key. That is what keeps the graph testable as
it grows — each milestone adds a field and an override, never a branch in a node.
"""
from __future__ import annotations

from app.config.container import AppContainer, build_container
from app.config.settings import Settings


def test_builds_with_no_arguments():
    container = build_container()
    assert isinstance(container, AppContainer)
    assert container.settings is not None


def test_injected_settings_win_over_the_default():
    injected = Settings(_env_file=None, max_steps=7)
    assert build_container(settings=injected).settings.max_steps == 7


def test_building_requires_no_key_and_no_network():
    """M0 wiring must stay inert: importing and building must never reach outside."""
    container = build_container(settings=Settings(_env_file=None))
    assert container.settings.configured_providers() == ()


def test_container_is_immutable():
    """State belongs in AgentState, not on the container."""
    container = build_container()
    try:
        container.settings = None  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("AppContainer must be frozen")
