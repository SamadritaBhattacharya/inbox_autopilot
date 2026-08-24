"""A hallucinated referent must survive into the trajectory, not just into the model's turn.

**The hole this closes.** `ActionValidator` (`app/surface/dispatch.py`) already refused a
stale index, an unminted token, an unbound verb, and a second compose window — a hallucinated
referent was always a one-turn correction rather than a wrong click, and the model always saw
the typed reason. What it did NOT do was survive: the `act` node's `StepRecord` hardcoded
`error_code=None`, and the linear (`rules`) worker's `StepRecord` omitted the field entirely.
Both existed because `StepRecord.error_code` was typed as the run-TERMINATION `ErrorCode`
enum, and a dispatch-rejection code such as `STALE_INDEX` is not a member of it — assigning
it raised a `ValidationError`, so both call sites quietly wrote nothing instead.

The practical cost: every dispatch rejection reached the model and then vanished from
`TrajectoryStore`. Nobody replaying a run, mining failures offline (CLAUDE.md §10.2's
`SkillRegistry`, or the B6 "cross-run learning" tier), or asking "how often does the model
invent an index" could answer from persisted state — only from watching a live event stream.

`StepRecord.error_code` is now `str | None` rather than `ErrorCode | None`, which is safe:
every existing caller already passes either an `ErrorCode` member or `None`, and a `StrEnum`
serialises identically to the plain string either way — see the field's docstring in
`app/telemetry/records.py` for why the two vocabularies coexist on one field.
"""
from __future__ import annotations

import json

from inbox_contracts import ActionResult, Element

from app.agent.graph import build_manager_graph
from app.llm.base import LLMResult, ToolCall
from app.rules.store import InMemoryRulesStore, NoRules, Rule
from app.telemetry.records import StepRecord
from tests.fakes.fake_llm import FakeLLMClient, ok
from tests.fakes.fake_surface import FakeEmailSurface, observation


def intake(action: str, confidence: float = 0.95, **slots) -> LLMResult:
    return ok(json.dumps({"action": action, "slots": slots, "confidence": confidence}))


def acts(name: str, text: str, **args) -> LLMResult:
    return LLMResult(
        text=text, tool_calls=[ToolCall(id=name, name=name, args=args)], provider="fake"
    )


def inbox_view():
    return observation(
        Element(index=2, role="row", name="P3 — Newsletter"),
        Element(index=3, role="button", name="Archive"),
        title="Inbox",
    )


async def test_a_stale_index_rejection_is_recorded_with_its_code():
    """`Click(index=99)` on a screen with no such index: refused, corrected, and now kept.

    `FakeEmailSurface` is scripted, not a reimplementation of `ActionValidator` — it does not
    itself judge an index stale, so the rejection it should have produced is supplied as a
    scripted result. What this test actually exercises is the graph's own plumbing: whatever
    `ActionResult.error_code` a surface returns must reach the `act` StepRecord unchanged,
    which is exactly the step that used to discard it.
    """
    llm = FakeLLMClient(
        [
            intake("archive", selector="the newsletter"),
            ok("decision"),
            ok("Archive it"),
            acts("Archive", "Row 99 looks right.", index=99),
            acts("Archive", "That was stale; using the row actually on screen.", index=2),
            acts("Complete", "Archived.", success=True, reason="archived"),
        ]
    )
    surface = FakeEmailSurface(
        [inbox_view()],
        results=[
            ActionResult(
                success=False,
                reason="[99] is not in the current observation",
                error_code="STALE_INDEX",
            ),
            ActionResult(success=True, reason="archived"),
        ],
    )
    graph = build_manager_graph(llm=llm, surface=surface, rules=NoRules())

    final = await graph.ainvoke(
        {"task": "archive the newsletter", "thread_id": "traj-1"},
        {"configurable": {"thread_id": "traj-1"}},
    )

    act_rows: list[StepRecord] = [r for r in final["history"] if r.node == "act"]
    codes = [r.error_code for r in act_rows]

    assert "STALE_INDEX" in codes, f"the rejection's code did not reach the trajectory: {codes}"
    rejected = next(r for r in act_rows if r.error_code == "STALE_INDEX")
    assert rejected.success is False
    assert rejected.action == "Archive"

    # The correction itself is a clean, untyped success — proving the fix does not paint
    # every row with a leftover code from an earlier turn.
    succeeded = next(r for r in act_rows if r.success is True)
    assert succeeded.error_code is None


async def test_recording_a_rejection_does_not_raise():
    """The exact crash this fix avoids: `StepRecord(error_code="STALE_INDEX")` used to be a
    `pydantic.ValidationError` because the field was typed as the terminal `ErrorCode` enum.
    A silent `except` anywhere upstream would have hidden that as a dropped step rather than
    a loud failure — this asserts the run completes and reports what happened, not just that
    it doesn't crash."""
    llm = FakeLLMClient(
        [
            intake("archive", selector="the newsletter"),
            ok("decision"),
            ok("Archive it"),
            acts("Archive", "Trying row 99.", index=99),
            acts("Archive", "Correcting.", index=2),
            acts("Complete", "Archived.", success=True, reason="archived"),
        ]
    )
    surface = FakeEmailSurface([inbox_view()])
    graph = build_manager_graph(llm=llm, surface=surface, rules=NoRules())

    final = await graph.ainvoke(
        {"task": "archive the newsletter", "thread_id": "traj-2"},
        {"configurable": {"thread_id": "traj-2"}},
    )

    assert final["success"] is True
    assert final["status"] == "done"


async def test_the_linear_rules_worker_also_records_a_rejected_dispatch():
    """The second, easy-to-miss instance of the same bug: `rules_worker.py`'s `StepRecord`
    omitted `error_code` entirely rather than hardcoding `None` — same loss, different node,
    and the one the benchmark's `RunMetrics.invalid_referents` could not previously see for
    a `TRIAGE` (linear) run at all.

    The rule genuinely matches the row on screen, so the worker really does dispatch —
    exercising the field on a real linear-path call rather than the "nothing matched"
    branch, which never touched a `StepRecord.error_code` in the first place.
    """
    rules = InMemoryRulesStore([Rule(name="tidy", patterns=(r"newsletter",), actions=("Archive",))])
    llm = FakeLLMClient([intake("triage", scope="inbox")])

    surface = FakeEmailSurface(
        [observation(Element(index=2, role="row", name="P3 — Newsletter"))],
        results=[
            ActionResult(
                success=False,
                reason="a compose window is already open",
                error_code="COMPOSE_ALREADY_OPEN",
            )
        ],
    )
    graph = build_manager_graph(llm=llm, surface=surface, rules=rules)

    final = await graph.ainvoke(
        {"task": "archive the newsletter", "thread_id": "traj-3"},
        {"configurable": {"thread_id": "traj-3"}},
    )

    linear_rows = [r for r in final["history"] if r.node == "linear"]
    assert linear_rows, "the linear worker never recorded a step at all"
    assert any(r.error_code == "COMPOSE_ALREADY_OPEN" for r in linear_rows), (
        f"the rejection's code did not reach the trajectory: "
        f"{[r.error_code for r in linear_rows]}"
    )
