"""The calendar worker — turning a thread into a proposal a human can check."""
from __future__ import annotations

import json

import pytest
from inbox_contracts import Element

from app.agent.graph import build_manager_graph
from app.events.emitter import EventEmitter
from app.events.sink import BufferSink
from app.llm.base import LLMResult, ToolCall
from app.manager.intent import Action
from app.rules.store import NoRules
from app.workers.registry import worker_for
from app.workers.tools import CALENDAR_TOOLS, verb_names
from tests.fakes.fake_llm import FakeLLMClient, ok
from tests.fakes.fake_surface import FakeEmailSurface, observation

THREAD = observation(
    Element(index=1, role="button", name="Back to inbox"),
    Element(index=2, role="heading", name="Friday demo moved to 4pm"),
    Element(index=3, role="listitem", name="C1: can we do Friday 22 August at 16:00 IST"),
    Element(index=4, role="listitem", name="C2: Friday 4pm works for me"),
    view="thread",
)


def intake(action: str, **slots):
    return ok(json.dumps({"action": action, "slots": slots, "confidence": 0.95}))


def acts(name: str, text: str, **args) -> LLMResult:
    return LLMResult(
        text=text, tool_calls=[ToolCall(id="c", name=name, args=args)], provider="fake"
    )


# ── capability ──────────────────────────────────────────────────────────────


def test_the_calendar_worker_is_read_only():
    """It drafts. A mis-drafted proposal costs nothing; a mis-sent invite lands in other
    people's calendars and cannot be recalled."""
    spec = worker_for(Action.EXTRACT_EVENT)
    assert spec.name == "calendar"
    assert spec.read_only is True


def test_it_cannot_send_an_invite_or_anything_else():
    bound = verb_names(CALENDAR_TOOLS)
    assert bound & {"Send", "SendInvite", "DeleteForever", "Archive", "Label"} == set()
    assert "ProposeEvent" in bound


# ── through the graph ───────────────────────────────────────────────────────


@pytest.fixture
def calendar_run():
    def build(thread: str, *, script: list):
        sink = BufferSink()
        surface = FakeEmailSurface([THREAD])
        graph = build_manager_graph(
            llm=FakeLLMClient(script),
            surface=surface,
            emitter=EventEmitter(sink),
            rules=NoRules(),
            max_steps=10,
        )
        return graph, {"configurable": {"thread_id": thread}}, surface, sink

    return build


async def test_it_proposes_an_event_read_out_of_the_thread(calendar_run):
    graph, config, surface, sink = calendar_run(
        "cal-1",
        script=[
            intake("extract_event", thread_ref="the Friday demo thread"),
            ok("decision"),
            ok("Read the thread\nPropose the event"),
            acts(
                "ProposeEvent",
                "The thread agrees on Friday at 16:00.",
                title="Client demo",
                when="Friday 22 August, 16:00 IST",
                duration="45 minutes",
                attendees="C1, C2",
                evidence="can we do Friday 22 August at 16:00 IST",
            ),
            acts("Complete", "Drafted it.", success=True, reason="proposed the demo slot"),
        ],
    )

    final = await graph.ainvoke(
        {"task": "put the demo in my calendar", "thread_id": "cal-1"}, config
    )

    proposed = sink.of_type("event_proposed")
    assert proposed, "the cockpit must receive the proposal"
    assert proposed[0].data["when"] == "Friday 22 August, 16:00 IST"
    assert proposed[0].data["duration"] == "45 minutes"
    assert final["success"] is True


async def test_the_proposal_never_touches_the_mailbox(calendar_run):
    graph, config, surface, _ = calendar_run(
        "cal-2",
        script=[
            intake("extract_event", thread_ref="the demo thread"),
            ok("decision"),
            ok("Read it"),
            acts("ProposeEvent", "Here it is.", title="Demo", when="Friday 16:00"),
            acts("Complete", "Done.", success=True, reason="proposed"),
        ],
    )

    await graph.ainvoke({"task": "add the demo to my calendar", "thread_id": "cal-2"}, config)

    assert surface.calls == [], "proposing is a graph-owned verb; it never reaches the page"


async def test_attendees_stay_as_tokens(calendar_run):
    """The model never held the real addresses, and the proposal must not invent them."""
    graph, config, _, sink = calendar_run(
        "cal-3",
        script=[
            intake("extract_event", thread_ref="the demo thread"),
            ok("decision"),
            ok("Read it"),
            acts(
                "ProposeEvent",
                "Both agreed.",
                title="Demo",
                when="Friday 16:00",
                attendees="C1, C2",
            ),
            acts("Complete", "Done.", success=True, reason="proposed"),
        ],
    )

    await graph.ainvoke({"task": "book the demo", "thread_id": "cal-3"}, config)

    payload = sink.of_type("event_proposed")[0].data
    assert payload["attendees"] == "C1, C2"
    assert "@" not in json.dumps(payload)


async def test_the_evidence_is_carried_so_a_human_can_check_it(calendar_run):
    """A proposal without the words it came from asks the user to take it on trust."""
    graph, config, _, sink = calendar_run(
        "cal-4",
        script=[
            intake("extract_event", thread_ref="the demo thread"),
            ok("decision"),
            ok("Read it"),
            acts(
                "ProposeEvent",
                "Quoting the thread.",
                title="Demo",
                when="Friday 16:00",
                evidence="can we do Friday 22 August at 16:00 IST",
            ),
            acts("Complete", "Done.", success=True, reason="proposed"),
        ],
    )

    await graph.ainvoke({"task": "book the demo", "thread_id": "cal-4"}, config)

    assert "16:00 IST" in sink.of_type("event_proposed")[0].data["evidence"]
