"""A new run starts where every other run started — not where the last one gave up.

**The bug.** The browser outlives the run; `AgentState` does not. Every run gets a fresh
state keyed by `thread_id`, but the page is whatever the previous run walked away from — and
a run that was stopped, or whose approval timed out, walks away with a compose window open
and a half-written draft in it.

Type a new task, press Run, and the agent begins inside that stale window. Worse, the guard
that is *correct within* a run turns on you across runs: `COMPOSE_ALREADY_OPEN` says "a
compose window is already open — write in that one instead of opening another", so the new
task is steered into somebody else's abandoned email rather than starting fresh.

So the surface is reset once per run, at `dispatch` — the boundary between "we have decided
what to do" and "something is about to touch the mailbox". Not in PRE, which mutates
nothing; not in the worker loop, which runs per step.
"""
from __future__ import annotations

import json

import pytest
from inbox_contracts import Element

from app.agent.graph import build_dispatch_node, build_manager_graph
from app.agent.state import AgentState
from app.events.emitter import EventEmitter
from app.events.sink import BufferSink
from app.llm.base import LLMResult, ToolCall
from app.manager.intent import Action, TaskIntent
from app.rules.store import NoRules
from tests.fakes.fake_llm import FakeLLMClient, drafted, ok
from tests.fakes.fake_surface import FakeEmailSurface, observation


def acts(name: str, text: str, **args) -> LLMResult:
    return LLMResult(
        text=text, tool_calls=[ToolCall(id="c", name=name, args=args)], provider="fake"
    )


def intake(action: str, **slots):
    return ok(json.dumps({"action": action, "slots": slots, "confidence": 0.95}))


def a_state() -> AgentState:
    return AgentState(
        task="email P1 about the demo",
        thread_id="clean-1",
        intent=TaskIntent(
            action=Action.SEND_EMAIL,
            slots={"recipient_identity": "P1", "topic": "the demo"},
            action_confidence=0.95,
        ),
    )


# ── the reset happens, once, before anything touches the mailbox ────────────


@pytest.mark.anyio
async def test_dispatch_resets_the_surface_before_a_worker_runs():
    surface = FakeEmailSurface()
    node = build_dispatch_node(EventEmitter(BufferSink()), surface)

    await node(a_state())

    assert surface.resets == 1


@pytest.mark.anyio
async def test_it_happens_before_any_action_is_attempted():
    """Order is the whole guarantee. Resetting after the worker has typed into a stale
    compose window would close the window it just filled."""
    surface = FakeEmailSurface()
    node = build_dispatch_node(EventEmitter(BufferSink()), surface)

    await node(a_state())

    assert surface.resets == 1
    assert surface.calls == [], "an action ran before the page was made clean"


@pytest.mark.anyio
async def test_what_it_cleared_is_reported_to_the_human():
    """A window closing on its own is alarming unless somebody says why."""
    sink = BufferSink()
    surface = FakeEmailSurface(reset_report="closed a compose window left open by an earlier run")
    node = build_dispatch_node(EventEmitter(sink), surface)

    await node(a_state())

    said = " ".join(str(event.data.get("message", "")) for event in sink.events)
    assert "Starting fresh" in said
    assert "closed a compose window" in said


@pytest.mark.anyio
async def test_a_clean_page_says_nothing_at_all():
    """The common case is a tidy mailbox. Announcing a no-op every run is noise."""
    sink = BufferSink()
    node = build_dispatch_node(EventEmitter(sink), FakeEmailSurface())

    await node(a_state())

    assert "Starting fresh" not in " ".join(
        str(event.data.get("message", "")) for event in sink.events
    )


@pytest.mark.anyio
async def test_a_reset_that_fails_does_not_take_the_run_down():
    """A dirty starting page is a worse run, not a dead one. Refusing the task a human just
    asked for because we could not tidy up first is the wrong trade every time."""

    class Broken(FakeEmailSurface):
        async def reset(self) -> str:
            raise RuntimeError("the page went away")

    node = build_dispatch_node(EventEmitter(BufferSink()), Broken())

    delta = await node(a_state())

    assert delta["status"] == "running"
    assert delta["active_worker"]


@pytest.mark.anyio
async def test_a_graph_with_no_surface_still_dispatches():
    """The PRE-phase tests build the graph without a browser, and must stay able to."""
    node = build_dispatch_node(EventEmitter(BufferSink()), None)

    assert (await node(a_state()))["status"] == "running"


# ── through the real graph ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_second_run_on_a_dirty_page_is_cleaned_before_it_starts():
    """THE regression, end to end: run one leaves compose open, run two starts fresh."""
    surface = FakeEmailSurface(
        [observation(Element(index=9, role="button", name="Send"), compose_open=True)],
        reset_report="closed a compose window left open by an earlier run",
    )
    llm = FakeLLMClient(
        [
            intake("send_email", recipient_identity="P1", topic="the demo"),
            ok("decision"),
            ok("Open compose\nSend"),
            drafted(),
            acts("Complete", "Nothing more to do.", success=True, reason="done"),
        ]
    )
    graph = build_manager_graph(llm=llm, surface=surface, rules=NoRules())

    await graph.ainvoke(
        {"task": "email P1 about the demo", "thread_id": "clean-2"},
        {"configurable": {"thread_id": "clean-2"}},
    )

    assert surface.resets == 1, "the leftover compose window was inherited"
