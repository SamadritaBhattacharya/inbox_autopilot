"""The gate re-reads the whole conversation when an answer arrives.

**The bug this closes.** An answer used to be copied VERBATIM into every slot still
missing, because raw text is not a slot assignment and the gate had no way to split it.
Asked "who is it for, and what about?", a human answering "to Biyash about the demo" got
`recipient_identity = "to Biyash about the demo"` AND `topic = "to Biyash about the demo"`
— a recipient that is a sentence, and a topic that names a person. Both wrong, both then
acted on.

The fix is one classifier call that re-reads task-plus-answers together, merged under
rules that make it safe to be wrong:

  * a slot the HUMAN filled is never overwritten by a re-reading;
  * the action survives unless the human actually declined it;
  * a provider failure degrades to the old raw-fill rather than taking down a gate the
    human is mid-conversation with.

And it is not spent when it cannot buy anything: a bare "yes" carries nothing to place.
"""
from __future__ import annotations

import json

from langgraph.types import Command

from app.agent.graph import build_manager_graph
from app.llm.base import LLMResult, ProviderError
from app.manager.intent import Action, TaskIntent, merge_intent
from app.rules.store import NoRules
from tests.fakes.fake_llm import FakeLLMClient, drafted, ok


def intake(action: str, confidence: float = 0.9, **slots) -> LLMResult:
    return ok(json.dumps({"action": action, "slots": slots, "confidence": confidence}))


def config(thread: str) -> dict:
    return {"configurable": {"thread_id": thread}}


def reread_calls(llm: FakeLLMClient) -> int:
    """Intake-prompted calls only — the router shares the `classifier` role."""
    return sum(
        1
        for _, messages, _ in llm.requests
        if messages and messages[0].content.startswith("You convert an email request")
    )


async def test_a_compound_answer_is_split_across_the_slots_it_names():
    """THE regression: one sentence, two slots, each getting its own part."""
    llm = FakeLLMClient(
        [
            # Nothing given, so BOTH recipient and topic are outstanding.
            intake("send_email"),
            # The re-read of "send an email" + "to Biyash about the demo".
            intake("send_email", recipient_identity="Biyash", topic="the demo"),
            ok("decision"),
            ok("Compose\nSend"),
            drafted(),
        ]
    )
    graph = build_manager_graph(llm=llm, rules=NoRules())

    paused = await graph.ainvoke(
        {"task": "send an email", "thread_id": "reread-1"}, config("reread-1")
    )
    assert sorted(paused["__interrupt__"][0].value["missing"]) == ["recipient_identity", "topic"]

    resumed = await graph.ainvoke(Command(resume="to Biyash about the demo"), config("reread-1"))

    slots = resumed["intent"].slots
    assert slots["recipient_identity"] == "Biyash"
    assert slots["topic"] == "the demo"
    assert "about" not in slots["recipient_identity"], "the sentence leaked into the recipient"


async def test_a_reread_never_overwrites_a_slot_the_human_gave():
    """Evidence from a human outranks a second opinion from the model.

    The re-read sees the whole conversation and may re-derive a slot the human already
    stated outright. If a model guess could overwrite that, this feature would introduce a
    worse failure than the one it fixes — silently retargeting a recipient the human named.
    """
    existing = TaskIntent(
        action=Action.SEND_EMAIL,
        slots={"recipient_identity": "P1", "topic": "the demo"},
        action_confidence=0.9,
    )
    fresh = TaskIntent(
        action=Action.ARCHIVE,
        slots={"recipient_identity": "someone else", "topic": "unrelated", "tone": "warm"},
        action_confidence=0.99,
    )

    merged = merge_intent(existing, fresh, declined=False)

    assert merged.slots["recipient_identity"] == "P1"
    assert merged.slots["topic"] == "the demo"
    assert merged.action is Action.SEND_EMAIL, "a re-read must not silently change the action"
    # New slots the human never spoke to are still welcome.
    assert merged.slots["tone"] == "warm"


async def test_a_decline_lets_the_reread_replace_the_action():
    """"No, archive them instead" is the one case where the action SHOULD move."""
    existing = TaskIntent(action=Action.SEND_EMAIL, slots={}, action_confidence=0.9)
    fresh = TaskIntent(action=Action.ARCHIVE, slots={}, action_confidence=0.9)

    merged = merge_intent(existing, fresh, declined=True)

    assert merged.action is Action.ARCHIVE
    assert not merged.action_confirmed, "a decline cannot leave the old action grounded"


async def test_a_failed_reread_degrades_to_the_raw_answer():
    """A provider outage must not break a gate the human is actively talking to."""
    llm = FakeLLMClient(
        [
            intake("send_email"),
            ProviderError("groq", "quota exhausted"),
            ok("decision"),
            ok("Compose\nSend"),
            drafted(),
        ]
    )
    graph = build_manager_graph(llm=llm, rules=NoRules())

    await graph.ainvoke({"task": "send an email", "thread_id": "reread-2"}, config("reread-2"))
    resumed = await graph.ainvoke(Command(resume="to Biyash about the demo"), config("reread-2"))

    # Old behaviour: the answer fills what is missing. Coarse, but the run survives.
    slots = resumed["intent"].slots
    assert slots["recipient_identity"] == "to Biyash about the demo"
    assert slots["topic"] == "to Biyash about the demo"


async def test_a_bare_yes_costs_no_classifier_call():
    """The cost guard. A "yes" cannot move a slot, so re-reading to learn that is waste.

    Free-tier quota is the real constraint (CLAUDE.md §14), and confirmations are the most
    common answer there is — one call each would be a permanent tax for a known result.
    """
    llm = FakeLLMClient(
        [
            intake("send_email", confidence=0.8, recipient_identity="P1", topic="the demo"),
            ok("decision"),
            ok("Compose\nSend"),
            drafted(),
        ]
    )
    graph = build_manager_graph(llm=llm, rules=NoRules())

    paused = await graph.ainvoke(
        {"task": "email P1 about the demo", "thread_id": "reread-3"}, config("reread-3")
    )
    # Nothing is missing; the gate is asking about the ACTION.
    assert paused["__interrupt__"][0].value["missing"] == []
    before = reread_calls(llm)

    final = await graph.ainvoke(Command(resume="yes"), config("reread-3"))

    assert reread_calls(llm) == before, "a bare confirmation must not spend a re-read"
    assert final["intent"].action_confirmed


async def test_a_correction_after_confirmation_is_reread():
    """"No, send it to Biyash instead" is not a bare no — the correction has to be read."""
    llm = FakeLLMClient(
        [
            intake("send_email", confidence=0.8, recipient_identity="P1", topic="the demo"),
            intake("send_email", confidence=0.9, recipient_identity="Biyash", topic="the demo"),
            ok("decision"),
            ok("Compose\nSend"),
            drafted(),
        ]
    )
    graph = build_manager_graph(llm=llm, rules=NoRules())

    await graph.ainvoke(
        {"task": "email P1 about the demo", "thread_id": "reread-4"}, config("reread-4")
    )
    before = reread_calls(llm)

    await graph.ainvoke(Command(resume="no, send it to Biyash instead"), config("reread-4"))

    assert reread_calls(llm) == before + 1, "the correction was never re-read"
