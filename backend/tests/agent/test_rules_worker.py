"""The linear route — R6's payoff: deterministic work at ZERO model calls."""
from __future__ import annotations

import json

from inbox_contracts import ActionResult, Element

from app.agent.graph import build_manager_graph
from app.llm.base import LLMResult, ToolCall
from app.rules.store import InMemoryRulesStore, Rule
from app.telemetry.records import ErrorCode
from app.workers.rules_worker import ALLOWED_RULE_VERBS, MAX_STALLS, matching_elements
from tests.fakes.fake_llm import FakeLLMClient, ok
from tests.fakes.fake_surface import FakeEmailSurface, observation


def intake(action: str, **slots):
    return ok(json.dumps({"action": action, "slots": slots, "confidence": 0.95}))


def rows(*names: str) -> observation:
    return observation(
        *[Element(index=i + 1, role="listitem", name=name) for i, name in enumerate(names)]
    )


NEWSLETTERS = Rule(
    name="newsletters",
    patterns=(r"newsletter", r"unsubscribe"),
    actions=("Archive",),
    intents=("triage", "archive"),
)


# ── matching ────────────────────────────────────────────────────────────────


def test_matching_is_case_insensitive():
    page = rows("Your Weekly NEWSLETTER", "Q3 numbers")
    assert [e.index for e in matching_elements(page, NEWSLETTERS)] == [1]


def test_non_matching_rows_are_left_alone():
    page = rows("Friday demo moved to 4pm", "Q3 numbers")
    assert matching_elements(page, NEWSLETTERS) == []


def test_buttons_are_never_treated_as_mail():
    """Otherwise a rule for "unsubscribe" would archive the Unsubscribe button."""
    page = observation(Element(index=1, role="button", name="Unsubscribe"))
    assert matching_elements(page, NEWSLETTERS) == []


def test_a_gated_verb_can_never_be_a_rule_action():
    """A deterministic rule that could send mail would be the one path around the gate."""
    assert "Send" not in ALLOWED_RULE_VERBS
    assert "DeleteForever" not in ALLOWED_RULE_VERBS
    assert "SendInvite" not in ALLOWED_RULE_VERBS


# ── through the graph ───────────────────────────────────────────────────────


def linear_run(surface: FakeEmailSurface, thread: str, *, rules=None):
    llm = FakeLLMClient([intake("triage", scope="inbox")])
    graph = build_manager_graph(
        llm=llm,
        surface=surface,
        rules=rules or InMemoryRulesStore([NEWSLETTERS]),
        max_steps=30,
    )
    return graph, {"configurable": {"thread_id": thread}}, llm


class ShrinkingInbox(FakeEmailSurface):
    """A mailbox that actually changes: an archived row disappears, and the rest renumber."""

    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self._names = list(names)

    async def observe(self):
        self.observe_count += 1
        return rows(*self._names)

    async def act(self, call):
        self.calls.append(call)
        index = call.args.get("index")
        if isinstance(index, int) and 1 <= index <= len(self._names):
            self._names.pop(index - 1)
            return ActionResult(success=True, reason=f"archived [{index}]")
        return ActionResult(success=False, reason="no such row", error_code="STALE_INDEX")


async def test_a_rule_run_costs_exactly_one_model_call():
    """One call to understand the request. None to do the work."""
    surface = ShrinkingInbox(["Weekly newsletter", "Friday demo", "Shop newsletter"])
    graph, config, llm = linear_run(surface, "lin-1")

    final = await graph.ainvoke(
        {"task": "archive all the newsletters", "thread_id": "lin-1"}, config
    )

    assert llm.call_count == 1, "intake only — the router matched a rule and nothing reasoned"
    assert final["success"] is True
    assert surface.verbs == ["Archive", "Archive"]


async def test_it_leaves_everything_that_does_not_match():
    surface = ShrinkingInbox(["Weekly newsletter", "Friday demo", "Q3 numbers"])
    graph, config, _ = linear_run(surface, "lin-2")

    await graph.ainvoke({"task": "archive all the newsletters", "thread_id": "lin-2"}, config)

    assert surface._names == ["Friday demo", "Q3 numbers"]


async def test_it_re_observes_between_actions():
    """Archiving row 3 renumbers everything below it, so a list collected up front is stale."""
    surface = ShrinkingInbox(["a newsletter", "b newsletter"])
    graph, config, _ = linear_run(surface, "lin-3")

    await graph.ainvoke({"task": "archive all the newsletters", "thread_id": "lin-3"}, config)

    assert surface.observe_count > len(surface.calls)
    assert all(call.args["index"] == 1 for call in surface.calls)


async def test_an_empty_inbox_succeeds_quietly():
    surface = ShrinkingInbox(["Friday demo"])
    graph, config, _ = linear_run(surface, "lin-4")

    final = await graph.ainvoke(
        {"task": "archive all the newsletters", "thread_id": "lin-4"}, config
    )

    assert final["success"] is True
    assert surface.calls == []


async def test_actions_that_never_land_stop_rather_than_loop_forever():
    """A free-of-charge infinite loop is still an infinite loop."""

    class Deaf(FakeEmailSurface):
        async def observe(self):
            return rows("Weekly newsletter")

        async def act(self, call):
            self.calls.append(call)
            return ActionResult(success=False, reason="nothing happened")

    surface = Deaf()
    graph, config, _ = linear_run(surface, "lin-5")

    final = await graph.ainvoke(
        {"task": "archive all the newsletters", "thread_id": "lin-5"}, config
    )

    assert final["success"] is False
    assert final["error_code"] == ErrorCode.STUCK
    assert len(surface.calls) <= MAX_STALLS + 1


async def test_a_rule_with_no_permitted_action_fails_typed():
    sneaky = Rule(name="sneaky", patterns=(r"invoice",), actions=("Send",), intents=("triage",))
    surface = ShrinkingInbox(["an invoice"])
    graph, config, _ = linear_run(surface, "lin-6", rules=InMemoryRulesStore([sneaky]))

    final = await graph.ainvoke({"task": "handle the invoice", "thread_id": "lin-6"}, config)

    assert final["success"] is False
    assert final["error_code"] == ErrorCode.NO_ACTION
    assert surface.never_dispatched("Send")


async def test_decision_work_still_uses_the_reasoning_loop():
    """The linear path must not swallow work that needs judgement."""
    from app.rules.store import NoRules

    surface = ShrinkingInbox(["Friday demo"])
    llm = FakeLLMClient(
        [
            intake("triage", scope="inbox"),
            ok("decision"),
            ok("Read each thread"),
            # The reasoning turn itself — the point of the test is that this is REACHED.
            LLMResult(
                text="Nothing here needs a reply.",
                tool_calls=[
                    ToolCall(id="c", name="Complete", args={"success": True, "reason": "none"})
                ],
                provider="fake",
            ),
        ]
    )
    graph = build_manager_graph(llm=llm, surface=surface, rules=NoRules(), max_steps=4)

    await graph.ainvoke(
        {"task": "reply to the ones that need me", "thread_id": "lin-7"},
        {"configurable": {"thread_id": "lin-7"}},
    )

    assert llm.call_count > 1, "a judgement task must reach the reasoning loop"
