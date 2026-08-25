"""The three ends that were dangling — docs/IMPROVEMENT-PLAN.md §B7.

`app/feedback/` was always most of the way built: four kinds, a promotion threshold, a
written suggestion string, mid-run corrections already reaching the loop. What it lacked was
anyone on the other end of three specific wires:

  1. `ENDORSEMENT` was never constructed anywhere, so the system could only ever learn from
     complaints and never from what it got right.
  2. `RuleCandidate` was computed and read by nothing — the promotion path stopped one call
     short of a human seeing it.
  3. Nothing recorded a verdict on a whole RUN, so the only measure of success was the
     agent's own `Complete(success=True)`.

These pin all three, plus the two things that must NOT happen as a result: a resolved draft
must never reach the feedback store, and a run rating must never be replayed to the model as
if it were an instruction.
"""
from __future__ import annotations

import json

import pytest
from inbox_contracts import Element
from langgraph.types import Command

from app.agent.graph import build_manager_graph
from app.feedback.models import Feedback, FeedbackKind
from app.feedback.store import InMemoryFeedbackStore
from app.llm.base import LLMResult, ToolCall
from app.rules.store import NoRules
from tests.fakes.fake_llm import FakeLLMClient, drafted, ok
from tests.fakes.fake_surface import FakeEmailSurface, observation

DRAFT = "To:      Priya Nair <priya.nair@corp.com>\nSubject: Friday demo\n\nIt moved to 4pm."


def intake(action: str, **slots) -> LLMResult:
    return ok(json.dumps({"action": action, "slots": slots, "confidence": 0.95}))


def acts(name: str, text: str, **args) -> LLMResult:
    return LLMResult(
        text=text, tool_calls=[ToolCall(id=name, name=name, args=args)], provider="fake"
    )


def compose_llm(extra: list | None = None) -> FakeLLMClient:
    return FakeLLMClient(
        [
            intake("send_email", recipient_identity="P1", topic="the Friday demo"),
            ok("decision"),
            ok("Open compose\nSend"),
            drafted(),
            acts("Send", "The draft is ready.", index=9),
            *(extra or []),
        ]
    )


def compose_surface() -> FakeEmailSurface:
    return FakeEmailSurface(
        [observation(Element(index=9, role="button", name="Send"), compose_open=True)],
        preview=DRAFT,
    )


async def run_to_approval(feedback, llm, thread: str):
    surface = compose_surface()
    graph = build_manager_graph(
        llm=llm, surface=surface, rules=NoRules(), feedback=feedback, max_steps=12
    )
    config = {"configurable": {"thread_id": thread}}
    await graph.ainvoke({"task": "email P1 about the demo", "thread_id": thread}, config)
    return graph, config, surface


# ── end 1: the approval gate now produces feedback ──────────────────────────


async def test_approving_records_an_endorsement():
    """The signal that never existed: what the agent got RIGHT."""
    store = InMemoryFeedbackStore()
    llm = compose_llm([acts("Complete", "Sent.", success=True, reason="sent")])
    graph, config, _ = await run_to_approval(store, llm, "fb-1")

    await graph.ainvoke(Command(resume={"verdict": "approve"}), config)

    kinds = [f.kind for f in await store.for_thread("fb-1")]
    assert FeedbackKind.ENDORSEMENT in kinds


async def test_rejecting_records_a_rejection():
    store = InMemoryFeedbackStore()
    llm = compose_llm([acts("Complete", "Stood down.", success=False, reason="declined")])
    graph, config, _ = await run_to_approval(store, llm, "fb-2")

    await graph.ainvoke(
        Command(resume={"verdict": "reject", "reason": "wrong person"}), config
    )

    recorded = [f for f in await store.for_thread("fb-2") if f.kind is FeedbackKind.REJECTION]
    assert recorded, "a declined send is the clearest negative signal there is"
    assert recorded[0].text == "wrong person"


async def test_editing_records_a_CORRECTION_so_promotion_can_count_it():
    """The mapping that matters most. `candidates()` counts CORRECTIONs, so filing an edit
    as anything else leaves the promotion counter reading zero forever — and an edit
    instruction repeated across runs ("add regards") is precisely the standing-rule signal
    that path exists to catch."""
    store = InMemoryFeedbackStore()
    llm = compose_llm(
        [
            ok("Revised."),
            acts("Complete", "Done.", success=True, reason="sent"),
        ]
    )
    graph, config, _ = await run_to_approval(store, llm, "fb-3")

    await graph.ainvoke(
        Command(resume={"verdict": "edit", "edit": "add regards at the end"}), config
    )

    corrections = [
        f for f in await store.for_thread("fb-3") if f.kind is FeedbackKind.CORRECTION
    ]
    assert corrections
    assert corrections[0].text == "add regards at the end"


async def test_the_resolved_draft_never_reaches_the_feedback_store():
    """The security property. `preview` carries REAL addresses and body text, deliberately
    un-tokenized so a human can verify what they are approving. The feedback store is
    persisted and read back across threads by `candidates()` — putting a resolved draft in
    it would undo the vault one approval at a time."""
    store = InMemoryFeedbackStore()
    llm = compose_llm([acts("Complete", "Sent.", success=True, reason="sent")])
    graph, config, _ = await run_to_approval(store, llm, "fb-4")

    await graph.ainvoke(Command(resume={"verdict": "approve"}), config)

    dumped = json.dumps([f.model_dump(mode="json") for f in await store.for_thread("fb-4")])
    assert "priya.nair@corp.com" not in dumped
    assert "It moved to 4pm" not in dumped


async def test_approval_feedback_is_recorded_as_already_applied():
    """The human said it TO the gate and the gate acted on it in the same turn. Left
    pending, the loop would replay their own decision back at them as fresh guidance."""
    store = InMemoryFeedbackStore()
    llm = compose_llm([acts("Complete", "Sent.", success=True, reason="sent")])
    graph, config, _ = await run_to_approval(store, llm, "fb-5")

    await graph.ainvoke(Command(resume={"verdict": "approve"}), config)

    assert await store.pending("fb-5") == []


async def test_a_run_without_a_feedback_store_still_approves():
    """`feedback` is optional on the gate; a missing store must not break the send."""
    llm = compose_llm([acts("Complete", "Sent.", success=True, reason="sent")])
    surface = compose_surface()
    graph = build_manager_graph(llm=llm, surface=surface, rules=NoRules(), max_steps=12)
    config = {"configurable": {"thread_id": "fb-6"}}

    await graph.ainvoke({"task": "email P1", "thread_id": "fb-6"}, config)
    await graph.ainvoke(Command(resume={"verdict": "approve"}), config)

    assert surface.verbs.count("Send") == 1


# ── end 2: rule candidates reach a human ────────────────────────────────────


async def test_repeated_corrections_become_an_offered_candidate():
    from app.api.ws import _offer_rule_candidates

    store = InMemoryFeedbackStore()
    for n in range(3):
        await store.record(
            Feedback(
                thread_id=f"t{n}",
                kind=FeedbackKind.CORRECTION,
                text="stop archiving the newsletters",
            )
        )

    emitted: list[tuple[str, int]] = []

    class FakeEmitter:
        async def rule_candidate(self, suggestion: str, count: int) -> None:
            emitted.append((suggestion, count))

    class FakeRun:
        emitter = FakeEmitter()

    class FakeContainer:
        feedback = store

    await _offer_rule_candidates(FakeRun(), FakeContainer())

    assert emitted, "three matching corrections is the documented promotion threshold"
    suggestion, count = emitted[0]
    assert count == 3
    assert "standing rule" in suggestion


async def test_a_single_correction_offers_nothing():
    """Two is coincidence; the threshold exists so the agent does not nag."""
    from app.api.ws import _offer_rule_candidates

    store = InMemoryFeedbackStore()
    await store.record(
        Feedback(thread_id="t1", kind=FeedbackKind.CORRECTION, text="stop archiving those")
    )

    emitted = []

    class FakeEmitter:
        async def rule_candidate(self, suggestion: str, count: int) -> None:
            emitted.append(suggestion)

    class FakeRun:
        emitter = FakeEmitter()

    class FakeContainer:
        feedback = store

    await _offer_rule_candidates(FakeRun(), FakeContainer())
    assert emitted == []


async def test_offering_candidates_never_takes_down_a_finished_run():
    """A run that just succeeded must not be reported as failed because a nice-to-have
    suggestion could not be computed."""
    from app.api.ws import _offer_rule_candidates

    class ExplodingStore:
        async def candidates(self):
            raise RuntimeError("store is down")

    class FakeRun:
        emitter = None  # would raise if touched

    class FakeContainer:
        feedback = ExplodingStore()

    await _offer_rule_candidates(FakeRun(), FakeContainer())


# ── end 3: a verdict on the whole run ───────────────────────────────────────


async def test_a_run_rating_is_readable_back():
    store = InMemoryFeedbackStore()
    await store.record(
        Feedback(
            thread_id="r1", kind=FeedbackKind.RUN_RATING, text="worked perfectly", applied=True
        )
    )

    rating = await store.rating("r1")
    assert rating is not None
    assert rating.text == "worked perfectly"


async def test_no_rating_is_distinguishable_from_a_bad_one():
    """"Nobody said" and "somebody said it was bad" are different facts. An evaluation that
    conflates them scores every unattended run as a failure."""
    store = InMemoryFeedbackStore()
    assert await store.rating("never-rated") is None


async def test_the_last_rating_wins():
    store = InMemoryFeedbackStore()
    for text in ("bad", "actually fine"):
        await store.record(
            Feedback(thread_id="r2", kind=FeedbackKind.RUN_RATING, text=text, applied=True)
        )

    rating = await store.rating("r2")
    assert rating is not None and rating.text == "actually fine"


async def test_a_run_rating_is_never_replayed_to_the_model():
    """It is a LABEL on a finished run, not an instruction. In `pending()` it would be fed
    back to the model as fresh guidance if the thread ever resumed."""
    store = InMemoryFeedbackStore()
    await store.record(
        Feedback(
            thread_id="r3", kind=FeedbackKind.RUN_RATING, text="that went badly", applied=True
        )
    )

    assert await store.pending("r3") == []


async def test_a_mid_run_correction_IS_still_replayed():
    """The counterfactual: the guard above must not have silenced ordinary corrections."""
    store = InMemoryFeedbackStore()
    await store.record(
        Feedback(thread_id="r4", kind=FeedbackKind.CORRECTION, text="not that thread")
    )

    pending = await store.pending("r4")
    assert [f.text for f in pending] == ["not that thread"]


@pytest.mark.parametrize("kind", list(FeedbackKind))
def test_every_kind_round_trips_as_its_wire_value(kind):
    """The cockpit sends these as strings; an enum member that does not survive the trip is
    a message silently refiled as something else."""
    assert FeedbackKind(kind.value) is kind
