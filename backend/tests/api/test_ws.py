"""The cockpit socket, and the run-outlives-the-socket guarantee.

Driven through a real `TestClient` WebSocket against a graph on fakes — no browser, no
provider. What is under test is the transport contract: what the cockpit receives, in what
order, and what survives a disconnect.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api import ws as ws_module
from app.api.main import app
from app.api.run_manager import RunManager
from app.config.container import build_container
from app.config.settings import Settings
from app.events.protocol import AgentEvent
from app.events.sink import BufferSink
from app.rules.store import NoRules
from tests.fakes.fake_llm import FakeLLMClient, drafted, ok


def intake(action: str, confidence: float = 0.95, **slots):
    return ok(json.dumps({"action": action, "slots": slots, "confidence": confidence}))


@pytest.fixture(autouse=True)
def fresh_runs(monkeypatch):
    """A clean registry per test — runs are process-level by design."""
    manager = RunManager()
    monkeypatch.setattr(ws_module, "RUNS", manager)
    import app.api.main as main_module

    monkeypatch.setattr(main_module, "RUNS", manager)
    return manager


@pytest.fixture
def wired(monkeypatch):
    """Patch the socket's container factory to one built on fakes."""

    def install(llm: FakeLLMClient):
        container = build_container(
            settings=Settings(_env_file=None, groq_api_key="k"),
            llm=llm,
            rules=NoRules(),
        )
        monkeypatch.setattr(ws_module, "build_container", lambda: container)
        return container

    return install


def collect(socket, *, until: str, limit: int = 60) -> list[dict]:
    """Read frames until `until` arrives."""
    seen: list[dict] = []
    for _ in range(limit):
        frame = socket.receive_json()
        seen.append(frame)
        if frame.get("event") == until:
            return seen
    raise AssertionError(f"never saw {until!r}; got {[f.get('event') for f in seen]}")


# ── a complete run ──────────────────────────────────────────────────────────


def test_a_run_streams_to_completion(wired):
    # `summarize` cannot run linearly — reading a mailbox needs a screen — so the router
    # clamps it to decision and a planner call follows. See `topology_for`.
    wired(
        FakeLLMClient(
            [intake("summarize", scope="inbox"), ok("linear"), ok("Read the inbox")]
        )
    )
    with TestClient(app).websocket_connect("/ws/run") as socket:
        socket.send_json({"type": "start", "task": "summarize my inbox", "threadId": "t1"})
        events = [f["event"] for f in collect(socket, until="run_complete")]

    assert "status" in events
    assert "finalize" in events
    assert events[-1] == "run_complete"


def test_run_complete_is_always_sent_even_when_finalize_is_not(wired):
    """Some failure paths produce no `finalize`. A cockpit keying off it alone would hang
    on exactly the runs a user most wants to see the end of."""
    wired(FakeLLMClient([]))  # unscripted: the first call raises
    with TestClient(app).websocket_connect("/ws/run") as socket:
        socket.send_json({"type": "start", "task": "anything", "threadId": "t-err"})
        events = [f["event"] for f in collect(socket, until="run_complete")]

    assert "error" in events
    assert events[-1] == "run_complete"


# ── the human interrupt ─────────────────────────────────────────────────────


def test_an_incomplete_task_asks_and_waits(wired):
    wired(FakeLLMClient([intake("send_email", recipient_identity="P1")]))
    with TestClient(app).websocket_connect("/ws/run") as socket:
        socket.send_json({"type": "start", "task": "email P1", "threadId": "t2"})
        question = collect(socket, until="question")[-1]

    assert "what the email should be about" in question["data"]["question"]
    assert question["data"]["missing"] == ["topic"]


def test_answering_resumes_the_run(wired):
    wired(
        FakeLLMClient(
            [
                intake("send_email", recipient_identity="P1"),
                ok("linear"),  # clamped to decision: composing needs a screen
                ok("Open compose"),
                drafted(),
            ]
        )
    )
    with TestClient(app).websocket_connect("/ws/run") as socket:
        socket.send_json({"type": "start", "task": "email P1", "threadId": "t3"})
        collect(socket, until="question")

        socket.send_json({"type": "answer", "answer": "about the Friday demo"})
        events = [f["event"] for f in collect(socket, until="run_complete")]

    assert "finalize" in events


# ── the run outlives the socket ─────────────────────────────────────────────


def test_a_disconnect_detaches_the_view_but_keeps_the_run(wired, fresh_runs):
    """The whole point of a durable interrupt: a refresh must not destroy the run."""
    wired(FakeLLMClient([intake("send_email", recipient_identity="P1")]))
    client = TestClient(app)

    with client.websocket_connect("/ws/run") as socket:
        socket.send_json({"type": "start", "task": "email P1", "threadId": "keep-me"})
        collect(socket, until="question")

    run = fresh_runs.get("keep-me")
    assert run is not None, "the run must survive the disconnect"


def test_reattaching_replays_everything_that_happened(wired, fresh_runs):
    """Replay is the same code path as live, so a reconnect sees what it would have seen."""
    wired(FakeLLMClient([intake("send_email", recipient_identity="P1")]))
    client = TestClient(app)

    with client.websocket_connect("/ws/run") as socket:
        socket.send_json({"type": "start", "task": "email P1", "threadId": "replay-me"})
        first = [f["event"] for f in collect(socket, until="question")]

    with client.websocket_connect("/ws/run") as socket:
        socket.send_json({"type": "attach", "threadId": "replay-me"})
        replayed = [f["event"] for f in collect(socket, until="question")]

    # Byte-for-byte the same stream: every frame goes through the buffer, so a frame that
    # skipped it would show up here as a shorter replay.
    assert replayed == first
    assert "question" in replayed, "a pending question must still be there"


def test_attaching_to_a_run_that_is_gone_says_so(wired):
    wired(FakeLLMClient([]))
    with TestClient(app).websocket_connect("/ws/run") as socket:
        socket.send_json({"type": "attach", "threadId": "never-existed"})
        frame = socket.receive_json()

    assert frame["event"] == "run_absent"


def test_stopping_tears_the_run_down(wired, fresh_runs):
    wired(FakeLLMClient([intake("send_email", recipient_identity="P1")]))
    with TestClient(app).websocket_connect("/ws/run") as socket:
        socket.send_json({"type": "start", "task": "email P1", "threadId": "stop-me"})
        collect(socket, until="question")
        socket.send_json({"type": "stop"})
        socket.receive_json()

    assert fresh_runs.get("stop-me") is None


# ── mid-run feedback ────────────────────────────────────────────────────────


def test_feedback_is_acknowledged_so_the_user_knows_it_landed(wired):
    """Silent feedback is worse than none: the user sees nothing change and gives up."""
    container = wired(FakeLLMClient([intake("send_email", recipient_identity="P1")]))
    with TestClient(app).websocket_connect("/ws/run") as socket:
        socket.send_json({"type": "start", "task": "email P1", "threadId": "fb"})
        collect(socket, until="question")

        socket.send_json({"type": "feedback", "text": "use a shorter subject"})
        ack = collect(socket, until="feedback_ack")[-1]

    assert ack["data"]["text"] == "use a shorter subject"
    assert container.feedback is not None


def test_an_unknown_message_is_ignored_not_fatal(wired):
    """A newer cockpit talking to an older backend should degrade, not disconnect."""
    wired(FakeLLMClient([intake("summarize", scope="inbox"), ok("linear")]))
    with TestClient(app).websocket_connect("/ws/run") as socket:
        socket.send_json({"type": "some-future-message", "payload": 1})
        socket.send_json({"type": "start", "task": "summarize", "threadId": "fut"})
        events = [f["event"] for f in collect(socket, until="run_complete")]

    assert "run_complete" in events


# ── the registry ────────────────────────────────────────────────────────────


async def test_replay_reports_trimmed_events_rather_than_hiding_them():
    """A replay that silently starts mid-run looks like a run that began strangely."""
    manager = RunManager()
    run = manager.create("trim")
    run.buffer._capacity = 2  # noqa: SLF001 - exercising the overflow path
    for index in range(5):
        await run.fanout.emit(AgentEvent(event="status", data={"i": index}))

    sent: list[dict] = []

    async def collector(payload: dict) -> None:
        sent.append(payload)

    await run.attach(collector)

    assert any("trimmed" in str(frame.get("data", {})) for frame in sent)


async def test_finished_runs_are_collected_after_their_ttl():
    manager = RunManager(ttl_seconds=0)
    run = manager.create("old")
    run.mark_finished()

    assert await manager.gc() == 1
    assert manager.get("old") is None


async def test_a_live_run_is_never_collected():
    manager = RunManager(ttl_seconds=0)
    manager.create("live")
    assert await manager.gc() == 0


async def test_an_answer_with_nothing_waiting_is_not_an_error():
    manager = RunManager()
    assert manager.create("x").answer("hello") is False


async def test_the_buffer_is_always_a_subscriber():
    """Replay and live must be the same stream, or the reattach path drifts."""
    run = RunManager().create("b")
    await run.fanout.emit(AgentEvent(event="status"))
    assert len(run.buffer.events) == 1


async def test_a_failing_viewer_is_dropped_not_retried():
    """One dead socket must not stall a run."""
    run = RunManager().create("d")

    class Broken(BufferSink):
        async def emit(self, event):
            raise RuntimeError("socket closed")

    run.fanout.add(Broken())
    await run.fanout.emit(AgentEvent(event="status"))
    await run.fanout.emit(AgentEvent(event="status"))

    assert len(run.buffer.events) == 2
