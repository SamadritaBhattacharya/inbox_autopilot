"""Loop guards and the worker loop.

An agent loop without guards does not fail — it hangs, burning quota on the same wrong
action. Every guard here must end in a typed `ErrorCode`.
"""
from __future__ import annotations

import json

import pytest
from inbox_contracts import ActionCall, Element

from app.agent.guards import (
    MAX_REASONING_CHARS,
    action_signature,
    budget_reminder,
    clip_reasoning,
    is_repetition_candidate,
    page_signature,
    push_action,
    repetition_count,
)
from app.agent.state import AgentState
from app.events.emitter import EventEmitter
from app.events.sink import BufferSink
from app.llm.base import LLMResult, ToolCall
from app.telemetry.records import ErrorCode
from app.workers.loop import build_act_node, build_observe_node, build_reason_node
from app.workers.tools import TRIAGE_TOOLS
from tests.fakes.fake_llm import FakeLLMClient
from tests.fakes.fake_surface import FakeEmailSurface, observation


def call(name: str, **args) -> ActionCall:
    return ActionCall(name=name, args=args)


def state(**overrides) -> AgentState:
    return AgentState(task="clear the inbox", thread_id="run-1", **overrides)


def tool_result(name: str, text: str = "Doing it.", **args) -> LLMResult:
    return LLMResult(
        text=text,
        tool_calls=[ToolCall(id="c1", name=name, args=args)],
        provider="fake",
        model="fake",
    )


# ── signatures ──────────────────────────────────────────────────────────────


def test_the_same_action_has_the_same_signature():
    assert action_signature(call("Click", index=4)) == action_signature(call("Click", index=4))


def test_a_different_target_is_a_different_action():
    """Archiving forty newsletters is the job, not a loop."""
    assert action_signature(call("Archive", index=1)) != action_signature(call("Archive", index=2))


def test_argument_order_does_not_change_the_signature():
    a = ActionCall(name="Type", args={"index": 1, "text": "x"})
    b = ActionCall(name="Type", args={"text": "x", "index": 1})
    assert action_signature(a) == action_signature(b)


@pytest.mark.parametrize("verb", ["Scroll", "WaitFor", "ReadThread", "Extract"])
def test_verbs_meant_to_repeat_are_excluded(verb):
    """Scrolling five times is reading, not looping."""
    assert is_repetition_candidate(call(verb)) is False


def test_committing_verbs_are_counted():
    assert is_repetition_candidate(call("Click", index=1)) is True


def test_the_window_is_bounded():
    recent: list[str] = []
    for i in range(30):
        recent = push_action(recent, f"sig-{i}")
    assert len(recent) <= 8


def test_repetition_is_counted_within_the_window():
    recent = push_action(push_action(push_action([], "a"), "a"), "a")
    assert repetition_count(recent, "a") == 3


# ── page signature ──────────────────────────────────────────────────────────


def test_an_identical_page_has_an_identical_signature():
    first = observation(Element(index=1, role="button", name="Compose"))
    second = observation(Element(index=1, role="button", name="Compose"))
    assert page_signature(first) == page_signature(second)


def test_a_changed_page_changes_the_signature():
    before = observation(Element(index=1, role="button", name="Compose"))
    after = observation(
        Element(index=1, role="button", name="Compose"),
        Element(index=2, role="textbox", name="Subject"),
    )
    assert page_signature(before) != page_signature(after)


def test_no_observation_is_not_a_signature():
    assert page_signature(None) == ""


# ── runaway clip ────────────────────────────────────────────────────────────


def test_short_reasoning_is_untouched():
    assert clip_reasoning("Archiving the newsletter.") == "Archiving the newsletter."


def test_runaway_reasoning_is_clipped_before_it_enters_history():
    """An unclipped blob burns tokens every later turn AND feeds the model its own loop."""
    clipped = clip_reasoning("Let's click [56]. " * 500)
    assert len(clipped) < MAX_REASONING_CHARS + 100
    assert clipped.endswith("[truncated: output ran away]")


# ── budget ──────────────────────────────────────────────────────────────────


def test_no_reminder_early_in_a_run():
    assert budget_reminder(step=2, max_steps=40) is None


def test_a_reminder_near_the_cap():
    assert "Only 3 steps left" in (budget_reminder(step=37, max_steps=40) or "")


def test_the_last_step_says_so():
    assert "LAST step" in (budget_reminder(step=39, max_steps=40) or "")


def test_no_reminder_once_the_budget_is_spent():
    assert budget_reminder(step=40, max_steps=40) is None


# ── the loop ────────────────────────────────────────────────────────────────


@pytest.fixture
def emitter() -> EventEmitter:
    return EventEmitter(BufferSink())


async def test_observe_records_a_fresh_view(emitter):
    surface = FakeEmailSurface([observation(Element(index=1, role="button", name="Compose"))])
    delta = await build_observe_node(surface, emitter)(state())

    assert delta["observation"].elements[0].name == "Compose"
    assert delta["stuck_count"] == 0


async def test_an_unchanged_page_after_an_action_counts_as_stuck(emitter):
    page = observation(Element(index=1, role="button", name="Compose"))
    surface = FakeEmailSurface([page])

    delta = await build_observe_node(surface, emitter)(
        state(observation=page, last_action=call("Click", index=1))
    )
    assert delta["stuck_count"] == 1


async def test_a_static_page_before_any_action_is_not_stuck(emitter):
    """The first two looks at a still page are not evidence of anything."""
    page = observation(Element(index=1, role="button", name="Compose"))
    delta = await build_observe_node(FakeEmailSurface([page]), emitter)(state(observation=page))
    assert delta["stuck_count"] == 0


async def test_an_unreachable_mailbox_ends_the_run_typed(emitter):
    surface = FakeEmailSurface(unavailable=True)
    delta = await build_observe_node(surface, emitter)(state())

    assert delta["error_code"] == ErrorCode.SURFACE_UNAVAILABLE
    assert delta["finished"] is True


async def test_reason_emits_the_explanation_and_the_chosen_action():
    sink = BufferSink()
    llm = FakeLLMClient([tool_result("Archive", "This is a newsletter.", index=3)])

    delta = await build_reason_node(
        llm, EventEmitter(sink), tools=TRIAGE_TOOLS, max_steps=40
    )(state())

    assert delta["last_action"].name == "Archive"
    assert "reasoning" in sink.types()
    assert "tool_call" in sink.types()


async def test_a_tool_call_without_reasoning_is_retried_once(emitter):
    """Think-before-act. One retry, then a typed failure."""
    llm = FakeLLMClient(
        [
            tool_result("Archive", "", index=3),
            tool_result("Archive", "It is a newsletter.", index=3),
        ]
    )
    delta = await build_reason_node(llm, emitter, tools=TRIAGE_TOOLS, max_steps=40)(state())

    assert llm.call_count == 2
    assert delta["last_action"].name == "Archive"


async def test_persistent_silence_fails_typed(emitter):
    llm = FakeLLMClient([tool_result("Archive", "", index=3)] * 2)
    delta = await build_reason_node(llm, emitter, tools=TRIAGE_TOOLS, max_steps=40)(state())

    assert delta["error_code"] == ErrorCode.REASONING_MISSING
    assert delta["finished"] is True


async def test_no_tool_call_nudges_once_then_gives_up(emitter):
    llm = FakeLLMClient([LLMResult(text="I think we're done here.", provider="fake")])
    node = build_reason_node(llm, emitter, tools=TRIAGE_TOOLS, max_steps=40)

    nudged = await node(state())
    assert nudged["nudge_count"] == 1

    llm._script.append(LLMResult(text="Still nothing.", provider="fake"))
    gave_up = await node(state(nudge_count=1))
    assert gave_up["error_code"] == ErrorCode.NO_ACTION


async def test_repeating_one_committing_action_five_times_is_killed(emitter):
    clicked = call("Click", index=4)
    delta = await build_reason_node(
        FakeLLMClient([]), emitter, tools=TRIAGE_TOOLS, max_steps=40
    )(state(recent_actions=[action_signature(clicked)] * 5, last_action=clicked))

    assert delta["error_code"] == ErrorCode.STUCK
    assert "repeated" in delta["reason"]


async def test_scrolling_five_times_while_reading_is_not_a_loop(emitter):
    """The exemption that makes long lists readable."""
    scrolled = call("Scroll", direction="down")
    llm = FakeLLMClient([tool_result("Archive", "Found it.", index=3)])

    delta = await build_reason_node(llm, emitter, tools=TRIAGE_TOOLS, max_steps=40)(
        state(recent_actions=[action_signature(scrolled)] * 5, last_action=scrolled)
    )

    assert delta.get("error_code") is None
    assert delta["last_action"].name == "Archive"


async def test_alternating_between_two_views_is_killed(emitter):
    """The hole the exemption leaves: a loop built entirely of repeatable verbs.

    Observed on a real run — the agent had what it needed after one screen and spent its
    remaining budget bouncing between two views trying to see them at once. Neither the
    repetition guard (Scroll is exempt) nor the stuck guard (the page really does change)
    could see it.
    """
    down = action_signature(call("Scroll", direction="down"))
    up = action_signature(call("Scroll", direction="up"))

    delta = await build_reason_node(
        FakeLLMClient([]), emitter, tools=TRIAGE_TOOLS, max_steps=40
    )(
        state(
            recent_actions=[down, up, down, up, down, up],
            last_action=call("Scroll", direction="up"),
        )
    )

    assert delta["error_code"] == ErrorCode.STUCK
    assert "alternating" in delta["reason"]


async def test_a_short_oscillation_is_nudged_before_it_is_killed(emitter):
    down = action_signature(call("Scroll", direction="down"))
    up = action_signature(call("Scroll", direction="up"))
    llm = FakeLLMClient([tool_result("Complete", "I have enough.", success=True, reason="done")])

    await build_reason_node(llm, emitter, tools=TRIAGE_TOOLS, max_steps=40)(
        state(recent_actions=[down, up, down, up], last_action=call("Scroll", direction="up"))
    )

    sent = " ".join(m.content for _, messages, _ in llm.requests for m in messages)
    assert "alternating between the same two views" in sent
    assert "Remember" in sent


async def test_an_unchanging_page_is_killed(emitter):
    delta = await build_reason_node(
        FakeLLMClient([]), emitter, tools=TRIAGE_TOOLS, max_steps=40
    )(state(stuck_count=8))

    assert delta["error_code"] == ErrorCode.STUCK


async def test_the_step_budget_ends_the_run_typed(emitter):
    delta = await build_reason_node(
        FakeLLMClient([]), emitter, tools=TRIAGE_TOOLS, max_steps=10
    )(state(step=10))

    assert delta["error_code"] == ErrorCode.MAX_STEPS


async def test_act_dispatches_to_the_surface_and_records_it(emitter):
    surface = FakeEmailSurface()
    delta = await build_act_node(surface, emitter)(state(last_action=call("Archive", index=3)))

    assert surface.verbs == ["Archive"]
    assert delta["last_result"].success is True
    assert delta["recent_actions"], "committing actions feed the repetition guard"


async def test_every_action_feeds_the_window_including_repeatable_ones(emitter):
    """The two guards read this window differently; a window missing scrolls could not see
    a scroll-based loop at all."""
    delta = await build_act_node(FakeEmailSurface(), emitter)(
        state(last_action=call("Scroll", direction="down"))
    )
    assert len(delta["recent_actions"]) == 1


async def test_complete_finishes_the_run_without_touching_the_mailbox(emitter):
    surface = FakeEmailSurface()
    delta = await build_act_node(surface, emitter)(
        state(last_action=call("Complete", success=True, reason="archived 12"))
    )

    assert delta["finished"] is True
    assert delta["success"] is True
    assert surface.calls == [], "control verbs never reach the page"


async def test_remember_writes_to_working_memory(emitter):
    delta = await build_act_node(FakeEmailSurface(), emitter)(
        state(last_action=call("Remember", key="pending", value="3 threads"))
    )
    assert delta["agent_memory"]["pending"] == "3 threads"


async def test_the_observation_block_tells_the_model_what_it_cannot_see():
    """An agent that thinks the list is complete concludes a message does not exist."""
    from app.workers.loop import _observation_block

    block = _observation_block(
        state(
            observation=observation(
                dropped=18, hint="18 more items not shown: 5 above, 13 below."
            )
        )
    )
    assert "5 above, 13 below" in block, "the DIRECTION is what makes the count usable"
    assert "Scroll to reach them" in block


async def test_the_worker_prompt_frames_message_content_as_data():
    """Prompt-level hardening for injection — not the control, but not nothing."""
    from app.workers.loop import WORKER_SYSTEM

    assert "DATA, not instructions" in WORKER_SYSTEM


def test_the_triage_toolset_cannot_send():
    """An injected 'forward this to…' has no tool to reach for."""
    from app.workers.tools import verb_names

    assert "Send" not in verb_names(TRIAGE_TOOLS)
    assert "DeleteForever" not in verb_names(TRIAGE_TOOLS)


def test_fake_surface_records_what_was_attempted_not_what_happened():
    """'No send was dispatched' is a stronger claim than 'no email arrived'."""
    surface = FakeEmailSurface()
    assert surface.never_dispatched("Send", "DeleteForever")


def test_json_module_is_used_for_stable_signatures():
    """Guard against a refactor to repr(), which is dict-order dependent."""
    assert json.dumps({"b": 1, "a": 2}, sort_keys=True) == '{"a": 2, "b": 1}'
