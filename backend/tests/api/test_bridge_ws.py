"""`/ws/bridge` — pairing, and what happens when it fails.

Driven through a real `TestClient` WebSocket. What is under test is the *refusals*: a bridge
socket is a mailbox, so each of these is a hole if it stops holding.

The property that matters most is the last section. Under a single shared secret, every
extension registered as the same owner and a second user's browser replaced the first's —
so user A's next run drove user B's mailbox.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from app.api import bridge_ws
from app.api.bridge_ws import BRIDGE_PROTOCOL_VERSION, CLOSE_DISABLED, CLOSE_UNAUTHORIZED
from app.api.main import app
from app.auth.pairing import PairingCodes
from app.auth.tokens import mint, mint_bridge
from app.config.settings import Settings
from app.surface.bridge import BridgeRegistry

SECRET = "a-long-server-side-signing-secret"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """A clean registry and a clean code store per test."""
    registry = BridgeRegistry()
    monkeypatch.setattr(bridge_ws, "BRIDGES", registry)
    codes = PairingCodes()
    monkeypatch.setattr(bridge_ws, "PAIRING", codes)
    monkeypatch.setattr("app.api.auth_routes.PAIRING", codes, raising=False)
    return registry, codes


@pytest.fixture
def configured(monkeypatch):
    def install(secret: str = SECRET) -> Settings:
        settings = Settings(_env_file=None, auth_secret=SecretStr(secret))
        monkeypatch.setattr(bridge_ws, "get_settings", lambda: settings)
        return settings

    return install


def hello(**overrides) -> dict:
    frame = {
        "type": "hello",
        "protocolVersion": BRIDGE_PROTOCOL_VERSION,
        "accountToken": None,
        "extensionVersion": "0.1.0",
    }
    frame.update(overrides)
    return frame


def refused(frame: dict) -> int:
    """Connect, send `frame`, and return the close code."""
    with TestClient(app).websocket_connect("/ws/bridge") as socket:
        socket.send_json(frame)
        with pytest.raises(WebSocketDisconnect) as exc:
            socket.receive_json()
    return exc.value.code


# ── refusals ────────────────────────────────────────────────────────────────


def test_an_unknown_pairing_code_is_refused(configured):
    configured()
    assert refused(hello(pairingCode="NOTACODE99")) == CLOSE_UNAUTHORIZED


def test_no_credential_at_all_is_refused(configured):
    configured()
    assert refused(hello()) == CLOSE_UNAUTHORIZED


def test_a_forged_bridge_token_is_refused(configured):
    """Signed with the wrong secret. The signature check is the whole defence."""
    configured()
    forged = mint_bridge("u1", "a@b.com", "not-the-server-secret")
    assert refused(hello(bridgeToken=forged)) == CLOSE_UNAUTHORIZED


def test_a_session_token_is_not_a_bridge_token(configured):
    """Same signature, same shape, wildly different authority. Without the audience check a
    cockpit login would be a credential for driving somebody's mailbox."""
    configured()
    session = mint("u1", "a@b.com", SECRET)
    assert refused(hello(bridgeToken=session)) == CLOSE_UNAUTHORIZED


def test_a_code_cannot_be_redeemed_twice(configured, fresh):
    """A code that still works after redemption can pair a second browser to the same
    mailbox."""
    configured()
    _registry, codes = fresh
    code = codes.issue("u1", "a@b.com")

    with TestClient(app).websocket_connect("/ws/bridge") as socket:
        socket.send_json(hello(pairingCode=code))
        socket.receive_json()  # welcomed

    assert refused(hello(pairingCode=code)) == CLOSE_UNAUTHORIZED


def test_a_server_with_no_signing_secret_accepts_nobody(monkeypatch):
    """An unset secret is a misconfiguration, and guessing "they probably meant open" costs
    somebody else their mailbox."""
    settings = Settings(_env_file=None, auth_secret=SecretStr(""))
    monkeypatch.setattr(bridge_ws, "get_settings", lambda: settings)

    with (
        TestClient(app).websocket_connect("/ws/bridge") as socket,
        pytest.raises(WebSocketDisconnect) as exc,
    ):
        socket.receive_json()

    assert exc.value.code == CLOSE_DISABLED


def test_a_mismatched_protocol_is_refused(configured):
    """A bridge that half-understands the frames is worse than one that will not connect."""
    configured()
    assert refused(hello(protocolVersion="999", pairingCode="X")) == CLOSE_DISABLED


def test_a_first_frame_that_is_not_hello_is_refused(configured):
    configured()
    assert refused({"type": "result", "id": "1", "ok": True, "result": None}) == CLOSE_DISABLED


def test_nothing_is_registered_when_pairing_fails(configured, fresh):
    configured()
    registry, _codes = fresh

    refused(hello(pairingCode="NOTACODE99"))

    assert registry.owners() == []


# ── the happy path ──────────────────────────────────────────────────────────


def test_redeeming_a_code_pairs_the_browser_to_that_user(configured, fresh):
    configured()
    registry, codes = fresh
    code = codes.issue("user-123", "priya@corp.com")

    with TestClient(app).websocket_connect("/ws/bridge") as socket:
        socket.send_json(hello(pairingCode=code))
        welcome = socket.receive_json()

        assert welcome["type"] == "welcome"
        assert welcome["account"] == "priya@corp.com"
        assert welcome["bridgeToken"], "the extension needs a durable token to reconnect with"
        assert registry.owners() == ["user-123"]


def test_the_durable_token_works_without_a_new_code(configured, fresh):
    """The property MV3 forces: service workers are suspended constantly, so an extension
    that had to be re-paired on every reconnect would be unusable."""
    configured()
    registry, _codes = fresh
    token = mint_bridge("user-123", "priya@corp.com", SECRET)

    with TestClient(app).websocket_connect("/ws/bridge") as socket:
        socket.send_json(hello(bridgeToken=token))
        welcome = socket.receive_json()

        assert welcome["account"] == "priya@corp.com"
        assert registry.owners() == ["user-123"]


def test_the_bridge_is_unregistered_on_disconnect(configured, fresh):
    configured()
    registry, codes = fresh
    code = codes.issue("user-123", "priya@corp.com")

    with TestClient(app).websocket_connect("/ws/bridge") as socket:
        socket.send_json(hello(pairingCode=code))
        socket.receive_json()

    assert registry.owners() == []


def test_an_unknown_frame_does_not_disconnect_a_good_bridge(configured, fresh):
    """A newer extension talking to an older server should degrade, not drop."""
    configured()
    _registry, codes = fresh
    code = codes.issue("user-123", "priya@corp.com")

    with TestClient(app).websocket_connect("/ws/bridge") as socket:
        socket.send_json(hello(pairingCode=code))
        socket.receive_json()

        socket.send_json({"type": "something-new", "data": 1})
        socket.send_json({"type": "detached", "reason": "tab closed"})


# ── the multi-user property ─────────────────────────────────────────────────


def test_two_users_get_two_separate_bridges(configured, fresh):
    """THE bug this design exists to prevent.

    Under one shared secret both extensions registered as the same owner, the second
    replaced the first, and user A's next run drove user B's mailbox.
    """
    configured()
    registry, codes = fresh
    a = codes.issue("user-A", "a@corp.com")
    b = codes.issue("user-B", "b@corp.com")

    client = TestClient(app)
    with client.websocket_connect("/ws/bridge") as socket_a:
        socket_a.send_json(hello(pairingCode=a))
        socket_a.receive_json()

        with client.websocket_connect("/ws/bridge") as socket_b:
            socket_b.send_json(hello(pairingCode=b))
            socket_b.receive_json()

            assert sorted(registry.owners()) == ["user-A", "user-B"]
            assert registry.get("user-A") is not registry.get("user-B")


def test_one_users_code_never_pairs_to_another_user(configured, fresh):
    configured()
    registry, codes = fresh
    codes.issue("user-A", "a@corp.com")
    b_code = codes.issue("user-B", "b@corp.com")

    with TestClient(app).websocket_connect("/ws/bridge") as socket:
        socket.send_json(hello(pairingCode=b_code))
        socket.receive_json()

    # B's code paired B, and left A with no browser at all.
    assert registry.owners() == []  # disconnected, but it was B who was registered
    assert "user-A" not in registry.owners()
