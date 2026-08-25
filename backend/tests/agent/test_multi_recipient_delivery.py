"""End to end: "email P1 and P2" through the real graph, gate, and worker prompt.

Everything else in this feature (`test_conditional_slots.py`, `test_delivery_instruction.py`)
tests one layer in isolation. This drives the actual `build_manager_graph` the way
`api/ws.py` does, so it is the one place that proves the whole chain actually connects:
intake fills two tokens -> the gate asks ONE extra question, batched correctly with nothing
else -> the human's free-text answer resolves it -> the worker's own prompt states the
decision as an instruction, never as a decision left for it to make.
"""
from __future__ import annotations

import json

from langgraph.types import Command

from app.agent.graph import build_manager_graph
from app.llm.base import LLMResult, ToolCall
from app.rules.store import NoRules
from tests.fakes.fake_llm import FakeLLMClient, drafted, ok
from tests.fakes.fake_surface import FakeEmailSurface, observation


def intake(action: str, confidence: float = 0.95, **slots) -> LLMResult:
    return ok(json.dumps({"action": action, "slots": slots, "confidence": confidence}))


def acts(name: str, text: str, **args) -> LLMResult:
    return LLMResult(
        text=text, tool_calls=[ToolCall(id=name, name=name, args=args)], provider="fake"
    )


def run_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


async def test_two_recipients_pause_for_exactly_one_extra_question():
    llm = FakeLLMClient(
        [intake("send_email", recipient_identity="P1, P2", topic="the Friday demo")]
    )
    graph = build_manager_graph(llm=llm, rules=NoRules())

    result = await graph.ainvoke(
        {"task": "email P1 and P2 about the Friday demo", "thread_id": "multi-1"},
        run_config("multi-1"),
    )

    assert "__interrupt__" in result, "recipient count is > 1, so delivery mode must be asked"
    payload = result["__interrupt__"][0].value
    assert payload["missing"] == ["delivery_mode"]
    assert "one email to everyone" in payload["question"]
    assert "unless you say separately" in payload["question"]
    # Exactly one call: intake. Neither the router nor the planner ran while the gate was
    # still open — the same guarantee `test_graph.py` already pins for a missing topic.
    assert llm.call_count == 1


async def test_answering_separately_reaches_the_worker_as_an_instruction():
    llm = FakeLLMClient(
        [
            intake("send_email", recipient_identity="P1, P2", topic="the Friday demo"),
            ok("decision"),
            ok("Open compose\nSend to each separately"),
            drafted(),
            acts(
                "Complete",
                "Composed and sent the first; the human still needs to approve each.",
                success=True,
                reason="drafted",
            ),
        ]
    )
    surface = FakeEmailSurface([observation(title="Inbox")])
    graph = build_manager_graph(llm=llm, surface=surface, rules=NoRules(), max_steps=3)
    config = run_config("multi-2")

    await graph.ainvoke(
        {"task": "email P1 and P2 about the Friday demo", "thread_id": "multi-2"}, config
    )
    await graph.ainvoke(Command(resume="send them separately"), config)

    reason_calls = [msgs for role, msgs, _tools in llm.requests if role == "executor"]
    assert reason_calls, "the reason node never ran"
    worker_saw = "\n".join(m.content for m in reason_calls[-1])

    assert "2 people SEPARATELY" in worker_saw
    assert "1. P1" in worker_saw and "2. P2" in worker_saw
    # The raw answer must not also appear verbatim as a slot line for the model to
    # re-interpret itself — it already got the resolved instruction above.
    assert "delivery_mode: send them separately" not in worker_saw


async def test_answering_together_reaches_the_worker_as_one_field():
    llm = FakeLLMClient(
        [
            intake("send_email", recipient_identity="P1, P2", topic="the Friday demo"),
            ok("decision"),
            ok("Open compose\nSend one email"),
            drafted(),
            acts("Complete", "Draft ready.", success=True, reason="drafted"),
        ]
    )
    surface = FakeEmailSurface([observation(title="Inbox")])
    graph = build_manager_graph(llm=llm, surface=surface, rules=NoRules(), max_steps=3)
    config = run_config("multi-3")

    await graph.ainvoke(
        {"task": "email P1 and P2 about the Friday demo", "thread_id": "multi-3"}, config
    )
    await graph.ainvoke(Command(resume="just one email is fine"), config)

    reason_calls = [msgs for role, msgs, _tools in llm.requests if role == "executor"]
    worker_saw = "\n".join(m.content for m in reason_calls[-1])

    assert "2 people TOGETHER" in worker_saw
    assert "P1, P2" in worker_saw


async def test_a_single_recipient_never_asks_about_delivery_mode():
    llm = FakeLLMClient(
        [
            intake("send_email", recipient_identity="P1", topic="the Friday demo"),
            ok("decision"),
            ok("Open compose\nSend"),
            drafted(),
        ]
    )
    graph = build_manager_graph(llm=llm, rules=NoRules())

    result = await graph.ainvoke(
        {"task": "email P1 about the Friday demo", "thread_id": "multi-4"}, run_config("multi-4")
    )

    assert "__interrupt__" not in result
    assert result["status"] == "done"
