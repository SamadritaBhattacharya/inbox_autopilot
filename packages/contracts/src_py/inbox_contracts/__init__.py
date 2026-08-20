"""Inbox Autopilot wire contracts — the single source of truth.

Pydantic v2 here -> JSON Schema -> Zod/TS. Never hand-edit the generated side.
"""
from __future__ import annotations

from .models import (
    WIRE_MODELS,
    ActionCall,
    ActionResult,
    Element,
    Envelope,
    MailContext,
    MailView,
    Observation,
    Viewport,
)
from .version import PROTOCOL_VERSION

__all__ = [
    "PROTOCOL_VERSION",
    "WIRE_MODELS",
    "ActionCall",
    "ActionResult",
    "Element",
    "Envelope",
    "MailContext",
    "MailView",
    "Observation",
    "Viewport",
]
