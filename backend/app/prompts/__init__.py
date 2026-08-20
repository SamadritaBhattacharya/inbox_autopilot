"""Prompts as content, not code.

Every system prompt lives in a `.txt` file next to this module and is loaded by name. Two
reasons, and the second is the one that matters:

1. A multi-line string embedded in Python is awkward to read and worse to diff — a one-word
   wording change shows up as a wall of re-indented text.
2. **Prompts are the part most likely to be edited by someone who is not editing code.**
   Tuning what the agent is told should not require opening the module that decides what it
   does, and a prompt change should never risk a syntax error in the loop.

Plain text rather than a template engine: none of these interpolate anything. State reaches
the model as separate messages — the observation, the nudges, the feedback — which keeps the
system prompt byte-stable, and a byte-stable prefix is what makes prompt caching work at all.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent


@lru_cache
def load_prompt(name: str) -> str:
    """Load `<name>.txt`. Cached: the content never changes at runtime."""
    path = _DIR / f"{name}.txt"
    if not path.is_file():
        raise FileNotFoundError(f"no prompt named {name!r} in {_DIR}")
    return path.read_text(encoding="utf-8").strip()


def available() -> list[str]:
    return sorted(p.stem for p in _DIR.glob("*.txt"))
