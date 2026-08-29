"""The approval gate — R2, and the guarantee that makes this safe on a real mailbox.

These are the highest-stakes tests in the suite. Every one of them asserts on what was
**attempted**, not on what happened afterwards: "no send was dispatched" is a far stronger
claim than "no email arrived".
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from inbox_contracts import ActionCall, Element
from langgraph.types import Command

from app.agent.graph import build_manager_graph
from app.agent.routing import (
    ACT,
    APPROVAL,
    FINALIZE,
    REASON,
    route_after_approval,
    route_after_reason,
)
from app.agent.state import AgentState
from app.llm.base import LLMResult, ToolCall
from app.rules.store import NoRules
from app.surface.dispatch import approval_fingerprint
from app.telemetry.records import ErrorCode
from app.workers.approval import (
    Decision,
    Verdict,
    build_request,
    decision_from,
    is_gated,
)
from tests.fakes.fake_llm import FakeLLMClient, drafted, ok
from tests.fakes.fake_surface import FakeEmailSurface, observation

DRAFT = "To:      Priya Nair <priya.nair@corp.com>\nSubject: Friday demo\n\nIt moved to 4pm."


def send_call(**args) -> ActionCall:
    return ActionCall(name="Send", args=args or {"index": 9})


def state(**overrides) -> AgentState:
    return AgentState(task="email P1", thread_id="run-1", **overrides)


def intake(action: str, **slots):
    return ok(json.dumps({"action": action, "slots": slots, "confidence": 0.95}))


def acts(name: str, text: str, **args) -> LLMResult:
    return LLMResult(
        text=text, tool_calls=[ToolCall(id="c", name=name, args=args)], provider="fake"
    )


# ── which verbs are gated ───────────────────────────────────────────────────


@pytest.mark.parametrize("verb", ["Send", "SendInvite", "DeleteForever"])
def test_every_irreversible_verb_is_gated(verb):
    assert is_gated(ActionCall(name=verb)) is True


@pytest.mark.parametrize("verb", ["Archive", "Click", "Type", "Scroll", "DraftReply"])
def test_reversible_verbs_are_not_gated(verb):
    """Gating everything would train the user to click Approve without reading."""
    assert is_gated(ActionCall(name=verb)) is False


def test_no_action_is_not_gated():
    assert is_gated(None) is False


# ── the routing IS the guarantee ────────────────────────────────────────────


def test_a_gated_verb_cannot_route_straight_to_act():
    """There is no edge from reason to act for a gated verb."""
    assert route_after_reason(state(last_action=send_call())) == APPROVAL


def test_an_ordinary_verb_goes_straight_to_act():
    archive = ActionCall(name="Archive", args={"index": 3})
    assert route_after_reason(state(last_action=archive)) == ACT


def test_an_approved_action_proceeds_to_act():
    assert route_after_approval(state(last_action=send_call())) == ACT


def test_a_cleared_action_goes_back_to_thinking():
    """Edit and reject both clear `last_action`, so there is nothing to dispatch."""
    assert route_after_approval(state(last_action=None)) == REASON


def test_a_terminal_state_finalizes_from_the_gate():
    assert route_after_approval(state(status="failed")) == FINALIZE


# ── decisions fail closed ───────────────────────────────────────────────────


def test_an_explicit_approval_is_honoured():
    assert decision_from({"verdict": "approve"}).approved is True


@pytest.mark.parametrize(
    "payload", [None, {}, {"verdict": "maybe"}, "yes", 42, {"verdict": ""}, []]
)
def test_anything_unrecognisable_is_a_rejection(payload):
    """A malformed resume must never read as consent."""
    decision = decision_from(payload)
    assert decision.verdict is Verdict.REJECT
    assert decision.approved is False


def test_an_edit_carries_the_humans_text():
    decision = decision_from({"verdict": "edit", "edit": "say 4pm IST"})
    assert decision.verdict is Verdict.EDIT
    assert decision.edit == "say 4pm IST"
    assert decision.approved is False, "an edited draft is a DIFFERENT draft"


# ── the request ─────────────────────────────────────────────────────────────


def test_the_request_binds_to_the_exact_payload():
    call = send_call(index=9, recipient="P1")
    request = build_request(call, request_id="ap-1", preview=DRAFT, timeout_seconds=600)
    assert request.fingerprint == approval_fingerprint(call, DRAFT)


def test_approval_binds_to_the_words_not_the_button():
    """`Send(index=9)` says where the button is, not what the email says.

    Fingerprinting the args alone meant one "yes" authorised that button for the rest of the
    run: edit the body, retype it, call `Send(index=9)` again, and it matched an approval the
    human gave for different words. The human approves an EMAIL.
    """
    call = send_call(index=9)
    edited = DRAFT.replace("4pm", "9am")

    assert approval_fingerprint(call, DRAFT) != approval_fingerprint(call, edited)


def test_the_fingerprint_never_leaks_the_draft():
    """It travels into request ids and logs; a resolved preview holds real addresses."""
    fingerprint = approval_fingerprint(send_call(), DRAFT)

    assert "priya.nair@corp.com" not in fingerprint
    assert "It moved to 4pm." not in fingerprint


def test_identical_content_is_stable_across_turns():
    """The gate re-executes on resume, so an unchanged draft must fingerprint the same or
    the human would be asked twice for one decision."""
    call = send_call(index=9)

    assert approval_fingerprint(call, DRAFT) == approval_fingerprint(call, DRAFT)


def test_the_preview_shows_a_resolved_recipient():
    """A human cannot verify "send to P17" — that check is the whole point of the gate."""
    request = build_request(send_call(), request_id="ap-1", preview=DRAFT, timeout_seconds=600)
    assert "priya.nair@corp.com" in request.preview


def test_a_delete_says_it_cannot_be_undone():
    request = build_request(
        ActionCall(name="DeleteForever", args={"index": 2}),
        request_id="ap-2",
        preview="thread 2",
        timeout_seconds=600,
    )
    assert request.kind == "delete"
    assert "cannot be undone" in request.summary


def test_an_expired_request_knows_it():
    request = build_request(send_call(), request_id="ap-3", preview=DRAFT, timeout_seconds=600)
    assert request.expired is False
    stale = request.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    assert stale.expired is True


# ── through the graph, end to end ───────────────────────────────────────────


def compose_run(surface: FakeEmailSurface, llm: FakeLLMClient, thread: str):
    graph = build_manager_graph(
        llm=llm, surface=surface, rules=NoRules(), max_steps=12
    )
    config = {"configurable": {"thread_id": thread}}
    return graph, config


def scoped(kind: str, brief: str = "") -> LLMResult:
    """What the edit-scope classifier returns for one human instruction."""
    return ok(json.dumps({"kind": kind, "brief": brief}))


def compose_llm(*, extra: list | None = None) -> FakeLLMClient:
    return FakeLLMClient(
        [
            intake("send_email", recipient_identity="P1", topic="the Friday demo"),
            ok("decision"),
            ok("Open compose\nFill fields\nSend"),
            drafted(),
            acts("Send", "The draft is complete; sending.", index=9),
            *(extra or []),
        ]
    )


def compose_surface() -> FakeEmailSurface:
    return FakeEmailSurface(
        [observation(Element(index=9, role="button", name="Send"), compose_open=True)],
        preview=DRAFT,
    )


async def test_a_send_pauses_and_shows_the_resolved_draft():
    surface = compose_surface()
    graph, config = compose_run(surface, compose_llm(), "ap-run-1")

    result = await graph.ainvoke(
        {"task": "email P1 about the demo", "thread_id": "ap-run-1"}, config
    )

    assert "__interrupt__" in result, "the run must PAUSE before sending"
    payload = result["__interrupt__"][0].value
    assert payload["approval"] is True
    assert "priya.nair@corp.com" in payload["preview"]
    assert surface.never_dispatched("Send"), "nothing may be sent before the human answers"
    assert surface.previewed, "the human must be shown the draft first"


async def test_approving_dispatches_exactly_once():
    surface = compose_surface()
    llm = compose_llm(extra=[acts("Complete", "Sent.", success=True, reason="sent")])
    graph, config = compose_run(surface, llm, "ap-run-2")

    await graph.ainvoke({"task": "email P1", "thread_id": "ap-run-2"}, config)
    final = await graph.ainvoke(Command(resume={"verdict": "approve"}), config)

    assert surface.verbs.count("Send") == 1
    assert final["success"] is True


async def test_rejecting_never_dispatches():
    surface = compose_surface()
    llm = compose_llm(
        extra=[acts("Complete", "Understood, not sending.", success=False, reason="declined")]
    )
    graph, config = compose_run(surface, llm, "ap-run-3")

    await graph.ainvoke({"task": "email P1", "thread_id": "ap-run-3"}, config)
    final = await graph.ainvoke(Command(resume={"verdict": "reject"}), config)

    assert surface.never_dispatched("Send")
    assert final["success"] is False


async def test_an_edit_returns_to_the_loop_without_sending():
    """An edited draft is a different draft; it has to be approved again."""
    surface = compose_surface()
    llm = compose_llm(
        extra=[
            # An instruction is classified first: "say 4pm IST" adjusts the words that are
            # there, rather than asking for a different email or asking a question.
            scoped("adjust"),
            # Then the reviser runs, applying the correction to the existing draft instead
            # of letting the loop regenerate the whole email.
            drafted(subject="Friday demo", body="It moved to 4pm IST."),
            acts("Complete", "Revised and stopping.", success=False, reason="revised"),
        ]
    )
    graph, config = compose_run(surface, llm, "ap-run-4")

    await graph.ainvoke({"task": "email P1", "thread_id": "ap-run-4"}, config)
    await graph.ainvoke(Command(resume={"verdict": "edit", "edit": "say 4pm IST"}), config)

    assert surface.never_dispatched("Send")
    # The human's words reached the model rather than being swallowed.
    sent = " ".join(m.content for _, messages, _ in llm.requests for m in messages)
    assert "say 4pm IST" in sent


async def test_a_malformed_resume_does_not_send():
    """Failing closed matters most exactly here."""
    surface = compose_surface()
    llm = compose_llm(extra=[acts("Complete", "Stopping.", success=False, reason="no consent")])
    graph, config = compose_run(surface, llm, "ap-run-5")

    await graph.ainvoke({"task": "email P1", "thread_id": "ap-run-5"}, config)
    await graph.ainvoke(Command(resume={"verdict": "definitely maybe"}), config)

    assert surface.never_dispatched("Send")


async def test_the_pause_survives_a_rebuilt_graph():
    """A user can close the tab and come back to the draft still waiting."""
    from langgraph.checkpoint.memory import InMemorySaver

    saver = InMemorySaver()
    surface = compose_surface()
    config = {"configurable": {"thread_id": "ap-run-6"}}

    first = build_manager_graph(
        llm=compose_llm(), surface=surface, rules=NoRules(), max_steps=12, checkpointer=saver
    )
    assert "__interrupt__" in await first.ainvoke(
        {"task": "email P1", "thread_id": "ap-run-6"}, config
    )

    rebuilt = build_manager_graph(
        llm=FakeLLMClient([acts("Complete", "Sent.", success=True, reason="sent")]),
        surface=surface,
        rules=NoRules(),
        max_steps=12,
        checkpointer=saver,
    )
    await rebuilt.ainvoke(Command(resume={"verdict": "approve"}), config)

    assert surface.dispatched("Send")


async def test_an_expired_approval_fails_typed_and_does_not_send():
    surface = compose_surface()
    llm = compose_llm()
    graph = build_manager_graph(
        llm=llm,
        surface=surface,
        rules=NoRules(),
        max_steps=12,
        approval_timeout_seconds=0,  # expired the instant it was created
    )
    config = {"configurable": {"thread_id": "ap-run-7"}}

    await graph.ainvoke({"task": "email P1", "thread_id": "ap-run-7"}, config)
    final = await graph.ainvoke(Command(resume={"verdict": "approve"}), config)

    assert surface.never_dispatched("Send")
    assert final["error_code"] == ErrorCode.APPROVAL_TIMEOUT


# ── the second, independent lock ────────────────────────────────────────────


async def test_the_surface_still_refuses_an_unapproved_send():
    """Routing is the first lock; the surface's fingerprint check is the second.

    Even if a future edit routed a gated verb straight to `act`, the surface would refuse
    it — which is what "defence in depth" has to mean to be worth saying.
    """
    from app.security.vault import SessionPiiVault
    from app.surface.dispatch import ActionValidator, DispatchRejected

    validator = ActionValidator(
        vault=SessionPiiVault(),
        geometry={9: (1.0, 2.0)},
        bound_verbs={"Send"},
        approved=set(),
    )
    with pytest.raises(DispatchRejected) as exc:
        validator.validate(send_call())
    assert exc.value.error_code == "APPROVAL_REQUIRED"


def test_no_remediation_can_produce_an_approval():
    """A strategy that could approve on the user's behalf would make the gate decorative."""
    assert not hasattr(Decision, "auto_approve")
    assert Decision(verdict=Verdict.REJECT).approved is False


# ── the deadline is enforced where the waiting happens ──────────────────────


def test_an_expired_verdict_is_distinct_from_a_rejection():
    """Nobody declined; nobody was there. Different facts, different codes."""
    decision = decision_from({"verdict": "expired"})
    assert decision.verdict is Verdict.EXPIRED
    assert decision.approved is False


async def test_a_timed_out_approval_fails_typed_and_never_sends():
    surface = compose_surface()
    llm = compose_llm()
    graph, config = compose_run(surface, llm, "ap-run-8")

    await graph.ainvoke({"task": "email P1", "thread_id": "ap-run-8"}, config)
    # What the transport sends when the deadline passes with no answer.
    final = await graph.ainvoke(Command(resume={"verdict": "expired"}), config)

    assert surface.never_dispatched("Send")
    assert final["error_code"] == ErrorCode.APPROVAL_TIMEOUT
