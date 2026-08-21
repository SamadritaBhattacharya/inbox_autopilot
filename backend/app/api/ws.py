"""The cockpit WebSocket — transport only.

This file adapts a socket to the graph and does nothing else. No agent logic, no decisions,
no LLM calls: a route that starts deciding things is a bug in the wrong file.

**Interrupts are the interesting part.** The graph pauses by raising `interrupt()`, and the
runtime returns control here with an `__interrupt__` payload. We forward it as a `question`
event and wait — but the *run* is not waiting in memory, it is checkpointed. That is why a
cockpit can disconnect during a question and reconnect ten minutes later to find it still
there.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

from fastapi import WebSocket, WebSocketDisconnect
from langgraph.types import Command

from app.api.run_manager import Run, RunManager
from app.config.container import AppContainer, build_container
from app.events.protocol import AgentEvent
from app.feedback.models import Feedback, FeedbackKind

logger = logging.getLogger(__name__)

#: Process-level so a run outlives the socket that started it.
RUNS = RunManager()


async def drive(run: Run, container: AppContainer, task: str) -> None:
    """Run the graph to completion, forwarding interrupts to the cockpit.

    Emits into the run's fanout (buffer + any attached socket) rather than to a socket
    directly, so everything replays to a cockpit that reconnects later.
    """
    emitter = run.emitter

    # One browser per run, torn down with the run. Registered on the Run before anything can
    # fail, so a crash mid-setup still closes it — a leaked Chromium is invisible until the
    # host runs out of memory.
    try:
        surface, close_surface = await container.new_surface()
    except Exception as exc:
        await emitter.error(f"could not start the browser: {exc}")
        await emitter.run_complete(success=False, reason=str(exc))
        run.mark_finished()
        return
    run.cleanup = close_surface

    # Live browser frames. Best-effort: a run that works without a picture is still a working
    # run, so a screencast that cannot start must never take the run down with it.
    if surface is not None and hasattr(surface, "start_screencast"):
        try:
            await surface.start_screencast(
                lambda jpeg, seq: emitter.frame(jpeg, seq)
            )
        except Exception as exc:
            logger.warning("screencast unavailable: %s", exc)
            await emitter.activity("blind", "no live view for this run")

    # The emitter MUST reach the graph, or nodes stream nothing and the cockpit shows a run
    # that appears to do nothing until it finishes.
    # The SAME vault the surface uses. A separate one would mint tokens at intake that the
    # dispatcher could not resolve — the recipient would exist and be unreachable.
    graph = container.build_graph(
        emitter=emitter,
        feedback=container.feedback,
        surface=surface,
        vault=getattr(surface, "vault", None),
    )
    config = {"configurable": {"thread_id": run.thread_id}}
    payload: object = {"task": task, "thread_id": run.thread_id}

    try:
        while True:
            result = await graph.ainvoke(payload, config)

            interrupts = result.get("__interrupt__")
            if not interrupts:
                await emitter.finalize(
                    bool(result.get("success")),
                    str(result.get("reason") or ""),
                    (code.value if (code := result.get("error_code")) else None),
                )
                await emitter.run_complete(
                    success=result.get("success"), reason=result.get("reason")
                )
                return

            request = interrupts[0].value

            # Three kinds of pause reach here. Approval and options were already
            # emitted by the nodes that raised them — those need the executor to resolve a
            # draft, or the registry to rank remedies, so neither could be built here. A
            # question has no such owner, so it is emitted here.
            if not (request.get("approval") or request.get("options")):
                await emitter.question(
                    str(request.get("question", "")),
                    list(request.get("missing", [])),
                    f"q-{uuid.uuid4().hex[:8]}",
                )

            # The run is checkpointed here, not parked. This await is just this coroutine.
            #
            # An approval carries a deadline, and it is enforced HERE because this is where
            # the waiting is. The gate node cannot do it: LangGraph re-executes a node from
            # the top on resume, so any deadline it computes is recomputed into the future
            # and has never elapsed by the time it is checked.
            waiting = run.expect_answer()
            if request.get("approval"):
                try:
                    answer = await asyncio.wait_for(
                        waiting, timeout=container.settings.approval_timeout_seconds
                    )
                except TimeoutError:
                    answer = {"verdict": "expired", "reason": "no answer before the deadline"}
            else:
                answer = await waiting
            payload = Command(resume=answer)

    except asyncio.CancelledError:
        raise  # stop() already emitted the sentinel
    except Exception as exc:
        logger.exception("run %s failed", run.thread_id)
        await emitter.error(str(exc))
        await emitter.run_complete(success=False, reason=str(exc))
    finally:
        run.mark_finished()
        # Close the browser as soon as the work is done. The run stays *attachable* for its
        # TTL so a late refresh still shows the result, but holding a Chromium open for ten
        # minutes per finished run to achieve that would be absurd.
        if run.cleanup is not None:
            with contextlib.suppress(Exception):
                await run.cleanup()
            run.cleanup = None


async def ws_run(websocket: WebSocket, container: AppContainer | None = None) -> None:
    """One cockpit connection. May start, watch, steer, and stop runs."""
    await websocket.accept()
    container = container or build_container()
    viewing: str | None = None
    sink = None

    async def send(payload: dict) -> None:
        await websocket.send_json(payload)

    async def detach() -> None:
        nonlocal sink
        if viewing and sink is not None:
            run = RUNS.get(viewing)
            if run is not None:
                run.detach(sink)
        sink = None

    try:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")

            if kind == "start":
                await RUNS.gc()  # opportunistic: free stale runs while there is activity
                await detach()
                thread_id = message.get("threadId") or f"run-{uuid.uuid4().hex[:8]}"
                run = RUNS.create(thread_id)
                viewing = thread_id
                sink = await run.attach(send)
                # Through the EMITTER, never straight to the socket: a frame that skips the
                # buffer is a frame a reattaching cockpit never sees, and replay silently
                # stops being a faithful reproduction of the live stream.
                await run.emitter.status("starting", f"run {thread_id}")
                run.task = asyncio.create_task(
                    drive(run, container, str(message.get("task", "")))
                )

            elif kind == "attach":
                # Reconnect to a run already in flight, or learn that it is gone.
                thread_id = str(message.get("threadId", ""))
                run = RUNS.get(thread_id)
                if run is None:
                    await send(
                        AgentEvent(event="run_absent", data={"threadId": thread_id}).to_wire()
                    )
                    continue
                await detach()
                viewing = thread_id
                sink = await run.attach(send)

            elif kind == "answer":
                run = RUNS.get(viewing) if viewing else None
                if run is not None and not run.answer(message.get("answer", "")):
                    logger.debug("answer arrived with nothing waiting for it")

            elif kind == "decision":
                # An approval verdict. Passed through verbatim so the gate — not this
                # transport layer — decides what counts as consent; `decision_from` fails
                # closed on anything it does not recognise.
                run = RUNS.get(viewing) if viewing else None
                if run is not None:
                    run.answer(
                        {
                            "verdict": message.get("verdict"),
                            "edit": message.get("edit", ""),
                            "reason": message.get("reason", ""),
                        }
                    )

            elif kind == "choice":
                # A recovery option. Passed through verbatim so the options node — not this
                # transport layer — interprets it; unlike an approval, an unparseable choice
                # falls back to the RECOMMENDED remedy rather than failing, because every
                # option here is a safe read-only move.
                run = RUNS.get(viewing) if viewing else None
                if run is not None:
                    run.answer(
                        {"option": message.get("option", 1), "text": message.get("text", "")}
                    )

            elif kind == "feedback":
                # Mid-run steering. Recorded and injected on the next turn; acknowledged
                # immediately so the user knows it landed rather than guessing.
                text = str(message.get("text", "")).strip()
                run = RUNS.get(viewing) if viewing else None
                if run is not None and text:
                    await container.feedback.record(
                        Feedback(
                            thread_id=run.thread_id,
                            kind=FeedbackKind(message.get("kind", "correction")),
                            text=text,
                        )
                    )
                    await run.emitter.feedback_ack(text)

            elif kind == "stop":
                if viewing is not None:
                    run = RUNS.get(viewing)
                    if run is not None:
                        await run.emitter.run_complete(stopped=True)
                    await RUNS.remove(viewing)
                    viewing = None
                    sink = None

            # Unknown messages are ignored: a newer cockpit talking to an older backend
            # should degrade, not disconnect.

    except WebSocketDisconnect:
        # The view goes away. The run does not — that is the whole point.
        await detach()
    except Exception:
        logger.exception("cockpit socket error")
        with contextlib.suppress(Exception):
            await websocket.close()
        await detach()
