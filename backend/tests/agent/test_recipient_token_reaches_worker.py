"""The recipient the operator typed must arrive at the worker as a usable token.

**The bug this exists to prevent.** Intake mints an addressable token for an address the
operator typed, and used to write it into `intent.slots` only. The worker was handed
`Task: <the original text>` and nothing else, so it read a literal address in a system whose
prompt tells it literal addresses are rejected and that it will never see one. There was no
correct move left: it asked the human for a token that had been minted three nodes earlier,
got told to get on with it, and asked again until the run died.

Two independent failures, so two independent guards below — the raw address must not survive
into the task text, and the token must actually be shown to the worker. Either one alone
still leaves a broken run.
"""
from __future__ import annotations

import json

from app.agent.state import AgentState
from app.manager.intent import Action, TaskIntent
from app.manager.nodes import build_intake_node
from app.security.vault import SessionPiiVault
from app.workers.rendering import task_block
from tests.fakes.fake_llm import FakeLLMClient, ok

ADDRESS = "samadritabhatt163.official@gmail.com"
TASK = f"write a good afternoon mail with short motivation and send email to {ADDRESS}"


def intake_reply(**slots):
    return ok(json.dumps({"action": "send_email", "slots": slots, "confidence": 0.95}))


async def run_intake(vault: SessionPiiVault) -> dict:
    node = build_intake_node(
        FakeLLMClient([intake_reply(recipient_identity=ADDRESS, body_intent="short motivation")]),
        vault=vault,
    )
    return await node(AgentState(task=TASK, thread_id="t1"))


async def test_the_raw_address_does_not_survive_into_the_task():
    """The task text reaches the model verbatim, so a raw address here is raw PII in the
    prompt — the exact thing the vault exists to prevent — and an instruction the model is
    simultaneously told it must refuse."""
    vault = SessionPiiVault()

    delta = await run_intake(vault)

    assert ADDRESS not in delta["task"]
    assert vault.token_of(ADDRESS) in delta["task"]


async def test_the_minted_token_is_addressable():
    """A token the model cannot send to is no better than no token at all."""
    vault = SessionPiiVault()

    await run_intake(vault)

    assert vault.is_addressable(vault.token_of(ADDRESS))


async def test_the_worker_is_shown_the_resolved_recipient():
    """The other half: tokenizing the task is useless if the worker is never told which
    token is the recipient."""
    intent = TaskIntent(
        action=Action.SEND_EMAIL,
        slots={"recipient_identity": "P1", "body_intent": "short motivation"},
        action_confidence=0.95,
    )
    state = AgentState(task="send email to P1", thread_id="t1", intent=intent)

    block = task_block(state)

    assert "recipient_identity: P1" in block
    assert "short motivation" in block


async def test_empty_slots_are_not_shown():
    """Printing `topic: ` invites the model to treat blank as a filled answer."""
    intent = TaskIntent(
        action=Action.SEND_EMAIL,
        slots={"recipient_identity": "P1", "topic": "   "},
        action_confidence=0.95,
    )

    block = task_block(AgentState(task="t", thread_id="t1", intent=intent))

    assert "topic" not in block


async def test_a_task_with_no_intent_still_renders():
    """`task_block` runs before intake on no path today, but a renderer that explodes on a
    None it declares optional is a landmine for the next caller."""
    block = task_block(AgentState(task="summarize my inbox", thread_id="t1"))

    assert block == "Task: summarize my inbox"
