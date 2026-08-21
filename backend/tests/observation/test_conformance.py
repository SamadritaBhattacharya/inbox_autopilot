"""The Python funnel against the shared conformance fixtures.

The funnel exists twice — here, and in TypeScript for the bridge extension. Two
implementations of the same pruning logic drift, and the drift is invisible in the worst
way: a bug reproduces on one surface and not the other, and whoever chases it suspects the
agent long before they suspect the surface.

So both are held to the same committed outputs in `fixtures/funnel/expected/`. This side
*generates* them (`scripts/gen_funnel_goldens.py`), which makes this suite a regression test:
it fails when the Python funnel changes and the fixtures were not regenerated deliberately.
The TypeScript suite reads the identical files, which is what makes the two agree.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.observation.funnel.pipeline import ObservationFunnel
from app.observation.raw import PageMeta, RawElement
from app.security.tokenizer import PiiTokenizer
from app.security.vault import SessionPiiVault

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "funnel"
CASES = sorted(p.stem for p in (FIXTURES / "cases").glob("*.json"))


def load(kind: str, name: str) -> dict:
    return json.loads((FIXTURES / kind / f"{name}.json").read_text(encoding="utf-8"))


def run(case: dict) -> dict:
    vault = SessionPiiVault()
    funnel = ObservationFunnel(PiiTokenizer(vault))

    elements = [
        RawElement(
            node_id=e["nodeId"],
            role=e["role"],
            name=e["name"],
            value=e["value"],
            x=e["x"],
            y=e["y"],
            width=e["width"],
            height=e["height"],
            interactive=e["interactive"],
            displayed=e["displayed"],
            paint_order=e["paintOrder"],
            receives_pointer=e["receivesPointer"],
            parent_id=e["parentId"],
            depth=e["depth"],
        )
        for e in case["elements"]
    ]
    m = case["meta"]
    observation, _geometry, _report = funnel.run(
        elements,
        PageMeta(
            context_ref=m["contextRef"],
            title=m["title"],
            viewport_width=m["viewportWidth"],
            viewport_height=m["viewportHeight"],
            scroll_x=m["scrollX"],
            scroll_y=m["scrollY"],
            view=m["view"],
            thread_ref=m["threadRef"],
            unread_count=m["unreadCount"],
            compose_open=m["composeOpen"],
        ),
    )
    return json.loads(observation.model_dump_json(by_alias=True))


def test_the_fixtures_exist():
    """A conformance suite with no cases passes silently and proves nothing."""
    assert CASES, "run `uv run --project backend python scripts/gen_funnel_goldens.py`"


@pytest.mark.parametrize("name", CASES)
def test_matches_the_committed_output(name):
    """Regenerate deliberately, never to make this green.

    A changed golden is a changed observation, which is a changed prompt, which is a changed
    agent. If this fails, decide whether the new output is *better* before accepting it.
    """
    assert run(load("cases", name)) == load("expected", name)


@pytest.mark.parametrize("name", CASES)
def test_no_case_leaks_an_identifier(name):
    """Belt and braces over the fixtures themselves: whatever else the goldens pin, they
    must never pin a leak."""
    tokenizer = PiiTokenizer(SessionPiiVault())
    serialized = json.dumps(load("expected", name))

    assert not tokenizer.contains_pii(serialized)


def test_the_injection_case_still_carries_its_bait():
    """If the fixture stops containing a hostile address, the case stops testing anything —
    and it would pass even more convincingly."""
    raw = json.dumps(load("cases", "injection"))

    assert "attacker@evil.com" in raw
