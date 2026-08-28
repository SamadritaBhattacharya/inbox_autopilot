"""A click on Send is a send.

**The hole.** Gating was a set of verb names — `Send`, `DeleteForever`, `SendInvite`. The
compose worker is also bound to `Click`, and Gmail's Send button is an ordinary element with
an index. `Click(index=108)` sent the email, matched no gated verb, and dispatched with no
approval. The transcript that found this had the model write "Then click Send." on its own,
unprompted: this was not an exotic bypass, it was the obvious move.

"Nothing leaves your mailbox without you clicking approve" is the strongest claim this
project makes. These tests are what make it true rather than aspirational.
"""
from __future__ import annotations

import pytest
from inbox_contracts import ActionCall, Element, Observation

from app.agent.routing import ACT, APPROVAL, route_after_reason
from app.agent.state import AgentState
from app.security.vault import SessionPiiVault
from app.surface.dispatch import ActionValidator, DispatchRejected
from app.workers.approval import is_gated
from tests.fakes.fake_surface import observation as build_observation

SEND_INDEX = 108


def compose_view() -> Observation:
    return build_observation(
        Element(index=SEND_INDEX, role="button", name="Send (Ctrl-Enter)"),
        Element(index=7, role="button", name="Save draft"),
        Element(index=9, role="link", name="Sender settings"),
        title="Compose",
        compose_open=True,
    )


def click(index: int) -> ActionCall:
    return ActionCall(name="Click", args={"index": index})


# ── the predicate ───────────────────────────────────────────────────────────


def test_clicking_the_send_button_is_gated():
    assert is_gated(click(SEND_INDEX), compose_view()) is True


@pytest.mark.parametrize("index", [7, 9])
def test_ordinary_controls_are_not_gated(index):
    """Gating everything trains the user to click Approve without reading, which is how a
    gate stops being a gate."""
    assert is_gated(click(index), compose_view()) is False


def test_ctrl_enter_is_gated_even_with_no_button_involved():
    """Gmail sends on Ctrl+Enter. There is no element to inspect, so the keystroke itself
    has to be recognised — otherwise the bypass is one shortcut away."""
    call = ActionCall(name="PressKey", args={"key": "Control+Enter"})
    assert is_gated(call, compose_view()) is True


def test_a_plain_enter_is_not_gated():
    call = ActionCall(name="PressKey", args={"key": "Enter"})
    assert is_gated(call, compose_view()) is False


def test_without_an_observation_the_verb_still_gates():
    """Callers that genuinely have no observation must not silently lose verb-level gating."""
    assert is_gated(ActionCall(name="Send", args={"index": 1}), None) is True


# ── the routing ─────────────────────────────────────────────────────────────


def test_a_send_click_routes_to_the_approval_gate():
    state = AgentState(
        task="email P1", thread_id="g-1", last_action=click(SEND_INDEX), observation=compose_view()
    )
    assert route_after_reason(state) == APPROVAL


def test_an_ordinary_click_still_goes_straight_to_act():
    state = AgentState(
        task="email P1", thread_id="g-2", last_action=click(7), observation=compose_view()
    )
    assert route_after_reason(state) == ACT


# ── the dispatcher, which is the enforcement ────────────────────────────────


def validator(approved=frozenset()) -> ActionValidator:
    return ActionValidator(
        vault=SessionPiiVault(),
        geometry={SEND_INDEX: (10.0, 20.0), 7: (30.0, 40.0)},
        bound_verbs={"Click", "Type", "Send"},
        approved=approved,
        observation=compose_view(),
    )


def test_the_dispatcher_refuses_an_unapproved_send_click():
    """Defence in depth: even if routing were wrong, nothing reaches the browser."""
    with pytest.raises(DispatchRejected) as exc:
        validator().validate(click(SEND_INDEX))

    assert exc.value.error_code == "APPROVAL_REQUIRED"
    assert "Send" in exc.value.reason


def test_an_approved_send_click_dispatches():
    from app.surface.dispatch import approval_fingerprint

    call = click(SEND_INDEX)
    resolved = validator(approved={approval_fingerprint(call)}).validate(call)

    assert resolved.point == (10.0, 20.0)


def test_approving_one_click_does_not_authorize_a_different_one():
    """Approval binds to a payload, not a verb — otherwise one Approve blesses every send
    for the rest of the run."""
    from app.surface.dispatch import approval_fingerprint

    approved = {approval_fingerprint(click(7))}

    with pytest.raises(DispatchRejected):
        validator(approved=approved).validate(click(SEND_INDEX))


# ── one compose window, not two ─────────────────────────────────────────────


def inbox_with_compose_open(open_: bool = True) -> Observation:
    return build_observation(
        Element(index=72, role="button", name="Compose"),
        Element(index=75, role="button", name="Compose"),
        Element(index=54, role="textbox", name="To"),
        compose_open=open_,
    )


def compose_validator(observation: Observation) -> ActionValidator:
    return ActionValidator(
        vault=SessionPiiVault(),
        geometry={72: (1.0, 2.0), 75: (3.0, 4.0), 54: (5.0, 6.0)},
        bound_verbs={"Click", "Type"},
        observation=observation,
    )


def test_a_second_compose_window_is_refused():
    """The bug: it clicked Compose, re-observed, still saw a Compose button — Gmail's is
    always there — and clicked again. Recipient went in one window, subject in the other,
    and the mail sent with no subject."""
    with pytest.raises(DispatchRejected) as exc:
        compose_validator(inbox_with_compose_open()).validate(click(75))

    assert exc.value.error_code == "COMPOSE_ALREADY_OPEN"
    assert "already open" in exc.value.reason


def test_the_first_compose_click_is_allowed():
    resolved = compose_validator(inbox_with_compose_open(False)).validate(click(72))

    assert resolved.point == (1.0, 2.0)


def test_other_clicks_still_work_while_compose_is_open():
    """The guard must be narrow: the whole point is to keep working inside the open
    window."""
    resolved = compose_validator(inbox_with_compose_open()).validate(click(54))

    assert resolved.point == (5.0, 6.0)


def test_the_open_window_is_announced_on_its_own_line():
    """Buried in "view: inbox · unread: 12 · compose is open" it was reliably missed."""
    from app.workers.rendering import observation_block

    state = AgentState(
        task="email P1", thread_id="c-1", observation=inbox_with_compose_open()
    )

    assert "ALREADY OPEN" in observation_block(state)


# ── the card must DESCRIBE the send, not the click ──────────────────────────


def test_a_send_click_is_described_as_a_send_not_a_bulk_change():
    """Gating went consequence-based when `Click` on Gmail's Send button turned out to send
    mail; the CARD was left on the old verb-name test. The result was an approval prompt for
    the most irreversible thing this product does, headed "About to make a BULK CHANGE — Run
    Click". A person cannot approve what the card will not name."""
    from app.workers.approval import build_request

    request = build_request(
        click(SEND_INDEX),
        request_id="ap-1",
        preview="To: a@b.com\nSubject: Hi\n\nbody",
        timeout_seconds=600,
        observation=compose_view(),
    )

    assert request.kind == "send"
    assert request.summary == "Send this email"


def test_an_ordinary_click_is_still_described_honestly():
    """The counterfactual: this must not relabel every click as a send."""
    from app.workers.approval import build_request

    request = build_request(
        click(7),  # Save draft
        request_id="ap-2",
        preview="whatever",
        timeout_seconds=600,
        observation=compose_view(),
    )

    assert request.kind == "bulk"


def test_ctrl_enter_is_described_as_a_send():
    from inbox_contracts import ActionCall

    from app.workers.approval import build_request

    request = build_request(
        ActionCall(name="PressKey", args={"key": "Control+Enter"}),
        request_id="ap-3",
        preview="To: a@b.com\n\nbody",
        timeout_seconds=600,
        observation=compose_view(),
    )

    assert request.kind == "send"


def test_no_observation_falls_back_to_the_honest_wording():
    """Without an observation there is no way to know what a click targets, and claiming
    "send" would be inventing certainty."""
    from app.workers.approval import build_request

    request = build_request(
        click(SEND_INDEX), request_id="ap-4", preview="x", timeout_seconds=600
    )

    assert request.kind == "bulk"
