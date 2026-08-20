"""Contract tests — the wire types are the single source of truth.

The security invariants in docs/WS-PROTOCOL.md §2 are asserted here as SCHEMA-LEVEL
facts, not conventions: a payload carrying coordinates, raw DOM, or a URL must be
*rejected by validation*, so no downstream code can rely on discipline alone.
"""
from __future__ import annotations

import pytest
from inbox_contracts import (
    PROTOCOL_VERSION,
    ActionCall,
    ActionResult,
    Element,
    Envelope,
    MailContext,
    Observation,
    Viewport,
)
from pydantic import ValidationError


def _observation(**overrides) -> dict:
    payload = {
        "contextId": "ctx-abc123",
        "title": "Inbox (12)",
        "viewport": {"width": 1440, "height": 900, "scrollX": 0, "scrollY": 240},
        "elements": [
            {"index": 1, "role": "button", "name": "Compose"},
            {"index": 2, "role": "listitem", "name": "P17 — Friday demo", "isNew": True},
        ],
        "mail": {"view": "inbox", "unreadCount": 12, "composeOpen": False},
        "droppedCount": 18,
    }
    payload.update(overrides)
    return payload


# ── round-trip + casing ─────────────────────────────────────────────────────

def test_observation_round_trips_by_alias():
    obs = Observation.model_validate(_observation())
    dumped = obs.model_dump(by_alias=True)
    assert Observation.model_validate(dumped) == obs


def test_wire_casing_is_camel():
    obs = Observation.model_validate(_observation())
    dumped = obs.model_dump(by_alias=True)
    assert "protocolVersion" in dumped
    assert "contextId" in dumped
    assert "droppedCount" in dumped
    assert dumped["viewport"]["scrollY"] == 240
    assert dumped["elements"][1]["isNew"] is True
    # snake_case must NOT leak onto the wire
    assert "protocol_version" not in dumped
    assert "dropped_count" not in dumped


def test_python_side_accepts_snake_case_field_names():
    """populate_by_name: backend code constructs with field names, the wire uses aliases."""
    obs = Observation(
        context_id="ctx-1",
        viewport=Viewport(width=800, height=600),
        dropped_count=3,
    )
    assert obs.model_dump(by_alias=True)["droppedCount"] == 3


def test_protocol_version_rides_on_every_wire_message():
    assert Observation.model_validate(_observation()).protocol_version == PROTOCOL_VERSION
    assert Envelope(type="observe").protocol_version == PROTOCOL_VERSION


# ── invariant 1: NO COORDINATES ─────────────────────────────────────────────

@pytest.mark.parametrize("field", ["x", "y", "centerX", "centerY", "backendNodeId"])
def test_element_rejects_geometry(field):
    """Geometry stays in the executor's hidden index map. It must never be expressible."""
    with pytest.raises(ValidationError):
        Element.model_validate({"index": 1, "role": "button", "name": "Compose", field: 42})


@pytest.mark.parametrize("field", ["x", "y", "coordinates"])
def test_observation_rejects_geometry(field):
    with pytest.raises(ValidationError):
        Observation.model_validate(_observation(**{field: 10}))


# ── invariant 2: NO RAW DOM ─────────────────────────────────────────────────

@pytest.mark.parametrize("field", ["html", "outerHTML", "dom", "selector"])
def test_observation_rejects_raw_dom(field):
    with pytest.raises(ValidationError):
        Observation.model_validate(_observation(**{field: "<div>…</div>"}))


# ── invariant 4: NO URL (it leaks message + thread ids on an email surface) ──

def test_observation_rejects_url():
    with pytest.raises(ValidationError):
        Observation.model_validate(_observation(url="https://mail.google.com/mail/u/0/#inbox/18f3a"))


def test_observation_uses_opaque_context_id_instead_of_url():
    obs = Observation.model_validate(_observation())
    assert obs.context_id == "ctx-abc123"
    assert not hasattr(obs, "url")


# ── invariant 6: droppedCount is honest ─────────────────────────────────────

def test_dropped_count_defaults_to_zero_and_survives_round_trip():
    obs = Observation(context_id="c", viewport=Viewport(width=1, height=1))
    assert obs.dropped_count == 0
    assert Observation.model_validate(_observation()).dropped_count == 18


# ── required fields ─────────────────────────────────────────────────────────

def test_context_id_is_required():
    with pytest.raises(ValidationError):
        Observation.model_validate({"viewport": {"width": 1, "height": 1}})


def test_mail_context_view_is_constrained():
    with pytest.raises(ValidationError):
        MailContext.model_validate({"view": "spam-folder"})
    assert MailContext.model_validate({"view": "compose", "composeOpen": True}).compose_open


# ── ActionCall / ActionResult ───────────────────────────────────────────────

def test_action_call_round_trips():
    call = ActionCall(name="Type", args={"index": 14, "text": "P17"})
    assert ActionCall.model_validate(call.model_dump()) == call


def test_action_call_args_stay_free_form():
    """args is an open dict by design — the dispatcher validates it per verb, not the schema."""
    assert ActionCall(name="Scroll", args={"direction": "down", "amount": 2}).args["amount"] == 2


def test_action_result_error_code_alias():
    res = ActionResult(success=False, reason="target vanished", error_code="STALE_INDEX")
    assert res.model_dump(by_alias=True)["errorCode"] == "STALE_INDEX"


def test_action_result_carries_undo_payload():
    res = ActionResult(success=True, undo={"verb": "Archive", "restore_to": "INBOX"})
    assert res.undo == {"verb": "Archive", "restore_to": "INBOX"}


# ── Envelope (the relay frame) ──────────────────────────────────────────────

def test_envelope_correlation_id_is_optional():
    assert Envelope(type="frame").id is None
    assert Envelope(type="act", id="req-7", payload={"call": {}}).id == "req-7"


# ── emitted JSON Schema ─────────────────────────────────────────────────────

def test_emitted_schema_uses_wire_casing():
    schema = Observation.model_json_schema(by_alias=True)
    props = schema["properties"]
    assert "contextId" in props
    assert "protocolVersion" in props
    assert "droppedCount" in props
    assert "context_id" not in props


def test_emitted_schema_forbids_extra_properties():
    """The generated Zod must inherit the no-coordinates/no-DOM guarantee."""
    assert Observation.model_json_schema(by_alias=True).get("additionalProperties") is False
