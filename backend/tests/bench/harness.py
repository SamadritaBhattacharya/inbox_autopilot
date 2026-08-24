"""Running a golden task and turning it into numbers.

The runner is deliberately thin: it builds the same graph the application builds, drives it
the same way the WebSocket layer drives it, and reads the outcome off the final state. It
does not reach inside a node, patch a guard, or shortcut an interrupt — the moment a harness
starts special-casing the system under test, its numbers stop describing production.

Two things it refuses to do:

- **Judge success from the agent's own verdict.** `Complete(success=True)` is an opinion.
  What counts is which actions reached the surface.
- **Rubber-stamp an approval.** Resuming with `{"verdict": "approve"}` goes through the real
  gate, which is why `gate_bypasses` means something.
"""
from __future__ import annotations

from langgraph.types import Command

from app.rules.store import NoRules
from app.surface.base import SurfaceUnavailable
from app.surface.dispatch import approval_fingerprint
from app.workers.approval import is_gated
from tests.bench.metering import MeteringClient
from tests.bench.metrics import INVALID_REFERENT_CODES, Aggregate, RunMetrics
from tests.bench.tasks import GOLDEN, GoldenTask
from tests.fakes.fake_llm import FakeLLMClient
from tests.fakes.fake_surface import FakeEmailSurface


def call_key(call) -> str:
    """A stable identity for one action, for pairing a dispatch with its preview."""
    return f"{call.name}:{sorted(call.args.items())}"


class RecordingSurface(FakeEmailSurface):
    """A `FakeEmailSurface` that also keeps what it RETURNED, not only what it was asked.

    Originally needed because the trajectory could not be trusted: the `act` node hardcoded
    `error_code=None` on its `StepRecord` (`workers/loop.py`), and the linear worker's
    `StepRecord` omitted the field outright (`workers/rules_worker.py`) — both because
    `StepRecord.error_code` was typed as the run-termination `ErrorCode` enum, and a
    dispatch-rejection code such as `STALE_INDEX` is not a member of it. Assigning one
    raised a `ValidationError`, so both nodes silently wrote nothing instead of the code the
    surface actually returned.

    Both are fixed now (`error_code` widened to `str | None`; see its docstring in
    `app/telemetry/records.py`), so `invalid_referents` reads from `final["history"]` —
    the real trajectory, the thing `TrajectoryStore` actually persists in production.

    `results_seen` stays as a cross-check rather than the primary source: a harness that
    special-cases the system it is measuring stops describing production, so once the
    trajectory could be trusted, continuing to read the boundary would itself have become
    the thing to fix. `test_the_trajectory_and_the_boundary_agree` in `test_bench.py` is
    what keeps this honest if that bug class ever comes back.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.results_seen: list = []
        #: call -> the exact text the human was shown. Kept so the harness can recompute
        #: the approval fingerprint the way the real dispatcher does, rather than trusting
        #: that a preview happening at all implies consent to what was previewed.
        self.previews: dict[str, str] = {}

    async def act(self, call):
        result = await super().act(call)
        self.results_seen.append(result)
        return result

    async def preview(self, call) -> str:
        text = await super().preview(call)
        self.previews[call_key(call)] = text
        return text


def _build(task: GoldenTask, thread_id: str):
    llm = MeteringClient(FakeLLMClient(list(task.script)))

    surface = None
    if task.surface:
        surface = RecordingSurface(
            list(task.observations),
            list(task.results),
            unavailable=task.unavailable,
            preview=task.preview,
        )

    # Imported here rather than at module scope so that importing the golden set — to list
    # it, to count it, to render docs from it — never pulls in the whole graph.
    from app.agent.graph import build_manager_graph

    graph = build_manager_graph(
        llm=llm, surface=surface, rules=NoRules(), max_steps=task.max_steps
    )
    return graph, llm, surface, {"configurable": {"thread_id": thread_id}}


async def run_task(task: GoldenTask, *, thread_id: str | None = None) -> RunMetrics:
    """Drive one golden task to its end and measure it."""
    thread_id = thread_id or f"bench-{task.name}"
    graph, llm, surface, config = _build(task, thread_id)

    failures: list[str] = []
    try:
        final = await graph.ainvoke({"task": task.task, "thread_id": thread_id}, config)
        for resume in task.resumes:
            final = await graph.ainvoke(Command(resume=resume), config)
    except SurfaceUnavailable as exc:
        # A surface that cannot be reached should be caught and typed by the graph, not
        # allowed to escape as an exception. If it escapes, that IS the finding.
        return RunMetrics(
            name=task.name,
            passed=False,
            failures=(f"SurfaceUnavailable escaped the graph: {exc}",),
            status="crashed",
            typed_termination=False,
        )
    except AssertionError as exc:
        # The scripted client ran dry: the run wanted more turns than the task provides.
        # A harness bug, and it must read as one rather than as an agent failure.
        return RunMetrics(
            name=task.name,
            passed=False,
            failures=(f"script exhausted — {exc}",),
            status="script-error",
            typed_termination=False,
        )

    interrupted = "__interrupt__" in final
    status = str(final.get("status") or "")
    error_code = final.get("error_code")
    error_code = str(error_code) if error_code else None
    success = final.get("success")

    # ── the contract ──
    if task.expect_interrupt and not interrupted:
        failures.append("expected the run to PAUSE for a human; it ran to completion")
    if interrupted and not task.expect_interrupt:
        failures.append("the run paused for a human when it should have finished")
    if status != task.expect_status:
        failures.append(f"status {status!r}, expected {task.expect_status!r}")
    if error_code != task.expect_error:
        failures.append(f"error_code {error_code!r}, expected {task.expect_error!r}")
    if task.expect_llm_calls is not None and llm.call_count != task.expect_llm_calls:
        failures.append(f"{llm.call_count} llm calls, expected {task.expect_llm_calls}")

    verbs = surface.verbs if surface else []
    for verb in task.expect_actions:
        if verb not in verbs:
            failures.append(f"{verb} never reached the surface (dispatched: {verbs or 'nothing'})")
    for verb in task.forbid_actions:
        if verb in verbs:
            failures.append(f"{verb} reached the surface and must not have")

    # ── safety, counted rather than asserted ──
    #
    # The rule is the dispatcher's own: an irreversible action may reach the surface only
    # when the human approved THE CONTENT they were shown. Checking merely that a preview
    # happened would pass a run where the draft changed after the yes.
    gated = 0
    bypasses = 0
    if surface is not None:
        for call in surface.calls:
            if not is_gated(call, final.get("observation")):
                continue
            gated += 1
            shown = surface.previews.get(call_key(call))
            if shown is None or approval_fingerprint(call, shown) not in surface.approved:
                bypasses += 1
                failures.append(f"{call.name} dispatched with no approval — GATE BYPASS")

    # From the real trajectory — what `TrajectoryStore` actually persists in production.
    # See `RecordingSurface` for the two bugs that used to make this an unconditional zero.
    invalid = sum(
        1
        for record in final.get("history", [])
        if record.node in ("act", "linear") and (record.error_code or "") in INVALID_REFERENT_CODES
    )

    typed = interrupted or success is True or error_code is not None
    if not typed:
        failures.append("terminated with no ErrorCode — an ending nobody can explain")

    tracker = llm.tracker
    return RunMetrics(
        name=task.name,
        passed=not failures,
        failures=tuple(failures),
        steps=int(final.get("step") or 0),
        llm_calls=llm.call_count,
        usage=tracker.totals,
        latency_ms=sum(bucket.latency_ms for bucket in tracker.by_provider().values()),
        status=status,
        error_code=error_code,
        typed_termination=typed,
        gated_dispatched=gated,
        gate_bypasses=bypasses,
        invalid_referents=invalid,
        by_role={role: bucket.usage for role, bucket in tracker.by_role().items()},
        by_provider={name: bucket.usage for name, bucket in tracker.by_provider().items()},
    )


async def run_all(tasks=GOLDEN) -> Aggregate:
    """The whole set, in order. Sequential on purpose — the numbers are what matter, not
    the wall-clock of collecting them, and concurrency would make a flake harder to pin."""
    return Aggregate(tuple([await run_task(task) for task in tasks]))
