"""The feedback loop — three loops at three timescales."""
from __future__ import annotations

import pytest
from inbox_contracts import ActionCall, ActionResult, Element

from app.agent.assessment import derive_outcome, outcome_note, split_assessment
from app.agent.state import AgentState
from app.events.emitter import EventEmitter
from app.events.sink import BufferSink
from app.feedback.models import Feedback, FeedbackKind, Outcome
from app.feedback.store import InMemoryFeedbackStore, normalise
from app.llm.base import LLMResult, ToolCall
from app.workers.loop import build_reason_node
from app.workers.tools import TRIAGE_TOOLS
from tests.fakes.fake_llm import FakeLLMClient
from tests.fakes.fake_surface import FakeEmailSurface, observation


def state(**overrides) -> AgentState:
    return AgentState(task="clear the inbox", thread_id="run-1", **overrides)


def acted(text: str, **args) -> LLMResult:
    return LLMResult(
        text=text,
        tool_calls=[ToolCall(id="c1", name="Archive", args=args or {"index": 3})],
        provider="fake",
    )


def correction(text: str, thread_id: str = "run-1") -> Feedback:
    return Feedback(thread_id=thread_id, kind=FeedbackKind.CORRECTION, text=text)


# ── loop 1: per-turn self-assessment ────────────────────────────────────────


def test_an_assessment_line_is_separated_from_the_reasoning():
    assessment, rest = split_assessment(
        "Assessment: my click did not open the thread.\nI'll try the row itself instead."
    )
    assert assessment == "my click did not open the thread."
    assert "try the row itself" in rest
    assert "Assessment:" not in rest


@pytest.mark.parametrize(
    "line",
    ["Assessment: it worked", "**Assessment:** it worked", "- assessment - it worked"],
)
def test_the_assessment_is_recognised_however_it_is_decorated(line):
    assert split_assessment(f"{line}\nNow the next step.")[0] is not None


def test_reasoning_without_an_assessment_is_left_whole():
    """It is never enforced with a retry, so the absent case must be ordinary."""
    assessment, rest = split_assessment("I'll archive the newsletter at [3].")
    assert assessment is None
    assert rest == "I'll archive the newsletter at [3]."


def test_a_successful_action_that_moved_the_page_progressed():
    assert (
        derive_outcome(result=ActionResult(success=True), page_changed=True)
        is Outcome.PROGRESSED
    )


def test_a_successful_action_that_changed_nothing_is_the_interesting_case():
    """Invisible to a success flag, and the most common way a run is wasted."""
    assert (
        derive_outcome(result=ActionResult(success=True), page_changed=False)
        is Outcome.NO_EFFECT
    )


def test_a_failed_action_is_failed():
    assert (
        derive_outcome(result=ActionResult(success=False, reason="x"), page_changed=False)
        is Outcome.FAILED
    )


def test_the_first_turn_has_nothing_to_assess():
    assert (
        derive_outcome(result=ActionResult(success=True), page_changed=False, is_first_action=True)
        is Outcome.UNKNOWN
    )


def test_the_note_reports_what_happened_rather_than_dictating_what_to_do():
    """Telling the model its next move pre-empts the reasoning we want from it."""
    note = outcome_note(Outcome.NO_EFFECT, "Click") or ""
    assert "did not change" in note
    assert "Check before repeating" in note


def test_progress_needs_no_note():
    assert outcome_note(Outcome.PROGRESSED, "Click") is None


async def test_the_loop_feeds_the_measured_outcome_back_to_the_model():
    llm = FakeLLMClient([acted("Assessment: nothing moved.\nTrying the row.")])
    node = build_reason_node(
        llm, EventEmitter(BufferSink()), tools=TRIAGE_TOOLS, max_steps=40
    )

    await node(
        state(
            last_action=ActionCall(name="Click", args={"index": 2}),
            last_result=ActionResult(success=True),
            stuck_count=1,  # the page did not change
        )
    )

    sent = " ".join(m.content for _, messages, _ in llm.requests for m in messages)
    assert "did not change" in sent


async def test_the_assessment_is_emitted_as_its_own_signal():
    sink = BufferSink()
    llm = FakeLLMClient([acted("Assessment: the click missed.\nTrying again differently.")])

    await build_reason_node(llm, EventEmitter(sink), tools=TRIAGE_TOOLS, max_steps=40)(
        state(
            last_action=ActionCall(name="Click", args={"index": 2}),
            last_result=ActionResult(success=True),
            stuck_count=1,
        )
    )

    assessments = sink.of_type("assessment")
    assert assessments and assessments[0].data["text"] == "the click missed."
    assert assessments[0].data["outcome"] == Outcome.NO_EFFECT.value


async def test_the_assessment_is_recorded_against_the_run():
    store = InMemoryFeedbackStore()
    llm = FakeLLMClient([acted("Assessment: it worked.\nNext one.")])

    await build_reason_node(
        llm, EventEmitter(BufferSink()), tools=TRIAGE_TOOLS, max_steps=40, feedback=store
    )(
        state(
            last_action=ActionCall(name="Archive", args={"index": 1}),
            last_result=ActionResult(success=True),
        )
    )

    recorded = await store.for_thread("run-1")
    assert [f.kind for f in recorded] == [FeedbackKind.ASSESSMENT]
    assert recorded[0].action == "Archive"


# ── loop 2: human correction mid-run ────────────────────────────────────────


async def test_a_pending_correction_reaches_the_model_next_turn():
    store = InMemoryFeedbackStore()
    await store.record(correction("don't archive anything from my manager"))
    llm = FakeLLMClient([acted("Understood.")])

    await build_reason_node(
        llm, EventEmitter(BufferSink()), tools=TRIAGE_TOOLS, max_steps=40, feedback=store
    )(state())

    sent = " ".join(m.content for _, messages, _ in llm.requests for m in messages)
    assert "don't archive anything from my manager" in sent


async def test_a_correction_is_shown_once_and_then_marked_applied():
    """Unshown feedback is a broken promise; twice-shown feedback is nagging."""
    store = InMemoryFeedbackStore()
    await store.record(correction("skip the newsletters"))
    llm = FakeLLMClient([acted("Skipping those."), acted("Moving on.")])
    node = build_reason_node(
        llm, EventEmitter(BufferSink()), tools=TRIAGE_TOOLS, max_steps=40, feedback=store
    )

    await node(state())
    assert await store.pending("run-1") == []

    await node(state(step=1))
    second_turn = " ".join(m.content for m in llm.requests[1][1])
    assert "skip the newsletters" not in second_turn


async def test_assessments_are_never_replayed_as_instructions():
    """Otherwise the agent argues with itself."""
    store = InMemoryFeedbackStore()
    await store.record(
        Feedback(thread_id="run-1", kind=FeedbackKind.ASSESSMENT, text="that worked")
    )
    assert await store.pending("run-1") == []


async def test_a_loop_without_a_feedback_store_still_runs():
    """Feedback is a capability, not a dependency."""
    llm = FakeLLMClient([acted("Archiving.")])
    delta = await build_reason_node(
        llm, EventEmitter(BufferSink()), tools=TRIAGE_TOOLS, max_steps=40
    )(state())
    assert delta["last_action"].name == "Archive"


# ── loop 3: promotion across runs ───────────────────────────────────────────


def test_differently_phrased_corrections_share_a_shape():
    """Exact matching would never fire; people rephrase every time."""
    assert normalise("please don't archive newsletters") == normalise(
        "stop archiving the newsletters"
    )


async def test_a_repeated_correction_becomes_a_rule_candidate():
    """Said three times, it is a preference the user should not have to keep repeating."""
    store = InMemoryFeedbackStore()
    for thread in ("run-1", "run-2", "run-3"):
        await store.record(correction("don't archive receipts", thread))

    candidates = await store.candidates()
    assert len(candidates) == 1
    assert candidates[0].count == 3
    assert "standing rule" in candidates[0].suggestion


async def test_one_correction_is_not_a_pattern():
    store = InMemoryFeedbackStore()
    await store.record(correction("don't archive receipts"))
    assert await store.candidates() == []


async def test_candidates_span_threads_because_a_preference_recurs_per_run():
    store = InMemoryFeedbackStore(promotion_threshold=2)
    await store.record(correction("leave manager mail alone", "a"))
    await store.record(correction("leave the manager's mail alone", "b"))

    assert len(await store.candidates()) == 1


async def test_a_candidate_is_a_suggestion_not_an_applied_rule():
    """A rule created from an inferred preference is a behaviour change nobody approved —
    on a surface where behaviour changes send email."""
    store = InMemoryFeedbackStore(promotion_threshold=2)
    for thread in ("a", "b"):
        await store.record(correction("never touch starred mail", thread))

    candidate = (await store.candidates())[0]
    assert "shall i make it a standing rule" in candidate.suggestion.lower()
    assert candidate.examples[0] == "never touch starred mail"


async def test_only_corrections_are_promoted():
    """An endorsement means 'that was right', which is not an instruction to encode."""
    store = InMemoryFeedbackStore(promotion_threshold=2)
    for thread in ("a", "b", "c"):
        await store.record(
            Feedback(thread_id=thread, kind=FeedbackKind.ENDORSEMENT, text="good job")
        )
    assert await store.candidates() == []


# ── the loop closes ─────────────────────────────────────────────────────────


async def test_a_correction_changes_the_next_action_not_just_the_transcript():
    """The whole point: feedback that does not alter behaviour is theatre."""
    store = InMemoryFeedbackStore()
    await store.record(correction("archive [5] instead, not [3]"))

    llm = FakeLLMClient(
        [
            LLMResult(
                text="Assessment: the user corrected me.\nArchiving [5].",
                tool_calls=[ToolCall(id="c", name="Archive", args={"index": 5})],
                provider="fake",
            )
        ]
    )
    surface = FakeEmailSurface([observation(Element(index=5, role="listitem", name="X"))])

    delta = await build_reason_node(
        llm, EventEmitter(BufferSink()), tools=TRIAGE_TOOLS, max_steps=40, feedback=store
    )(state())

    assert delta["last_action"].args["index"] == 5
    assert surface.never_dispatched("Send")
