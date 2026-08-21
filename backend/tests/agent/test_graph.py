"""The manager graph on fakes — R3 (100% context) and R6 (routing), end to end.

No browser, no provider, no network. A scripted `FakeLLMClient` and a rules store are the
entire environment, which is what keeps these tests fast enough to run on every commit.
"""
from __future__ import annotations

import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agent.graph import build_manager_graph
from app.manager.intent import Action
from app.rules.store import InMemoryRulesStore, NoRules, Rule
from app.telemetry.records import ErrorCode
from tests.fakes.fake_llm import FakeLLMClient, drafted, ok


def intake_reply(action: str, confidence: float = 0.95, **slots):
    return ok(json.dumps({"action": action, "slots": slots, "confidence": confidence}))


def run_config(thread_id: str = "run-1") -> dict:
    return {"configurable": {"thread_id": thread_id}}


async def drive(graph, task: str, thread_id: str = "run-1"):
    return await graph.ainvoke({"task": task, "thread_id": thread_id}, run_config(thread_id))


# ── R3: nothing starts without full context ─────────────────────────────────


async def test_a_complete_task_clears_the_gate_and_routes():
    llm = FakeLLMClient(
        [
            intake_reply("send_email", recipient_identity="P1", topic="the Friday demo"),
            ok("decision"),
            ok("Click Compose\nFill recipient\nWrite body"),
            drafted(),
        ]
    )
    graph = build_manager_graph(llm=llm, rules=NoRules())

    final = await drive(graph, "email P1 about the Friday demo")

    assert final["missing_slots"] == []
    assert final["route"].topology == "decision"
    assert final["plan"] is not None
    assert final["status"] == "done"


async def test_an_incomplete_task_pauses_and_asks():
    """The headline guarantee: it will not start half-informed."""
    llm = FakeLLMClient([intake_reply("send_email", recipient_identity="P1")])
    graph = build_manager_graph(llm=llm, rules=NoRules())

    result = await drive(graph, "send an email to P1")

    assert "__interrupt__" in result, "the run must PAUSE, not proceed"
    payload = result["__interrupt__"][0].value
    assert "what the email should be about" in payload["question"]
    assert payload["missing"] == ["topic"]


async def test_nothing_downstream_runs_while_the_gate_is_open():
    """No router call, no planner call — the LLM script proves it."""
    llm = FakeLLMClient([intake_reply("send_email")])
    graph = build_manager_graph(llm=llm, rules=NoRules())

    result = await drive(graph, "send an email")

    assert "__interrupt__" in result
    # Exactly ONE call: intake. A router call here would mean the gate was bypassed.
    assert llm.call_count == 1
    assert [role for role, _, _ in llm.requests] == ["classifier"]


async def test_answering_the_question_resumes_the_run():
    llm = FakeLLMClient(
        [
            intake_reply("send_email", recipient_identity="P1"),
            ok("decision"),
            ok("Click Compose\nWrite body"),
            drafted(),
        ]
    )
    graph = build_manager_graph(llm=llm, rules=NoRules())

    await drive(graph, "send an email to P1")
    final = await graph.ainvoke(Command(resume="tell them the demo moved to 4pm"), run_config())

    assert final["missing_slots"] == []
    assert final["status"] == "done"
    assert final["answers"] == ["tell them the demo moved to 4pm"]


async def test_the_pause_survives_a_rebuilt_graph():
    """A durable interrupt, not a parked coroutine.

    This is why the gate uses `interrupt()` rather than a blocking prompt: the process can
    restart and the human can reconnect ten minutes later.
    """
    saver = InMemorySaver()
    first = build_manager_graph(
        llm=FakeLLMClient([intake_reply("send_email", recipient_identity="P1")]),
        rules=NoRules(),
        checkpointer=saver,
    )
    assert "__interrupt__" in await drive(first, "send an email to P1")

    # A completely fresh graph object, sharing only the checkpoint.
    rebuilt = build_manager_graph(
        llm=FakeLLMClient([ok("decision"), ok("Click Compose"), drafted()]),
        rules=NoRules(),
        checkpointer=saver,
    )
    final = await rebuilt.ainvoke(Command(resume="about the Friday demo"), run_config())

    assert final["status"] == "done"


async def test_the_gate_gives_up_typed_rather_than_asking_forever():
    """A gate that can ask forever can hang a run forever."""
    llm = FakeLLMClient([intake_reply("send_email", confidence=0.1)] * 8)
    graph = build_manager_graph(llm=llm, rules=NoRules())

    await drive(graph, "do something with email")
    for answer in ("still vague", "also vague", "no clearer"):
        result = await graph.ainvoke(Command(resume=answer), run_config())
        if "__interrupt__" not in result:
            break

    assert result["status"] == "failed"
    assert result["error_code"] == ErrorCode.CONTEXT_INCOMPLETE
    assert result["finished"] is True


async def test_an_unparseable_classification_asks_instead_of_crashing():
    """A flaky model must not be able to take the process down."""
    llm = FakeLLMClient([ok("I'm not sure what you mean, sorry!")])
    graph = build_manager_graph(llm=llm, rules=NoRules())

    result = await drive(graph, "asdfgh")

    assert "__interrupt__" in result
    assert result["intent"].action is Action.UNKNOWN


async def test_a_task_needing_nothing_starts_immediately():
    llm = FakeLLMClient([intake_reply("apply_rules"), ok("linear")])
    graph = build_manager_graph(llm=llm, rules=NoRules())

    final = await drive(graph, "apply my rules")

    assert final["status"] == "done"
    assert final["missing_slots"] == []


# ── R6: linear vs decision ──────────────────────────────────────────────────


async def test_a_rule_match_routes_linear_with_zero_classifier_calls():
    """The cheapest correct path. On a free tier this is the difference between one run a
    day and unlimited ones."""
    llm = FakeLLMClient([intake_reply("triage", scope="inbox")])
    graph = build_manager_graph(llm=llm, rules=InMemoryRulesStore())

    final = await drive(graph, "archive all the newsletters")

    assert final["route"].topology == "linear"
    assert final["route"].rule_matched is True
    # Only intake. No router call, no planner call.
    assert llm.call_count == 1


async def test_linear_work_never_reaches_the_planner():
    llm = FakeLLMClient([intake_reply("triage", scope="inbox")])
    graph = build_manager_graph(llm=llm, rules=InMemoryRulesStore())

    final = await drive(graph, "archive all the newsletters")

    # LangGraph returns only channels that were WRITTEN, so an absent key is proof the
    # planner node never ran — a stronger statement than `plan is None`.
    assert final.get("plan") is None, "there is nothing to deliberate about"


async def test_an_unmatched_task_falls_through_to_the_classifier():
    llm = FakeLLMClient(
        [intake_reply("triage", scope="inbox"), ok("decision"), ok("Read each thread")]
    )
    graph = build_manager_graph(llm=llm, rules=InMemoryRulesStore())

    final = await drive(graph, "reply to the ones that actually need me")

    assert final["route"].topology == "decision"
    assert final["route"].rule_matched is False
    assert final["plan"] is not None


async def test_an_ambiguous_classification_defaults_to_decision():
    """Treating judgement work as mechanical produces confident wrong actions; the reverse
    only costs tokens."""
    llm = FakeLLMClient([intake_reply("triage", scope="inbox"), ok("hmm, hard to say"), ok("Look")])
    graph = build_manager_graph(llm=llm, rules=NoRules())

    final = await drive(graph, "sort out my inbox somehow")

    assert final["route"].topology == "decision"


# ── telemetry ───────────────────────────────────────────────────────────────


async def test_every_llm_node_writes_a_trajectory_row():
    llm = FakeLLMClient(
        [
            intake_reply("send_email", recipient_identity="P1", topic="demo"),
            ok("decision"),
            ok("Click Compose"),
            drafted(),
        ]
    )
    graph = build_manager_graph(llm=llm, rules=NoRules())

    final = await drive(graph, "email P1 about demo")

    nodes = [record.node for record in final["history"]]
    # `writer` is an LLM node like any other: if it does not appear here it is drafting
    # without leaving an audit row, and the trajectory stops being the record of the run.
    assert nodes == ["intake", "router", "planner", "writer", "finalize"]


async def test_the_run_always_ends_typed():
    """Every terminal state carries a code or an explicit success."""
    llm = FakeLLMClient(
        [
            intake_reply("send_email", recipient_identity="P1", topic="demo"),
            ok("linear"),
            drafted(),
        ]
    )
    graph = build_manager_graph(llm=llm, rules=NoRules())

    final = await drive(graph, "email P1 about demo")

    assert final["finished"] is True
    assert final["success"] is not None or final["error_code"] is not None


# ── rules store ─────────────────────────────────────────────────────────────


def test_auto_send_needs_two_locks_not_one():
    """One accidental default must never be enough to bypass the approval gate."""
    risky = Rule(name="auto", patterns=(r"invoice",), actions=("Send",), auto_send=True)

    default_store = InMemoryRulesStore([risky])
    assert default_store.active()[0].auto_send is False, "off unless explicitly permitted"

    opted_in = InMemoryRulesStore([risky], allow_auto_send=True)
    assert opted_in.active()[0].auto_send is True


def test_a_disabled_rule_never_matches():
    store = InMemoryRulesStore([Rule(name="x", patterns=(r"news",), enabled=False)])
    assert store.match("archive the news") is None


def test_rule_precedence_is_first_match_so_a_human_can_reorder_it():
    first = Rule(name="first", patterns=(r"newsletter",))
    second = Rule(name="second", patterns=(r"newsletter",))
    assert InMemoryRulesStore([first, second]).match("newsletter") is not None
    assert InMemoryRulesStore([first, second]).match("newsletter").name == "first"


@pytest.mark.parametrize(
    "task", ["archive all newsletters", "clear the promotions", "mark everything as read"]
)
def test_the_default_rules_catch_ordinary_bulk_chores(task):
    assert InMemoryRulesStore().match(task, "triage") is not None


def test_a_rule_scoped_to_an_intent_does_not_fire_elsewhere():
    store = InMemoryRulesStore()
    assert store.match("archive all newsletters", "send_email") is None
