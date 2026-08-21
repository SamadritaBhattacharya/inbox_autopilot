"""The writer — the node that decides what the email actually says.

Drafting is separated from operating the screen because the two need opposite contexts. The
worker's context is 140 DOM elements and a prompt about indices; asking it to also write
prose, one `Type` call at a time, is the worst place in the system to compose anything.

What these pin is the seam, not the prose: the writer runs for writing actions and only
those, its failure is a downgrade rather than a dead run, and the words it produced actually
reach the worker.
"""
from __future__ import annotations

import json

import pytest

from app.agent.state import AgentState
from app.manager.intent import Action, TaskIntent
from app.manager.writer import Draft, WRITING_ACTIONS, brief_for, build_writer_node
from app.workers.rendering import task_block
from tests.fakes.fake_llm import FakeLLMClient, ok

BODY = "Good afternoon. Small push for the day: the hard part is starting. Best."


def reply(**fields):
    payload = {"subject": "A push for the afternoon", "body": BODY, "tone": "warm"}
    payload.update(fields)
    return ok(json.dumps(payload))


def state_for(action: Action, **slots) -> AgentState:
    return AgentState(
        task="write a good afternoon mail with short motivation and send to P1",
        thread_id="w-1",
        intent=TaskIntent(action=action, slots=slots, action_confidence=0.95),
    )


# ── when it runs ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("action", sorted(WRITING_ACTIONS, key=lambda a: a.value))
async def test_it_drafts_for_every_writing_action(action):
    llm = FakeLLMClient([reply()])

    delta = await build_writer_node(llm)(state_for(action))

    assert delta["draft"].body == BODY


@pytest.mark.parametrize("action", [Action.TRIAGE, Action.ARCHIVE, Action.SUMMARIZE])
async def test_it_costs_nothing_on_a_task_with_no_prose(action):
    """A model call per triage run, to draft an email nobody asked for, is pure waste — and
    on a free tier, waste is the resource that runs out."""
    llm = FakeLLMClient([])  # any call at all raises

    delta = await build_writer_node(llm)(state_for(action))

    assert delta == {}
    assert llm.call_count == 0


async def test_it_does_nothing_before_intake_has_run():
    llm = FakeLLMClient([])

    delta = await build_writer_node(llm)(AgentState(task="anything", thread_id="w-2"))

    assert delta == {}


# ── failure is a downgrade, not a dead run ──────────────────────────────────


@pytest.mark.parametrize("garbage", ["not json at all", "{}", '{"subject": "hi"}', ""])
async def test_an_unusable_reply_leaves_the_worker_to_improvise(garbage):
    """The worker can still write inline; it is merely worse at it. Failing the run here
    would turn a bad sentence into a dead run."""
    llm = FakeLLMClient([ok(garbage)])

    delta = await build_writer_node(llm)(state_for(Action.SEND_EMAIL))

    assert delta["draft"] is None
    assert delta["history"], "the attempt is still on the trajectory"


async def test_a_fenced_reply_is_still_read():
    """Models fence JSON and preface it. Refusing those loses a perfectly good draft."""
    llm = FakeLLMClient([ok(f"Here you go:\n```json\n{reply().text}\n```")])

    delta = await build_writer_node(llm)(state_for(Action.SEND_EMAIL))

    assert delta["draft"].body == BODY


async def test_a_runaway_body_cannot_crowd_out_the_observation():
    llm = FakeLLMClient([reply(body="x" * 99_999)])

    delta = await build_writer_node(llm)(state_for(Action.SEND_EMAIL))

    assert len(delta["draft"].body) <= 4000


# ── the draft has to survive to the worker ──────────────────────────────────


def test_the_worker_is_told_to_type_the_draft_verbatim():
    """Re-deciding the wording mid-loop, surrounded by DOM elements, reliably makes it
    worse — and the human may already have seen these exact words."""
    state = state_for(Action.SEND_EMAIL, recipient_identity="P1")
    state.draft = Draft(subject="A push for the afternoon", body=BODY, tone="warm")

    block = task_block(state)

    assert "A push for the afternoon" in block
    assert BODY in block
    assert "EXACTLY" in block


def test_the_brief_carries_the_resolved_slots():
    """The writer is briefed from the intent, not the raw task: those slots are what intake
    and the gate already agreed the request means."""
    brief = brief_for(state_for(Action.SEND_EMAIL, recipient_identity="P1", tone="friendly"))

    assert "tone: friendly" in brief
    assert "recipient_identity: P1" in brief


def test_a_draft_survives_serialization():
    """It lives in `AgentState`, which is checkpointed on every interrupt — including the
    send-approval pause this feature exists to serve."""
    draft = Draft(subject="s", body="b", tone="warm")

    assert Draft.model_validate(json.loads(draft.model_dump_json())) == draft
