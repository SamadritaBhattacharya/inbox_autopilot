"""Never ask a person for a number only the system can see.

**The worst question this agent has ever asked, observed live:**

    "What is the index number of the remove (×) button for the recipient chip in the To
     field?"

Indices are ours. They are rebuilt every turn by design, they appear in no interface the
human has, and the person on the other end had just watched the agent scroll six times
hunting for the answer themselves. The run stopped there, waiting on an answer that could
not exist.

It happened because the correction instruction named an element that is not in the
observation (a chip's × has no accessible name and does not survive the funnel). With
nothing findable and nothing to try, asking was the only move left. The instruction is
fixed elsewhere; this is the backstop, because the last one — "ask about what the human can
see" in the prompt — is exactly what did not hold.

Refusing costs one turn and hands back a usable alternative. Asking costs the human's
trust and still gets no answer.
"""
from __future__ import annotations

import pytest
from inbox_contracts import ActionCall

from app.agent.state import AgentState
from app.events.emitter import EventEmitter
from app.events.sink import BufferSink
from app.workers import internal_verbs as verbs_module
from app.workers.internal_verbs import _asks_for_internals, handle_internal


def ask(question: str) -> ActionCall:
    return ActionCall(name="AskUser", args={"question": question})


async def run(question: str) -> dict:
    """The real handler. `interrupt()` would raise outside a graph, so a question that
    reaches it fails this test loudly rather than passing quietly."""
    return await handle_internal(
        ask(question), AgentState(task="t", thread_id="ask-1"), EventEmitter(BufferSink())
    )


# ── refused ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "question",
    [
        "What is the index number of the remove (×) button for the recipient chip?",
        "Which index is the Send button?",
        "what indices correspond to the compose fields?",
        "Please give me the element number for the To field.",
        "Tell me the element id of the chip.",
        "Is the subject at [N]?",
    ],
)
@pytest.mark.anyio
async def test_a_question_about_our_own_bookkeeping_is_refused(question):
    delta = await run(question)

    assert "status" not in delta, "the run paused on an unanswerable question"
    assert "unanswerable" in delta["messages"][0].content


@pytest.mark.anyio
async def test_the_refusal_explains_what_to_do_instead():
    """A refusal the model cannot act on just moves the dead end one turn later."""
    content = (await run("what is the index of the × button?"))["messages"][0].content

    assert "rebuilt every turn" in content
    assert "did not survive the observation" in content
    assert "scrolling will" in content, "the agent had been scrolling for six turns"


# ── still asked ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "question",
    [
        "Which Priya did you mean — Priya Nair or Priya Sharma?",
        "Should this go out today or wait until Monday?",
        "I cannot find a compose button. Is Gmail open in this tab?",
        "Do you want me to reply to all three, or only the first?",
    ],
)
@pytest.mark.anyio
async def test_a_real_question_still_reaches_the_human(question, monkeypatch):
    """The guard must stay narrow. `AskUser` exists for exactly these, and a guard that
    swallowed them would trade a bad question for a silent wrong guess."""
    asked: list[dict] = []
    monkeypatch.setattr(
        verbs_module, "interrupt", lambda payload: asked.append(payload) or "sure"
    )

    delta = await run(question)

    assert [payload["question"] for payload in asked] == [question]
    assert delta["status"] == "running", "the run must resume with the answer"
    assert "sure" in delta["messages"][0].content


def test_the_matcher_is_about_the_number_not_the_neighbourhood():
    """"Index" inside an unrelated word must not disarm a legitimate question."""
    assert _asks_for_internals("what is the index?") is True
    assert _asks_for_internals("which index number?") is True
    assert _asks_for_internals("should I index the archive?") is True  # accepted overreach
    assert _asks_for_internals("who should this go to?") is False
    assert _asks_for_internals("is this the right address?") is False
    assert _asks_for_internals("") is False
