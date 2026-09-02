"""A turn that chose no action must not re-fire the previous one.

**The live failure.** Asked to open the Sent folder, the agent clicked `[57]`, Sent opened,
and it then produced prose instead of calling `Complete`. The stale `Click(index=57)` was
dispatched a second time — but the page had renumbered when Sent loaded, so 57 was no longer
the Sent link. It was a checkbox, and an unrelated email was silently selected.

It also cost the run its recovery: the re-dispatch consumed the single retry, so the next
empty turn died `NO_ACTION` with the task already finished and a failure card shown for a
success.

`route_after_reason` describes this exact hazard in its own docstring —

    A turn with no `last_action` is the nudge path... Sending it to `act` would dispatch
    whatever action the PREVIOUS turn chose, which is how an agent silently repeats itself.

— and guards it with `if state.last_action is None: return REASON`. **Nothing ever set that
field to None.** The guard read a value nobody cleared, so it could not fire.

That is the second documented guarantee in this codebase found unwired (the first: five
verbs bound to workers the executor refused). A docstring is not an enforcement mechanism,
so both halves are asserted here: the routing rule, and the structural invariant that every
non-terminal path out of `reason` says what it decided.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from inbox_contracts import ActionCall

from app.agent.routing import ACT, APPROVAL, REASON, VERIFY, route_after_reason
from app.agent.state import AgentState

LOOP_SOURCE = Path(__file__).resolve().parents[2] / "app" / "workers" / "loop.py"


def a_state(**overrides) -> AgentState:
    base = dict(task="open the sent folder", thread_id="nudge-1")
    base.update(overrides)
    return AgentState(**base)


# ── the routing rule ───────────────────────────────────────────────────────


def test_a_turn_with_no_action_goes_back_to_reason():
    """THE regression, as the pure function that decides it."""
    assert route_after_reason(a_state(last_action=None)) == REASON


def test_it_never_reaches_act_without_a_fresh_action():
    """`act` dispatches `state.last_action`. Arriving there with a stale one is a silent
    repeat of whatever the previous turn did — against a page that has since renumbered."""
    assert route_after_reason(a_state(last_action=None)) != ACT


def test_a_turn_WITH_an_action_still_dispatches_it():
    """The counterfactual: the fix must not stall the ordinary path."""
    state = a_state(last_action=ActionCall(name="Click", args={"index": 57}))

    assert route_after_reason(state) == ACT


def test_a_gated_action_still_goes_to_the_approval_gate_first():
    """The strongest guarantee in the system runs through this same function."""
    state = a_state(last_action=ActionCall(name="Send", args={"index": 9}))

    assert route_after_reason(state) == APPROVAL


def test_a_terminal_turn_goes_to_verify_whatever_last_action_holds():
    """Terminal wins over both branches, so a stale action cannot resurrect a finished run."""
    state = a_state(
        finished=True, last_action=ActionCall(name="Click", args={"index": 57})
    )

    assert route_after_reason(state) == VERIFY


# ── the structural invariant behind it ─────────────────────────────────────


def _reason_returns() -> list[tuple[int, set[str]]]:
    """Every `return {...}` inside the `reason` node, as (line number, keys)."""
    tree = ast.parse(LOOP_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "reason":
            return [
                (
                    inner.lineno,
                    {k.value for k in inner.value.keys if isinstance(k, ast.Constant)},
                )
                for inner in ast.walk(node)
                if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Dict)
            ]
    raise AssertionError("no `reason` node found in loop.py")


def test_the_reason_node_was_found_at_all():
    """A source-walking test that silently matches nothing is worse than no test."""
    assert len(_reason_returns()) >= 5


@pytest.mark.parametrize("line,keys", _reason_returns())
def test_every_continuing_path_says_what_it_decided(line, keys):
    """Each non-terminal return from `reason` must either choose an action or clear the old
    one. Leaving the field untouched is what let a finished turn re-fire a stale click.

    Terminal returns are exempt: the run is over, and `route_after_reason` sends them to
    `verify` regardless of what the field holds.
    """
    if "finished" in keys:
        return

    assert "last_action" in keys, (
        f"loop.py:{line} continues the loop without setting or clearing `last_action`. "
        "The next turn will dispatch whatever the PREVIOUS turn chose, against a page that "
        "has renumbered since."
    )


def test_the_nudge_path_specifically_clears_it():
    """Named on its own because it is the path that was broken, and the one a future edit
    is most likely to add a sibling to."""
    nudge = [
        keys
        for _, keys in _reason_returns()
        if "nudge_count" in keys and "finished" not in keys
    ]

    assert nudge, "the no-tool-call nudge path disappeared"
    for keys in nudge:
        assert "last_action" in keys
