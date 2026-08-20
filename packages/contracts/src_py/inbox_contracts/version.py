"""The wire protocol version.

Rides on every message. A mismatch between the backend and the cockpit/executor is
rejected loudly at handshake — never coerced.

Bump rules:
  - additive field            -> MINOR
  - removed or retyped field  -> MAJOR (regenerate BOTH sides in the same commit)
"""
from __future__ import annotations

PROTOCOL_VERSION = "1.0.0"
