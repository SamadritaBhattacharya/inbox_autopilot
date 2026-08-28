"""A send that succeeds ends the run. The alternative is sending twice.

**Observed live, and the worst behaviour this loop has produced.** The mail went out, Gmail
returned to the inbox, and the agent — with nothing telling it that the thing it existed to
do was done — hunted for the Send button in an inbox, could not find it, clicked Compose,
and began writing THE SAME EMAIL again. One more approval and the recipient gets it twice.

The companion bug in the same run: `Send` had no handler at all on the Playwright surface
(`_perform` looks for `_do_<verb>`), so every approved send returned "Send has no handler"
at the exact moment a human had just authorised it. The agent's only way through was to fall
back to `Click` on the button — which the worker prompt actively discourages. The
recommended path was the broken one, and it had never worked.
"""
from __future__ import annotations

import json

from inbox_contracts import ActionCall, ActionResult, Element
from langgraph.types import Command

from app.agent.graph import build_manager_graph
from app.llm.base import LLMResult, ToolCall
from app.rules.store import NoRules
from app.workers.irreversible import is_irreversible
from tests.fakes.fake_llm import FakeLLMClient, drafted, ok
from tests.fakes.fake_surface import FakeEmailSurface, observation

DRAFT = "To:      Priya Nair <priya.nair@corp.com>\nSubject: Hi\n\nEvening."


def intake(action: str, **slots) -> LLMResult:
    return ok(json.dumps({"action": action, "slots": slots, "confidence": 0.95}))


def acts(name: str, text: str, **args) -> LLMResult:
    return LLMResult(
        text=text, tool_calls=[ToolCall(id=name, name=name, args=args)], provider="fake"
    )


def compose_view():
    return observation(
        Element(index=9, role="button", name="Send"),
        title="Compose",
        compose_open=True,
    )


async def _run(script, thread: str, results=None):
    surface = FakeEmailSurface([compose_view()], results or [], preview=DRAFT)
    graph = build_manager_graph(
        llm=FakeLLMClient(script), surface=surface, rules=NoRules(), max_steps=12
    )
    config = {"configurable": {"thread_id": thread}}
    await graph.ainvoke({"task": "email P1", "thread_id": thread}, config)
    final = await graph.ainvoke(Command(resume={"verdict": "approve"}), config)
    return final, surface


PRELUDE = [
    intake("send_email", recipient_identity="P1", topic="the demo"),
    ok("decision"),
    ok("Compose\nSend"),
    drafted(),
]


async def test_a_successful_send_finishes_the_run():
    """No further turns. Anything after a send is work nobody authorised."""
    script = [
        *PRELUDE,
        acts("Send", "Sending now.", index=9),
        # Deliberately provided: if the loop keeps going it will consume these, and the
        # assertions below will show it did.
        acts("Click", "Looking for the compose button.", index=9),
        acts("Complete", "done", success=True, reason="done"),
    ]
    final, surface = await _run(script, "send-1")

    assert final["success"] is True
    assert final["status"] == "done"
    assert surface.verbs.count("Send") == 1
    assert "Click" not in surface.verbs, "the run continued after the mail had gone"


async def test_the_email_is_never_sent_twice():
    """THE property. A second send is not a wasted turn, it is a second email."""
    script = [
        *PRELUDE,
        acts("Send", "Sending.", index=9),
        acts("Send", "Sending again.", index=9),
        acts("Complete", "done", success=True, reason="done"),
    ]
    _final, surface = await _run(script, "send-2")

    assert surface.verbs.count("Send") == 1


async def test_a_FAILED_send_does_not_finish_the_run():
    """The counterfactual. A send that did not go through still needs the loop — to report
    it, or to try something else. Only success ends things."""
    script = [
        *PRELUDE,
        acts("Send", "Sending.", index=9),
        acts("Complete", "could not send", success=False, reason="send failed"),
    ]
    failed = ActionResult(
        success=False, reason="compose still open", error_code="SEND_NOT_CONFIRMED"
    )
    final, _surface = await _run(script, "send-3", results=[failed])

    assert final["success"] is False


async def test_a_reversible_action_does_not_finish_the_run():
    """Archiving one newsletter is not the end of a triage run."""
    script = [
        intake("archive", selector="the newsletter"),
        ok("decision"),
        ok("Archive it"),
        acts("Archive", "That is the newsletter.", index=1),
        acts("Complete", "archived", success=True, reason="archived"),
    ]
    surface = FakeEmailSurface([observation(Element(index=1, role="row", name="Newsletter"))])
    graph = build_manager_graph(
        llm=FakeLLMClient(script), surface=surface, rules=NoRules(), max_steps=12
    )
    config = {"configurable": {"thread_id": "send-4"}}
    final = await graph.ainvoke({"task": "archive it", "thread_id": "send-4"}, config)

    # It reached Complete on its own rather than being cut short after Archive.
    assert final["success"] is True
    assert surface.verbs == ["Archive"]


def test_the_send_verb_is_recognised_as_irreversible():
    """The predicate the completion rule turns on."""
    assert is_irreversible(ActionCall(name="Send", args={"index": 9}), compose_view())


def test_the_playwright_surface_has_a_handler_for_every_gated_verb():
    """`_perform` dispatches to `_do_<verb>`. A gated verb with no handler fails with
    VERB_NOT_BOUND *after* a human approved it — which is exactly what happened to `Send`."""
    from app.surface.playwright_surface import PlaywrightEmailSurface
    from app.workers.irreversible import GATED_VERBS

    missing = [
        verb
        for verb in GATED_VERBS
        if not hasattr(PlaywrightEmailSurface, f"_do_{verb.lower()}")
    ]
    assert "Send" not in missing, "Send must be performable once it has been approved"
