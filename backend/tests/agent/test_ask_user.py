"""`AskUser` must actually pause the run.

It was bound as a tool and then not handled, which is worse than not offering it at all:
the model called it, got "AskUser is not handled" back, reasoned at length about whether it
had called it wrongly, called it again, and burned its step budget on a tool that looked
available. A remediation strategy recommends this verb by name, so the gap was reachable by
design rather than by accident.

The scenario below is the one that exposed it: the agent lands on Gmail's sign-in wall,
cannot proceed, and needs a human.
"""
from __future__ import annotations

import inspect
import json

from inbox_contracts import Element
from langgraph.types import Command

from app.agent.graph import build_manager_graph
from app.llm.base import LLMResult, ToolCall
from app.rules.store import NoRules
from tests.fakes.fake_llm import FakeLLMClient, ok
from tests.fakes.fake_surface import FakeEmailSurface, observation


def acts(name: str, text: str, **args) -> LLMResult:
    return LLMResult(
        text=text, tool_calls=[ToolCall(id="c", name=name, args=args)], provider="fake"
    )


def intake(action: str, **slots):
    return ok(json.dumps({"action": action, "slots": slots, "confidence": 0.95}))


def signin_wall() -> FakeEmailSurface:
    return FakeEmailSurface(
        [observation(Element(index=1, role="button", name="Sign in"))]
    )


def asking_llm(*, after: list | None = None) -> FakeLLMClient:
    return FakeLLMClient(
        [
            intake("summarize", scope="inbox"),
            ok("decision"),
            ok("Read the inbox"),
            acts("AskUser", "I am on a sign-in screen and cannot proceed.",
                 question="Please sign in to Gmail, then tell me when to continue."),
            *(after or []),
        ]
    )


async def test_ask_user_pauses_the_run_instead_of_answering_itself():
    graph = build_manager_graph(
        llm=asking_llm(), surface=signin_wall(), rules=NoRules(), max_steps=12
    )
    config = {"configurable": {"thread_id": "ask-1"}}

    result = await graph.ainvoke({"task": "summarize my inbox", "thread_id": "ask-1"}, config)

    assert "__interrupt__" in result, "AskUser must pause the run"
    payload = result["__interrupt__"][0].value
    assert "sign in" in payload["question"].lower()
    # Not an approval or an options card: the transport routes those differently.
    assert not payload.get("approval")
    assert not payload.get("options")


async def test_the_answer_comes_back_to_the_model():
    """A question nobody can answer is just a slower failure."""
    graph = build_manager_graph(
        llm=asking_llm(after=[acts("Complete", "Signed in now; done.", success=True,
                                   reason="summarized after you signed in")]),
        surface=signin_wall(),
        rules=NoRules(),
        max_steps=12,
    )
    config = {"configurable": {"thread_id": "ask-2"}}

    await graph.ainvoke({"task": "summarize my inbox", "thread_id": "ask-2"}, config)
    final = await graph.ainvoke(Command(resume="I have signed in, go ahead"), config)

    assert final["success"] is True
    transcript = " ".join(
        m.content for m in final["messages"] if getattr(m, "content", None)
    )
    assert "I have signed in" in transcript, "the operator's answer must reach the model"


def test_every_advertised_control_verb_has_a_handler():
    """The class of bug, not just this instance.

    Binding a verb the dispatcher ignores produces a uniquely bad failure: the model is
    told the tool exists, calls it, and is told it does not work — so it tries again. Any
    new control tool must arrive with a branch.
    """
    from app.workers.internal_verbs import handle_internal
    from app.workers.tools import CONTROL_TOOLS

    source = inspect.getsource(handle_internal)
    for tool in CONTROL_TOOLS:
        assert f'call.name == "{tool.__name__}"' in source, (
            f"{tool.__name__} is offered to the model but has no handler"
        )
