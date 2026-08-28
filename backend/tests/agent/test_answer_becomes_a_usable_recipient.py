"""Answering the gate's question must produce a recipient the system can actually send to.

**The 100%-context rule worked; the answer was then thrown away.** Asked to "write a good
evening with short motivation email" — no recipient anywhere — the gate correctly refused to
start and asked who it was for. The human typed an address. It went into
`recipient_identity` RAW, and a raw address is not something this system can send to: the
dispatcher only ever accepts vault tokens (`UNKNOWN_TOKEN` otherwise). So the worker was
handed a recipient it could not use, typed it as literal text into the To box, and produced
loose text with no chip.

Intake has always tokenized addresses in the TASK. The gate never tokenized the ANSWER — so
asking the question worked and answering it did not.
"""
from __future__ import annotations

import json

from langgraph.types import Command

from app.agent.graph import build_manager_graph
from app.llm.base import LLMResult
from app.rules.store import NoRules
from app.security.vault import SessionPiiVault
from tests.fakes.fake_llm import FakeLLMClient, drafted, ok


def intake(action: str, **slots) -> LLMResult:
    return ok(json.dumps({"action": action, "slots": slots, "confidence": 0.95}))


async def test_an_address_given_as_an_ANSWER_becomes_an_addressable_token():
    """THE regression, end to end through the real graph."""
    vault = SessionPiiVault()
    llm = FakeLLMClient(
        [
            # No recipient anywhere — exactly the task that triggered this.
            intake("send_email", body_intent="short motivation email"),
            ok("decision"),
            ok("Compose\nSend"),
            drafted(),
        ]
    )
    graph = build_manager_graph(llm=llm, rules=NoRules(), vault=vault)
    config = {"configurable": {"thread_id": "ans-1"}}

    paused = await graph.ainvoke(
        {"task": "write a good evening with short motivation email", "thread_id": "ans-1"},
        config,
    )
    assert "__interrupt__" in paused, "the gate must refuse to start with no recipient"
    assert paused["__interrupt__"][0].value["missing"] == ["recipient_identity"]

    final = await graph.ainvoke(Command(resume="samadrita@corp.com"), config)

    recipient = final["intent"].slots["recipient_identity"]
    assert "samadrita@corp.com" not in recipient, "a raw address reached the worker"
    assert vault.resolve(recipient) == "samadrita@corp.com"
    assert vault.is_addressable(recipient), "the dispatcher would refuse a non-addressable token"


async def test_the_gate_still_refuses_to_start_without_a_recipient():
    """The rule this all rests on. Nothing below matters if the run starts half-informed."""
    llm = FakeLLMClient([intake("send_email", body_intent="short motivation email")])
    graph = build_manager_graph(llm=llm, rules=NoRules(), vault=SessionPiiVault())

    result = await graph.ainvoke(
        {"task": "write a good evening email", "thread_id": "ans-2"},
        {"configurable": {"thread_id": "ans-2"}},
    )

    assert "__interrupt__" in result
    # Exactly one call: intake. A router call here would mean the gate had been bypassed.
    assert llm.call_count == 1


async def test_an_answer_with_no_address_is_untouched():
    """Most answers name nobody — "the Friday demo" must survive verbatim."""
    vault = SessionPiiVault()
    llm = FakeLLMClient(
        [
            intake("send_email", recipient_identity="P1"),
            ok("decision"),
            ok("Compose\nSend"),
            drafted(),
        ]
    )
    graph = build_manager_graph(llm=llm, rules=NoRules(), vault=vault)
    config = {"configurable": {"thread_id": "ans-3"}}

    await graph.ainvoke({"task": "email P1", "thread_id": "ans-3"}, config)
    final = await graph.ainvoke(Command(resume="the Friday demo"), config)

    assert final["intent"].slots["topic"] == "the Friday demo"


async def test_a_run_with_no_vault_still_completes():
    """Not every caller has a session — a missing vault must not crash the gate."""
    llm = FakeLLMClient(
        [
            intake("send_email", recipient_identity="P1"),
            ok("decision"),
            ok("Compose\nSend"),
            drafted(),
        ]
    )
    graph = build_manager_graph(llm=llm, rules=NoRules())
    config = {"configurable": {"thread_id": "ans-4"}}

    await graph.ainvoke({"task": "email P1", "thread_id": "ans-4"}, config)
    final = await graph.ainvoke(Command(resume="the demo"), config)

    assert final["status"] == "done"
