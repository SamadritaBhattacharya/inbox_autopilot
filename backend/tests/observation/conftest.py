"""Shared builders for funnel tests.

Every stage is tested against synthetic elements rather than a browser: a stage that needs
Chromium to be tested is a stage whose logic is entangled with its input source.
"""
from __future__ import annotations

import pytest

from app.observation.raw import PageMeta, RawElement
from app.security.tokenizer import PiiTokenizer
from app.security.vault import SessionPiiVault

VIEWPORT_W = 1280
VIEWPORT_H = 800


def element(
    node_id: int,
    *,
    role: str = "listitem",
    name: str = "",
    value: str | None = None,
    x: float = 0,
    y: float = 0,
    width: float = 200,
    height: float = 40,
    interactive: bool = False,
    displayed: bool = True,
    paint_order: int = 0,
    receives_pointer: bool | None = None,
    parent_id: int | None = None,
    depth: int = 1,
) -> RawElement:
    return RawElement(
        node_id=node_id,
        role=role,
        name=name,
        value=value,
        x=x,
        y=y,
        width=width,
        height=height,
        interactive=interactive,
        displayed=displayed,
        paint_order=paint_order,
        receives_pointer=receives_pointer,
        parent_id=parent_id,
        depth=depth,
    )


def meta(**overrides) -> PageMeta:
    defaults = {
        "context_ref": "mail/u/0/#inbox",
        "title": "Inbox (12)",
        "viewport_width": VIEWPORT_W,
        "viewport_height": VIEWPORT_H,
        "view": "inbox",
        "unread_count": 12,
    }
    defaults.update(overrides)
    return PageMeta(**defaults)


@pytest.fixture
def vault() -> SessionPiiVault:
    return SessionPiiVault()


@pytest.fixture
def tokenizer(vault: SessionPiiVault) -> PiiTokenizer:
    return PiiTokenizer(vault)
