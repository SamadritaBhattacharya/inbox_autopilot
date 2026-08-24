# Benchmarks

The output of `python -m tests.bench.run`, recorded whenever the golden set or the graph
changes in a way worth comparing against. This file is the baseline B0 promised — see
[IMPROVEMENT-PLAN.md](IMPROVEMENT-PLAN.md) §B0: *"no macro, learned locator, or prompt change
ships unless it beats baseline on success rate without regressing steps or tokens."*

Run it yourself with:

```
cd backend
python -m tests.bench.run
```

Add `-k <substring>` to run a subset, e.g. `-k send/` for just the approval-gate tasks.

---

## Baseline — 2026-08-24

15 golden tasks, scripted `LLMClient`, no browser, no network. Commit: pre-A1 (working tree,
not yet committed — see [IMPROVEMENTS.md](IMPROVEMENTS.md) item 1).

```
task                                ok   steps  calls  tokens  status          code
-----------------------------------------------------------------------------------------
pre/complete-context-runs           yes  0      4      80      done            -
pre/missing-topic-asks              yes  0      1      20      awaiting_human  -
pre/gate-blocks-downstream          yes  0      1      20      awaiting_human  -
pre/answer-resumes                  yes  0      4      80      done            -
pre/summarize-needs-nothing         yes  0      3      60      done            -
pre/unknown-action-asks             yes  0      1      20      awaiting_human  -
send/pauses-before-sending          yes  1      5      80      running         -
send/dispatches-after-approval      yes  2      6      80      done            -
send/rejection-never-dispatches     yes  2      6      80      done            -
triage/archives-without-asking      yes  2      5      60      done            -
read/stays-read-only                yes  2      5      60      done            -
fail/no-tool-call-is-typed          yes  1      6      120     failed          NO_ACTION
fail/step-budget-is-typed           yes  4      7      60      failed          MAX_STEPS
fail/unreachable-surface-is-typed   yes  0      3      60      failed          SURFACE_UNAVAILABLE
recover/stale-index-is-typed        yes  3      6      60      done            -

success 15/15 (100%)  ·  typed termination 100%  ·  steps mean 1.1 p95 3  ·  63 llm calls  ·  940 tokens
approval gates: 1 irreversible actions, 0 bypassed
invalid referents: 1
tokens by role: classifier 540, executor 400
```

### Reading this baseline

- **15/15, 100% typed termination.** Every planned regression test in the golden set already
  holds against today's graph — this run did not find a NEW defect, it measured one already
  known and pinned it (see below).
- **`send/pauses-before-sending` — status `running`, not `awaiting_human`.** A recorded
  defect, deliberately pinned rather than papered over: the context gate sets
  `awaiting_human` when it asks a question (`app/manager/nodes.py`), but the approval gate
  never sets it, so the highest-stakes pause in the system — the one right before mail goes
  out — is indistinguishable from "still working" to anything that reads `status`. Worth
  fixing before A4 puts a real user in front of it.
- **`approval gates: 1 irreversible actions, 0 bypassed`.** The one number on this report
  with an absolute bar rather than a trend — see `docs/IMPROVEMENT-PLAN.md` §B0. Must read 0
  bypassed on every future run, permanently.
- **`invalid referents: 1`.** From `recover/stale-index-is-typed`, added specifically so this
  metric is not a permanent, meaningless zero. It is what B1 (dispatcher-as-validator) will
  move — watch it after that change, not before.
- **Metering note.** `UsageTracker.drain_step_records` exists in `app/llm/usage.py` and
  nothing in `app/` calls it, so live `StepRecord`s never carry LLM usage. The tokens above
  come from a benchmark-only adapter (`tests/bench/metering.py`) that wraps the `LLMClient`
  port directly — the harness's numbers are trustworthy; the trajectory's are not, yet. Still
  open; unrelated to the finding below.
- ~~**`error_code` is dropped on failed actions.**~~ **FIXED by B1**, same day. Two
  independent call sites — `workers/loop.py`'s `act` node and `workers/rules_worker.py`'s
  `linear` node — silently discarded a dispatch rejection's typed code before it reached the
  trajectory, because `StepRecord.error_code` was typed as the run-termination `ErrorCode`
  enum and a dispatch code like `STALE_INDEX` is not a member of it; assigning it directly
  would have raised a `ValidationError`. Widened the field to `str | None` and set the real
  code at both sites. `invalid_referents` above now reads from `final["history"]` — the real
  trajectory — rather than the boundary workaround this bullet used to describe. The number
  itself did not move: it was correct all along, just measured from the wrong place. See
  `docs/IMPROVEMENT-PLAN.md` §B1 for the full account.

The first finding is a pre-existing gap this benchmark run surfaced, not one it introduced,
and is left open — B0's job is to measure, not to change behaviour out from under its own
baseline. The second was closed the same day because it is what B1 needed to be measurable.

---

## History

| Date | Tasks | Success | Typed | Bypasses | Invalid referents | Tokens | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-08-24 | 15 | 100% | 100% | 0 | 1 | 940 | Initial baseline (B0) |
| 2026-08-24 | 15 | 100% | 100% | 0 | 1 | 940 | B1: `invalid_referents` now sourced from the real trajectory, not a boundary workaround. Numbers unchanged — the fix was to what the number MEANT. |
