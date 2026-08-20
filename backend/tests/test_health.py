from __future__ import annotations

from fastapi.testclient import TestClient
from inbox_contracts import PROTOCOL_VERSION

from app.api.main import app

client = TestClient(app)


def test_health_is_ok():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_health_reports_the_protocol_version():
    """The cockpit and executor handshake on this; a mismatch must be visible, not guessed."""
    assert client.get("/health").json()["protocolVersion"] == PROTOCOL_VERSION


def test_health_never_reports_secrets():
    body = client.get("/health").text.lower()
    for forbidden in ("api_key", "apikey", "secret", "token"):
        assert forbidden not in body
