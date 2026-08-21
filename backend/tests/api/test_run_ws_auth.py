"""`/ws/run` refuses an unauthenticated cockpit.

This route was open for most of the project's life: `allow_origins=["*"]` and no check at
all, so anyone who found the URL could start runs against whatever mailbox was paired. These
tests are what make that no longer true.

The guard runs *before* `accept`, so an unauthenticated client never reaches the run manager.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from app.api import main as main_module
from app.auth.tokens import mint, mint_bridge
from app.config.settings import Settings

SECRET = "a-long-server-side-signing-secret"


@pytest.fixture
def secured(monkeypatch):
    settings = Settings(_env_file=None, auth_mode="google", auth_secret=SecretStr(SECRET))
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    return settings


def connect(url: str) -> None:
    """Open the socket and read one frame, so a server-side close surfaces."""
    with TestClient(main_module.app).websocket_connect(url) as socket:
        socket.receive_json()


@pytest.mark.parametrize(
    "url",
    [
        "/ws/run",
        "/ws/run?token=",
        "/ws/run?token=nonsense",
        "/ws/run?token=a.b",
    ],
)
def test_a_cockpit_without_a_valid_session_is_refused(secured, url):
    with pytest.raises(WebSocketDisconnect) as exc:
        connect(url)

    assert exc.value.code == 4401


def test_a_token_signed_with_another_secret_is_refused(secured):
    forged = mint("user-1", "a@b.com", "not-the-server-secret")

    with pytest.raises(WebSocketDisconnect) as exc:
        connect(f"/ws/run?token={forged}")

    assert exc.value.code == 4401


def test_an_expired_session_is_refused(secured):
    with pytest.raises(WebSocketDisconnect) as exc:
        connect(f"/ws/run?token={mint('user-1', 'a@b.com', SECRET, ttl=-1)}")

    assert exc.value.code == 4401


def test_a_bridge_token_is_not_a_cockpit_session(secured):
    """Same signature, same shape. Without the audience check, a paired extension's
    credential would also drive the cockpit."""
    with pytest.raises(WebSocketDisconnect) as exc:
        connect(f"/ws/run?token={mint_bridge('user-1', 'a@b.com', SECRET)}")

    assert exc.value.code == 4401


def test_a_valid_session_is_accepted(secured):
    token = mint("user-1", "priya@corp.com", SECRET)

    with TestClient(main_module.app).websocket_connect(f"/ws/run?token={token}") as socket:
        # Accepted: the socket is open and the run manager is listening. Sending an unknown
        # frame is the cheapest way to prove it did not close on us.
        socket.send_json({"type": "ping"})


def test_auth_off_still_lets_a_laptop_run(monkeypatch):
    """`AUTH_MODE=off` is what keeps a localhost setup usable with no Google project. It is
    also why the startup banner shouts about it."""
    settings = Settings(_env_file=None, auth_mode="off")
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    with TestClient(main_module.app).websocket_connect("/ws/run") as socket:
        socket.send_json({"type": "ping"})
