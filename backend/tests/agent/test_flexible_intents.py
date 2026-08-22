"""Read-only requests: the flexible half of the system.

A mailbox request is more often a question than a command. These prove that asking one is
cheap (little to clarify) and safe (no capability to change anything), and that an
unrecognised request degrades into a read-only investigation rather than an interrogation.
"""
from __future__ import annotations

import json

import pytest

from app.agent.graph import build_manager_graph
from app.manager.intent import READ_ONLY_ACTIONS, Action, TaskIntent
from app.manager.slots import REQUIRED_SLOTS, apply_defaults, is_ready, missing_slots
from app.rules.store import NoRules
from app.workers.registry import QUERY, WORKER_FOR_ACTION, topology_for, worker_for
from app.workers.tools import QUERY_TOOLS, TOOLSETS, verb_names
from tests.fakes.fake_llm import FakeLLMClient, ok

MUTATING_VERBS = {"Archive", "MarkRead", "Label", "Snooze", "Send", "DeleteForever", "DraftReply"}


def intent(action: Action, confidence: float = 0.8, **slots) -> TaskIntent:
    return TaskIntent(action=action, slots=slots, action_confidence=confidence)


def intake_reply(action: str, confidence: float = 0.9, **slots):
    return ok(json.dumps({"action": action, "slots": slots, "confidence": confidence}))


# ── the read-only capability guarantee ──────────────────────────────────────


def test_the_query_toolset_contains_no_mutating_verb():
    """The capability half: there is nothing for an injected instruction to reach for."""
    assert verb_names(QUERY_TOOLS) & MUTATING_VERBS == set()


def test_every_read_only_action_binds_the_read_only_worker():
    for action in READ_ONLY_ACTIONS:
        assert worker_for(action).read_only is True


def test_an_unmapped_action_defaults_to_read_only():
    """The worst case for a misclassification must be a wasted look, not a mutation."""
    assert worker_for(Action.UNKNOWN) is QUERY
    assert worker_for(Action.UNKNOWN).read_only is True


def test_every_action_maps_somewhere_deliberate():
    for action in Action:
        if action is Action.UNKNOWN:
            continue
        assert action in WORKER_FOR_ACTION, f"{action} has no worker; it would default silently"


def test_mutating_workers_are_not_marked_read_only():
    assert worker_for(Action.TRIAGE).read_only is False
    assert worker_for(Action.SEND_EMAIL).read_only is False


# ── questions are cheap to ask ──────────────────────────────────────────────


def test_every_action_still_declares_a_slot_schema():
    for action in Action:
        assert action in REQUIRED_SLOTS, f"{action} has no schema; it would need nothing"


def test_summarize_my_inbox_needs_no_clarification():
    """Making the user say 'the inbox' is friction with no safety payoff."""
    assert missing_slots(intent(Action.SUMMARIZE)) == []
    assert is_ready(intent(Action.SUMMARIZE))


def test_count_defaults_to_the_inbox():
    assert apply_defaults(intent(Action.COUNT)).slots["scope"] == "inbox"


def test_a_search_still_needs_something_to_search_for():
    """Cheap is not the same as free — a search with no query is not answerable."""
    assert missing_slots(intent(Action.SEARCH)) == ["query"]
    assert missing_slots(intent(Action.SEARCH, query="from the bank")) == []


def test_read_only_work_clears_at_a_lower_confidence_bar():
    """A misread costs one wasted look; a misdirected send costs a relationship."""
    unsure = 0.6
    assert is_ready(intent(Action.ANSWER, unsure, query="what did Priya say"))
    assert not is_ready(
        intent(Action.SEND_EMAIL, unsure, recipient_identity="P1", topic="demo")
    )


def test_read_accepts_any_of_several_ways_to_point_at_mail():
    for slot in ("thread_ref", "selector", "query"):
        assert missing_slots(intent(Action.READ, **{slot: "the Friday demo one"})) == []


# ── end to end through the graph ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("task", "action"),
    [
        ("summarize my inbox", "summarize"),
        ("what did Priya say about the demo?", "answer"),
        ("find anything from the bank this week", "search"),
        ("how many unread do I have?", "count"),
        ("read the latest thread", "read"),
    ],
)
async def test_a_question_runs_without_being_interrogated(task, action):
    llm = FakeLLMClient(
        [intake_reply(action, query=task, thread_ref=task), ok("decision"), ok("Look")]
    )
    graph = build_manager_graph(llm=llm, rules=NoRules())

    final = await graph.ainvoke(
        {"task": task, "thread_id": "q"}, {"configurable": {"thread_id": "q"}}
    )

    assert "__interrupt__" not in final, f"{task!r} should not need clarification"
    assert final["active_worker"] == "query"


async def test_an_unfamiliar_email_request_investigates_rather_than_asking():
    """`answer` is the flexible fallback, and it is safe because it cannot mutate."""
    llm = FakeLLMClient(
        [
            intake_reply("answer", query="do I owe anyone a reply from last week"),
            ok("decision"),
            ok("Scan the inbox"),
        ]
    )
    graph = build_manager_graph(llm=llm, rules=NoRules())

    final = await graph.ainvoke(
        {"task": "do I owe anyone a reply from last week?", "thread_id": "u"},
        {"configurable": {"thread_id": "u"}},
    )

    assert final["intent"].action is Action.ANSWER
    assert final["active_worker"] == "query"


async def test_a_non_email_request_still_asks():
    """Flexible about email; not flexible about what it is for."""
    llm = FakeLLMClient([intake_reply("unknown", confidence=0.9)])
    graph = build_manager_graph(llm=llm, rules=NoRules())

    result = await graph.ainvoke(
        {"task": "book me a flight to Lisbon", "thread_id": "n"},
        {"configurable": {"thread_id": "n"}},
    )

    assert "__interrupt__" in result


async def test_a_mutating_request_still_gets_the_full_bar():
    """Widening read-only must not have loosened the gate for sends."""
    llm = FakeLLMClient([intake_reply("send_email", recipient_identity="P1")])
    graph = build_manager_graph(llm=llm, rules=NoRules())

    result = await graph.ainvoke(
        {"task": "email P1", "thread_id": "m"}, {"configurable": {"thread_id": "m"}}
    )

    assert "__interrupt__" in result


# ── the router cannot send a task somewhere it cannot run ───────────────────


class TestTopologyIsClamped:
    """Observed live: the router called "write a good evening mail to P1" *linear*.

    The linear path is the deterministic rules worker — no observe step, no perception at
    all. A compose task there has no way to find a Compose button, so it produced nothing
    and the run died as `NO_ACTION`, reported to the user as "the model stopped choosing
    actions". A routing mistake, blamed on the model.

    Whether a worker can run blind is a fact about the worker, so it is decided by the
    worker and not by a classifier's judgement.
    """

    @pytest.mark.parametrize(
        "action", [Action.SEND_EMAIL, Action.REPLY, Action.FORWARD, Action.EXTRACT_EVENT]
    )
    def test_writing_actions_are_forced_onto_the_decision_path(self, action):
        assert topology_for(action, "linear") == "decision"

    @pytest.mark.parametrize("action", sorted(READ_ONLY_ACTIONS, key=lambda a: a.value))
    def test_reading_actions_are_forced_onto_the_decision_path(self, action):
        """Answering a question about a mailbox needs to SEE the mailbox."""
        assert topology_for(action, "linear") == "decision"

    @pytest.mark.parametrize(
        "action", [Action.TRIAGE, Action.ARCHIVE, Action.LABEL, Action.SNOOZE, Action.APPLY_RULES]
    )
    def test_rule_shaped_actions_may_still_run_linearly(self, action):
        """The clamp must stay narrow: "archive all newsletters" genuinely needs no screen,
        and forcing it through the loop would spend model calls for nothing."""
        assert topology_for(action, "linear") == "linear"

    @pytest.mark.parametrize(
        "action", [Action.SEND_EMAIL, Action.TRIAGE, Action.SUMMARIZE]
    )
    def test_decision_is_never_downgraded(self, action):
        """The clamp only ever moves work TOWARDS more perception, never away from it."""
        assert topology_for(action, "decision") == "decision"


async def test_a_rule_match_cannot_drag_a_compose_task_onto_the_linear_path():
    """The same failure by a different route: a rule that fires on a compose task would
    send it somewhere with no perception loop."""
    from app.agent.graph import build_manager_graph
    from app.rules.store import InMemoryRulesStore

    llm = FakeLLMClient(
        [
            intake_reply("send_email", recipient_identity="P1", topic="the demo"),
            # The classifier is asked only when no rule matched; scripted either way so the
            # test asserts the CLAMP rather than which path reached it.
            ok("linear"),
            ok("Open compose"),
            ok('{"subject": "s", "body": "b", "tone": "warm"}'),
        ]
    )
    graph = build_manager_graph(llm=llm, rules=InMemoryRulesStore())

    final = await graph.ainvoke(
        {"task": "archive all the newsletters and email P1", "thread_id": "clamp-1"},
        {"configurable": {"thread_id": "clamp-1"}},
    )

    assert final["route"].topology == "decision"


# ── a read-only worker can still work a search box ─────────────────────────


class TestQueryCanNavigateAndSearch:
    """"Search for the invoice thread" needs a box filled and Enter pressed.

    `Type` was excluded from the read-only toolset along with the genuinely mutating verbs,
    which meant the query worker could click into a search box and then sit there unable to
    enter a character — failing a task it had been routed to do, for want of a keystroke.
    Typing changes nothing on its own; the read-only guarantee is about the MAILBOX.
    """

    def test_it_can_fill_a_search_box_and_submit(self):
        verbs = verb_names(QUERY_TOOLS)

        assert {"Click", "Type", "PressKey", "Clear"} <= verbs

    def test_it_still_cannot_change_the_mailbox(self):
        """The capability half of the read-only guarantee, unchanged: an injected
        instruction has nothing to reach for."""
        assert verb_names(QUERY_TOOLS) & MUTATING_VERBS == set()

    def test_it_can_reach_a_folder_by_clicking(self):
        """"Open the sent folder" is a click on a sidebar link, not a URL navigation. There
        is deliberately NO Navigate verb anywhere: an agent that can be talked into loading
        an arbitrary URL is a far larger hole than one that can only press what is on
        screen."""
        every_verb: set[str] = set()
        for tools in TOOLSETS.values():
            every_verb |= verb_names(tools)

        assert "Navigate" not in every_verb

    def test_a_click_on_send_is_still_refused_here(self):
        """Defence in depth. The query worker has Click, so it *could* press a Send button
        — and the dispatcher refuses it by CONSEQUENCE rather than by verb name."""
        from inbox_contracts import ActionCall

        from app.workers.irreversible import is_irreversible

        observation = observation_with_send()
        assert is_irreversible(ActionCall(name="Click", args={"index": 9}), observation)


def observation_with_send():
    from inbox_contracts import Element

    from tests.fakes.fake_surface import observation

    return observation(Element(index=9, role="button", name="Send (Ctrl-Enter)"))
