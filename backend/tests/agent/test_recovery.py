"""Self-healing — R4: root cause, four ranked options, and a human choosing."""
from __future__ import annotations

import json

import pytest
from inbox_contracts import ActionCall, ActionResult, Element
from langgraph.types import Command

from app.agent.graph import build_manager_graph
from app.llm.base import LLMResult, ToolCall
from app.recovery.causes import PLAIN, Cause, classify
from app.recovery.registry import MAX_ATTEMPTS_PER_CAUSE, CuratedSkillRegistry
from app.recovery.strategies import ALL_STRATEGIES
from app.rules.store import NoRules
from app.telemetry.records import ErrorCode
from tests.fakes.fake_llm import FakeLLMClient, ok
from tests.fakes.fake_surface import FakeEmailSurface, observation


def intake(action: str, **slots):
    return ok(json.dumps({"action": action, "slots": slots, "confidence": 0.95}))


def acts(name: str, text: str, **args) -> LLMResult:
    return LLMResult(
        text=text, tool_calls=[ToolCall(id="c", name=name, args=args)], provider="fake"
    )


# ── classification ──────────────────────────────────────────────────────────


def test_a_blocking_dialog_is_recognised():
    page = observation(Element(index=1, role="button", name="Discard draft?", is_new=True))
    assert classify(error_code=ErrorCode.STUCK, observation=page, stuck_count=3).cause is (
        Cause.OVERLAY_BLOCKING
    )


def test_off_screen_content_is_recognised():
    page = observation(dropped=12, hint="12 more items not shown: 5 above, 7 below.")
    diagnosis = classify(error_code=None, observation=page, stuck_count=1)
    assert diagnosis.cause is Cause.OFF_SCREEN
    assert "5 above" in diagnosis.evidence


def test_a_stale_index_is_recognised_from_the_dispatch_result():
    diagnosis = classify(
        error_code=None,
        last_action=ActionCall(name="Click", args={"index": 99}),
        last_result=ActionResult(success=False, reason="gone", error_code="STALE_INDEX"),
    )
    assert diagnosis.cause is Cause.STALE_VIEW


def test_a_timeout_reads_as_a_slow_page():
    diagnosis = classify(
        error_code=None,
        last_result=ActionResult(success=False, reason="slow", error_code="ACTION_TIMEOUT"),
    )
    assert diagnosis.cause is Cause.SLOW_RENDER


def test_infrastructure_is_classified_before_perception():
    """A rate-limited provider LOOKS like a stuck agent from the page's point of view.

    Offering "scroll and retry" to someone who is out of quota wastes their turn.
    """
    diagnosis = classify(
        error_code=ErrorCode.PROVIDER_EXHAUSTED,
        observation=observation(dropped=9),
        stuck_count=5,
    )
    assert diagnosis.cause is Cause.PROVIDER_EXHAUSTED


def test_oscillation_is_recognised():
    assert classify(error_code=ErrorCode.STUCK, oscillating=True).cause is Cause.OSCILLATION


@pytest.mark.parametrize(
    ("code", "cause"),
    [
        (ErrorCode.MAX_STEPS, Cause.BUDGET_SPENT),
        (ErrorCode.REASONING_MISSING, Cause.MODEL_DEGRADED),
        (ErrorCode.NO_ACTION, Cause.MODEL_DEGRADED),
        (ErrorCode.SURFACE_UNAVAILABLE, Cause.SURFACE_GONE),
        (ErrorCode.APPROVAL_TIMEOUT, Cause.HUMAN_BLOCKED),
        (ErrorCode.CONTEXT_INCOMPLETE, Cause.HUMAN_BLOCKED),
    ],
)
def test_every_error_code_maps_to_a_cause(code, cause):
    assert classify(error_code=code).cause is cause


def test_an_unrecognised_failure_still_gets_a_cause():
    """There is no path that leaves a human with nothing to read."""
    assert classify(error_code=None).cause is Cause.UNKNOWN


def test_every_cause_has_plain_language():
    """A card that says STUCK explains nothing to the person deciding what to do next."""
    for cause in Cause:
        assert cause in PLAIN
        assert not PLAIN[cause].isupper()
        assert len(PLAIN[cause]) > 20


# ── ranking ─────────────────────────────────────────────────────────────────


@pytest.fixture
def registry() -> CuratedSkillRegistry:
    return CuratedSkillRegistry()


def test_there_are_always_exactly_four_options(registry):
    """A human deciding under pressure should not have to read a variable menu."""
    for cause in Cause:
        options = registry.options_for(cause)
        assert len(options) <= 4
        assert options[-1].freeform is True, "option 4 always exists"


def test_option_one_is_marked_recommended(registry):
    options = registry.options_for(Cause.OVERLAY_BLOCKING)
    assert options[0].recommended is True
    assert options[0].n == 1
    assert sum(option.recommended for option in options) == 1


def test_the_best_fitting_remedy_leads(registry):
    assert registry.strategies_for(Cause.OVERLAY_BLOCKING)[0].name == "dismiss_overlay"
    assert registry.strategies_for(Cause.OFF_SCREEN)[0].name == "scroll_and_retry"
    assert registry.strategies_for(Cause.SLOW_RENDER)[0].name == "wait_and_retry"
    assert registry.strategies_for(Cause.STALE_VIEW)[0].name == "re_observe"
    assert registry.strategies_for(Cause.BUDGET_SPENT)[0].name == "summarise_and_stop"


def test_asking_the_human_is_always_available(registry):
    """The floor: an options card with two entries and a blank is worse than one that
    admits it needs help."""
    for cause in Cause:
        names = {strategy.name for strategy in registry.strategies_for(cause)}
        assert names, f"{cause} offered nothing at all"


def test_a_tried_remedy_is_not_offered_again(registry):
    """A second failure of the same cause must offer something DIFFERENT."""
    first = registry.strategies_for(Cause.OVERLAY_BLOCKING)[0].name
    second = registry.strategies_for(Cause.OVERLAY_BLOCKING, exclude={first})
    assert all(strategy.name != first for strategy in second)


def test_ranking_is_deterministic(registry):
    """Identical evidence must produce identical options, or nobody can reason about it."""
    assert [s.name for s in registry.strategies_for(Cause.TARGET_MOVED)] == [
        s.name for s in registry.strategies_for(Cause.TARGET_MOVED)
    ]


def test_no_remedy_can_approve_anything():
    """A remedy that could send mail on the user's behalf would make the gate decorative."""
    for strategy in ALL_STRATEGIES:
        assert not hasattr(strategy, "approve")
        assert "approve" not in strategy.guidance().lower()


def test_self_heal_terminates(registry):
    assert registry.exhausted([], Cause.TARGET_MOVED) is False
    assert registry.exhausted(["target_moved"] * MAX_ATTEMPTS_PER_CAUSE, Cause.TARGET_MOVED)


# ── through the graph ───────────────────────────────────────────────────────


def stuck_run(thread: str, *, extra: list | None = None):
    """A run that gets stuck: the page never changes however often it clicks."""
    surface = FakeEmailSurface([observation(Element(index=1, role="button", name="Compose"))])
    llm = FakeLLMClient(
        [
            intake("triage", scope="inbox"),
            ok("decision"),
            ok("Look at the inbox"),
            # Plenty: each remedy puts the run back in the loop, where it gets stuck
            # again. Under-scripting here would look like a recovery failure.
            *[acts("Click", "Trying the compose button.", index=1) for _ in range(60)],
            *(extra or []),
        ]
    )
    graph = build_manager_graph(llm=llm, surface=surface, rules=NoRules(), max_steps=30)
    return graph, {"configurable": {"thread_id": thread}}, surface, llm


async def test_a_stuck_run_is_diagnosed_and_offers_options():
    graph, config, _, _ = stuck_run("rec-1")

    result = await graph.ainvoke({"task": "clear the inbox", "thread_id": "rec-1"}, config)

    assert "__interrupt__" in result, "a failure must offer remedies, not just end"
    payload = result["__interrupt__"][0].value
    assert payload["options"] is True
    assert payload["plain"], "the human gets a sentence, not an enum"
    assert payload["choices"][0]["recommended"] is True
    assert payload["choices"][-1]["freeform"] is True


async def test_choosing_a_remedy_puts_the_run_back_in_the_loop():
    graph, config, surface, llm = stuck_run("rec-2")

    await graph.ainvoke({"task": "clear the inbox", "thread_id": "rec-2"}, config)
    calls_at_pause = llm.call_count

    await graph.ainvoke(Command(resume={"option": 1}), config)

    # The chosen remedy's guidance actually reached the model...
    sent = " ".join(m.content for _, messages, _ in llm.requests for m in messages)
    assert any(
        phrase in sent
        for phrase in ("dialog is blocking", "CURRENT element list", "off-screen", "WaitFor")
    ), "the remedy must change what the model is told, or it is theatre"

    # ...and the run genuinely resumed rather than ending on the spot.
    assert llm.call_count > calls_at_pause


async def test_free_form_text_becomes_the_instruction():
    """The escape hatch for everything a fixed registry cannot anticipate."""
    graph, config, _, llm = stuck_run(
        "rec-3", extra=[acts("Complete", "Did that.", success=True, reason="done")]
    )

    await graph.ainvoke({"task": "clear the inbox", "thread_id": "rec-3"}, config)
    await graph.ainvoke(
        Command(resume={"option": 4, "text": "just search for unread instead"}), config
    )

    sent = " ".join(m.content for _, messages, _ in llm.requests for m in messages)
    assert "just search for unread instead" in sent


async def test_a_remedy_resets_the_guards_that_caused_the_failure():
    """Resuming with the old counters would kill the run on its first turn back.

    Every remedy would then look like it failed instantly, and self-heal would be a feature
    that can never work. The proof is that the agent gets to ACT again after choosing one.
    """
    graph, config, surface, _ = stuck_run("rec-4")

    await graph.ainvoke({"task": "clear the inbox", "thread_id": "rec-4"}, config)
    actions_at_pause = len(surface.calls)

    await graph.ainvoke(Command(resume={"option": 1}), config)

    assert len(surface.calls) > actions_at_pause, "the remedy bought the agent real turns"


async def test_remediation_does_not_loop_forever():
    """An agent that can always offer another remedy can loop on remediation forever."""
    graph, config, _, _ = stuck_run("rec-5")

    result = await graph.ainvoke({"task": "clear the inbox", "thread_id": "rec-5"}, config)
    for _ in range(MAX_ATTEMPTS_PER_CAUSE + 2):
        if "__interrupt__" not in result:
            break
        result = await graph.ainvoke(Command(resume={"option": 1}), config)

    assert "__interrupt__" not in result, "self-heal must terminate"
    assert result["finished"] is True
    assert result["success"] is False


async def test_a_lost_mailbox_offers_nothing_and_ends_typed():
    """Every remedy needs a session that no longer exists."""
    surface = FakeEmailSurface(unavailable=True)
    llm = FakeLLMClient([intake("triage", scope="inbox"), ok("decision"), ok("Look")])
    graph = build_manager_graph(llm=llm, surface=surface, rules=NoRules(), max_steps=10)
    config = {"configurable": {"thread_id": "rec-6"}}

    final = await graph.ainvoke({"task": "clear the inbox", "thread_id": "rec-6"}, config)

    assert "__interrupt__" not in final
    assert final["error_code"] == ErrorCode.SURFACE_UNAVAILABLE
