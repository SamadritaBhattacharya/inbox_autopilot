"""The benchmark runner.

Console output is ASCII only — this runs on a cp1252 terminal, and a report that dies
encoding its own box-drawing characters is a report nobody reads.

    uv run --project backend python -m bench.run_bench
    uv run --project backend python -m bench.run_bench --only summarize_inbox --json out.json

Reports the numbers the PRD commits to, and they are deliberately separate:

**Interrupts are driven, not counted as failures.** A run paused for a human has not
terminated at all — it is waiting, which is the system working. The harness answers on the
user's behalf so every case reaches a real ending, with one absolute rule: **it never
approves anything.** A benchmark that could approve a send would be a benchmark that can
send email, and no reliability number is worth that.

- **typed termination** — did every run end with a code or an explicit success? Target 100%.
  A run that ends `STUCK` with a clear reason PASSES this while failing its task, because
  "it failed and said why" is the reliability property. "It stopped" is the bug.
- **task success** — did it do the job? Target ≥80%, adversarial cases excluded, since those
  are designed to fail and scoring them would just move the number without meaning.
- **guardrail violations** — was a forbidden verb ever dispatched? Target 0, and this one is
  not a percentage: any violation fails the whole run.

Results are written **incrementally**, so a crash halfway keeps every completed row instead
of losing the batch.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from langgraph.types import Command

from app.agent.graph import build_manager_graph
from app.events.emitter import EventEmitter
from app.events.sink import BufferSink
from bench.tasks import SUITE, BenchTask
from tests.fakes.fake_llm import FakeLLMClient
from tests.fakes.fake_surface import FakeEmailSurface


@dataclass
class Result:
    name: str
    adversarial: bool
    task_ok: bool
    typed: bool
    guardrail_ok: bool
    llm_calls: int
    within_budget: bool
    steps: int
    outcome: str
    error_code: str | None
    dispatched: list[str]
    seconds: float

    @property
    def passed(self) -> bool:
        """Everything that must hold. Guardrails are never traded against success."""
        return self.typed and self.guardrail_ok and self.within_budget and (
            self.task_ok or self.adversarial
        )


#: How many human decisions the harness will stand in for before calling it a
#: non-termination. Self-heal is capped, so a healthy run needs far fewer than this.
MAX_RESUMES = 8


async def drive_to_completion(graph, case: BenchTask, config: dict) -> dict:
    """Run to a real ending, answering interrupts the way a user would.

    Options get the RECOMMENDED remedy, which is the behaviour the product optimises for and
    therefore the one worth measuring. Questions get a plain answer. Approvals are always
    DECLINED — never approved, under any case, because a harness that can approve a send is
    a harness that can send email.
    """
    payload: object = {"task": case.task, "thread_id": case.name}

    for _ in range(MAX_RESUMES):
        final = await graph.ainvoke(payload, config)
        interrupts = final.get("__interrupt__")
        if not interrupts:
            return final

        request = interrupts[0].value
        if request.get("approval"):
            payload = Command(resume={"verdict": "reject", "reason": "benchmark never approves"})
        elif request.get("options"):
            payload = Command(resume={"option": 1})
        else:
            payload = Command(resume="benchmark: proceed with a sensible default")

    return final


async def run_one(case: BenchTask) -> Result:
    llm = FakeLLMClient(list(case.script), name="bench")
    surface = case.surface_factory() if case.surface_factory else FakeEmailSurface(list(case.pages))
    sink = BufferSink()
    graph = build_manager_graph(
        llm=llm,
        surface=surface,
        emitter=EventEmitter(sink),
        rules=case.rules,
        max_steps=12,
    )
    config = {"configurable": {"thread_id": f"bench-{case.name}"}}

    started = time.monotonic()
    try:
        final = await drive_to_completion(graph, case, config)
    except Exception as exc:  # a crash is the worst outcome: untyped by definition
        return Result(
            name=case.name,
            adversarial=case.adversarial,
            task_ok=False,
            typed=False,
            guardrail_ok=surface.never_dispatched(*case.forbid),
            llm_calls=llm.call_count,
            within_budget=llm.call_count <= case.max_llm_calls,
            steps=0,
            outcome=f"crashed: {exc}",
            error_code=None,
            dispatched=surface.verbs,
            seconds=time.monotonic() - started,
        )
    elapsed = time.monotonic() - started

    # After driving, a still-paused run means the harness ran out of resumes — which is
    # itself a failure to terminate, and should be reported as one.
    paused = "__interrupt__" in final
    success = final.get("success")
    code = final.get("error_code")
    code_value = getattr(code, "value", code)

    typed = (not paused) and (success is True or code_value is not None)
    task_ok = (
        (success is True)
        if case.expect_success
        else (code_value in case.expect_error if case.expect_error else code_value is not None)
    )

    return Result(
        name=case.name,
        adversarial=case.adversarial,
        task_ok=bool(task_ok),
        typed=typed,
        guardrail_ok=surface.never_dispatched(*case.forbid),
        llm_calls=llm.call_count,
        within_budget=llm.call_count <= case.max_llm_calls,
        steps=int(final.get("step") or 0),
        outcome="paused" if paused else str(final.get("reason") or "")[:80],
        error_code=code_value,
        dispatched=surface.verbs,
        seconds=elapsed,
    )


def report(results: list[Result]) -> int:
    scored = [r for r in results if not r.adversarial]
    width = max(len(r.name) for r in results) + 2

    print(f"\n{'case':<{width}} {'task':<6} {'typed':<7} {'guard':<7} {'calls':<7} outcome")
    print("-" * (width + 46))
    for r in results:
        mark = "ok" if r.passed else "FAIL"
        print(
            f"{r.name:<{width}} {('ok' if r.task_ok else '-'):<6} "
            f"{('ok' if r.typed else 'FAIL'):<7} {('ok' if r.guardrail_ok else 'FAIL'):<7} "
            f"{r.llm_calls:<7} {mark}: {r.outcome[:48]}"
        )

    typed_rate = sum(r.typed for r in results) / len(results)
    success_rate = (sum(r.task_ok for r in scored) / len(scored)) if scored else 1.0
    violations = [r.name for r in results if not r.guardrail_ok]
    over_budget = [r.name for r in results if not r.within_budget]

    print(f"\n  typed termination   {typed_rate:6.0%}   (target 100%)")
    print(f"  task success        {success_rate:6.0%}   (target >=80%, {len(scored)} scored)")
    print(f"  guardrail breaches  {len(violations):6d}   (target 0)")
    print(f"  total model calls   {sum(r.llm_calls for r in results):6d}")

    if violations:
        print(f"\n  ✗ FORBIDDEN VERB DISPATCHED: {', '.join(violations)}")
    if over_budget:
        print(f"  x over model-call budget: {', '.join(over_budget)}")

    ok = typed_rate == 1.0 and success_rate >= 0.8 and not violations and not over_budget
    print("\n" + ("PASS" if ok else "FAIL") + "\n")
    return 0 if ok else 1


async def main() -> int:
    parser = argparse.ArgumentParser(description="Inbox Autopilot benchmark")
    parser.add_argument("--only", help="run one case by name")
    parser.add_argument("--json", type=Path, help="write results incrementally to this file")
    args = parser.parse_args()

    cases = [c for c in SUITE if not args.only or c.name == args.only]
    if not cases:
        print(f"no case named {args.only!r}", file=sys.stderr)
        return 2

    results: list[Result] = []
    for case in cases:
        results.append(await run_one(case))
        if args.json:
            # Written after EVERY case: a crash halfway must not cost the completed rows.
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(
                json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8"
            )

    return report(results)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
