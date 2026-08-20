# Implementation Plan — Inbox Autopilot

- **Method:** spec → plan → TDD, one task per commit. Red → green → refactor.
- **Reads:** [`PRD.md`](PRD.md) for the *what*, [`SYSTEM-DESIGN.md`](SYSTEM-DESIGN.md) for the *how*,
  [`ENGINEERING-SPEC.md`](ENGINEERING-SPEC.md) for the *rules*.
- **Checkbox syntax** is deliberate: an agentic contributor works this file task by task.

---

## 0. Global constraints (apply to every task)

- Python ≥ 3.12 managed by **uv**; JS units by a **pnpm** workspace; both driven by `just`.
- **Pydantic v2 in `packages/contracts/src_py/` is the source of truth.** Wire types are authored once
  and the Zod/TS side is generated. Never hand-edit a generated file.
- Provider keys are read from a gitignored `.env`, **server-side only**.
- Every task ends with `just test` green and `just check` clean.
- Every new failure mode gets an `ErrorCode`. Every new verb declares reversibility, timeout, gating.
- Fakes (`FakeEmailSurface`, `FakeLLMClient`) are test doubles **only** — never on a real path.

## 1. Milestone map

| M | Ships | Proves | Depends on |
| --- | --- | --- | --- |
| **M0** | Monorepo scaffold + contracts pipeline + composition root | Every unit installs and builds; contracts generate without drift | — |
| **M1** | LLM gateway + PII vault + funnel over Gmail | Free model calls work; a Gmail page becomes a tokenized `Observation` | M0 |
| **M2** | Manager graph skeleton: `intake` → `context_gate` → `router` | **R3** (100% context) and **R6** (routing) | M1 |
| **M3** | `TriageWorker` + WS streaming + Next.js cockpit | **R1**, **R8** — live triage on screen | M2 |
| **M4** | `ComposeWorker` + `approval_gate` + take-over | **R2** — "send on X to Y", nothing sends without me | M3 |
| **M5** | `diagnose` → `options` + `SkillRegistry` + `RulesWorker` | **R4** self-heal, **R6** linear route at zero LLM calls | M4 |
| **M6** | `CalendarWorker` + failure-layer hardening + benchmark | **R9** reliability numbers, gated invite | M5 |
| **M7** | Cockpit polish, guardrail audit, deploy | End-to-end demo on free infrastructure | M6 |

A one-week sprint runs M0–M7 at roughly one milestone per day. A production build spends the same
sequence over three to four weeks with real hardening at each step. **The order is not negotiable** —
each milestone's acceptance is the next one's foundation.

---

## M0 — Scaffold and contracts

**Goal.** All four units install and build; the Pydantic → JSON Schema → Zod pipeline works; the
composition root exists and is the only place concretes are constructed.

**Produces.** `just` recipes (`setup`, `gen-contracts`, `check`, `test`, `dev-backend`,
`dev-frontend`, `bench`); the `@inbox/contracts` package; an empty but wired `config/container.py`.

### Tasks

- [x] **M0.1 — Root tooling.** `.gitignore`, `.env.example`, `pnpm-workspace.yaml`, root
      `package.json`, `justfile`. `.gitignore` must cover `.env`, `runs/`, `node_modules/`, `.venv/`.
- [x] **M0.2 — `packages/contracts`.** Author `Viewport`, `Element`, `MailContext`, `Observation`,
      `ActionCall`, `ActionResult`, `Envelope`, `PROTOCOL_VERSION` per
      [`WS-PROTOCOL.md §2`](WS-PROTOCOL.md). `scripts/gen.py` emits `schema/*.json`;
      `scripts/gen-zod.mjs` emits `src/generated/*.ts`.
  - *Test:* Pydantic round-trip; emitted schema matches the committed file; vitest parses a valid
    sample with the generated Zod and rejects an invalid one.
  - *Test:* `Observation` **rejects** a payload containing `x`, `y`, `url`, or `html` — invariants 1,
    2 and 4 are schema-level, not convention.
- [x] **M0.3 — Backend skeleton.** `app/api/main.py` with `/health`; `app/config/settings.py`
      (pydantic-settings, `.env`); empty `__init__.py` for every package in the layout.
- [x] **M0.4 — Composition root.** `app/config/container.py` with `build_default_app(...)` accepting
      injectable overrides for every port. It is empty of logic — it only wires.
- [x] **M0.5 — Cockpit scaffold (Next.js 16).** Generated with `create-next-app@latest`
      (App Router, TypeScript, Tailwind v4, ESLint, no `src/`). Renamed to `@inbox/cockpit`, wired to
      the workspace, `lib/env.ts` (Zod-validated `NEXT_PUBLIC_WS_URL`), monochrome theme tokens, and
      a static landing shell that imports `PROTOCOL_VERSION` from `@inbox/contracts` — which is what
      proves the codegen actually reaches a consumer. **The live cockpit itself lands at M3.**
- [ ] **M0.6 — Extension scaffold.** Deferred to **M6.7**, where it is built alongside
      `ExtensionEmailSurface`. There is nothing for it to drive until the funnel and action
      dispatcher exist.
- [x] **M0.7 — CI.** Contract-drift guard + tests + cockpit typecheck/build on push, plus a
      standing secret-scan job (no committed key patterns, `.env` untracked, and `frontend/` may
      reference exactly one `NEXT_PUBLIC_*` variable).

**Acceptance** *(met)*
- ✅ `pnpm run setup` completes on a clean machine (uv fetches Python 3.12; Chromium is a separate
  `setup-browser` recipe so M0 does not pay for a large download it cannot use yet).
- ✅ `pnpm run check` is clean; editing a generated file makes it fail.
- ✅ `pnpm run test` green — 41 pytest + 27 vitest at M0 close. `GET /health` returns 200 with the
  protocol version. `pnpm -C frontend build` succeeds (Next 16.3.1 / Turbopack / React 19.2.8).

**Notes from the build**
- The Zod generator rendered `#/$defs/*` as `z.any()`, silently dropping `.strict()` from nested
  `Element` / `Viewport` / `MailContext`. That would have left the no-coordinates invariant enforced
  in Python but **not** in TypeScript. `scripts/gen-zod.mjs` now dereferences `$ref` before emitting;
  both sides are tested against the same hostile payloads.
- Pydantic does not serialize `default_factory` into JSON Schema, so `elements` generated as
  `Element[] | undefined` while always being a list in Python. `scripts/gen.py` now emits explicit
  container defaults so the two sides cannot disagree.
- `create-next-app` writes a nested `pnpm-workspace.yaml` inside `frontend/`, which would make it
  its own workspace root and detach it from the monorepo. Removed; its `allowBuilds` entries were
  merged into the root file.

**Next.js 16 facts that change M3** *(from `frontend/node_modules/next/dist/docs`, not from memory —
the scaffold ships an `AGENTS.md` warning that v16 diverges from training data, and it is right)*
- **Request APIs are async-only.** `params`, `searchParams`, `cookies()`, `headers()` can no longer
  be read synchronously. `app/run/[threadId]/page.tsx` must `await params`.
- **Route types are generated**, not hand-written: `LayoutProps<"/">`, `PageProps<"/run/[threadId]">`
  come from `next typegen`. A bare `tsc --noEmit` on a clean checkout fails until typegen runs, so
  the `typecheck` script and CI both run it first.
- **Turbopack is the default** bundler for `dev` and `build`.
- `middleware` is renamed to `proxy`; `revalidateTag` now requires a `cacheLife` argument.

---

## M1 — LLM gateway, PII vault, and the funnel over Gmail

**Goal.** The two things that make the rest possible: free model calls with fallback, and Gmail's DOM
becoming a tokenized `Observation`.

### Tasks

- [x] **M1.1 — `LLMClient` port + `FallbackLLMClient`.** Ordered chain; advance on 429/quota/5xx;
      retry transient errors with backoff respecting `Retry-After`; **never re-route inside a retry**.
  - *Test:* provider 1 returns 429 → provider 2 is called, same messages, once.
  - *Test:* a retry uses the same model; a fallback uses the next provider.
  - *Test:* all providers exhausted → `PROVIDER_EXHAUSTED`, not an unhandled exception.
- [ ] **M1.2 — Provider adapters.** `GroqClient`, `OpenRouterClient`, `GeminiClient`, all
      OpenAI-compatible behind the port. Model slugs from settings, per role.
  - *Test:* no model ID string appears anywhere outside settings (a grep-based test).
- [ ] **M1.3 — Metering.** `UsageTracker` → `StepRecord{provider, role, input_tokens,
      output_tokens, latency_ms}` on **every** call, including classifier and validator roles.
- [ ] **M1.4 — `PiiVault` + `PiiTokenizer`.** Deterministic tokenization of addresses, phones, and
      identifiers; best-effort names. Stable within a session, never reused across sessions,
      in-memory only.
  - *Test:* stability (same input → same token within a run), isolation (new session → new
    numbering), completeness on the deterministic classes.
  - *Test:* `resolve()` on an unknown token raises rather than returning a passthrough.
- [ ] **M1.5 — Redaction filters.** Install on the logger, the trajectory writer, the event emitter,
      and error-reason construction — all eight egress points in
      [`SECURITY-MODEL.md §2.4`](SECURITY-MODEL.md).
- [ ] **M1.6 — Funnel stages.** `Extract`, `VisibilityFilter`, `OcclusionCuller`,
      `WrapperCollapser`, `PiiTokenizer`, `SoMIndexer`, `ReadingOrderFormatter` — one class each,
      composed into `run_funnel()`.
  - *Test:* each stage in isolation against synthetic snapshot fixtures.
  - *Test:* **stage ordering** — the tokenizer runs before the indexer and formatter. Reordering the
    pipeline must fail a test, because that reordering is a security regression.
  - *Test:* the budget path reports `droppedCount` and never truncates silently.
- [ ] **M1.7 — `PlaywrightEmailSurface`.** `EmailSurface` port over Playwright + raw CDP; headful +
      stealth; `observe()` runs the funnel, `act()` resolves index and token then dispatches trusted
      `Input.*`.
- [ ] **M1.8 — Gmail fixtures.** Recorded-DOM fixtures for inbox, thread, and compose views. These
      are what CI runs against; no live account in CI.

**Acceptance**
- A Gmail fixture page becomes an `Observation` of ~1–3k tokens with a numbered element list.
- **Zero** raw addresses in the observation, the LLM request, the events, the trajectory, or the logs
  (`test_no_raw_pii_egress` green).
- A real call succeeds through Groq and, with Groq forced to 429, through the next provider.

---

## M2 — Manager graph: intake, context gate, router

**Goal.** R3 and R6. The agent refuses to start half-informed, and routes deterministic work away
from the LLM.

### Tasks

- [ ] **M2.1 — `AgentState`.** Pydantic model per [`SYSTEM-DESIGN.md §4`](SYSTEM-DESIGN.md), append
      reducers on `messages` and `history`.
  - *Test:* checkpoint serde round-trip including `ErrorCode` **inside** a `StepRecord`. Custom types
    nested in checkpointed state are the classic silent-drop bug: the checkpoint of a *failed* run
    quietly loses its error code, so the failure path is exactly where the gap hides.
- [ ] **M2.2 — `intake`.** NL → `TaskIntent` via the classifier role. No side effects.
- [ ] **M2.3 — Slot registry.** The table in [`SYSTEM-DESIGN.md §5.1`](SYSTEM-DESIGN.md) as data, not
      prompt text.
- [ ] **M2.4 — `context_gate`.** Compute `missing_slots` + confidence; `AskUser` interrupt; loop
      until confidence ≥ τ. Read-only observation permitted for disambiguation.
  - *Test:* `test_context_gate_blocks_dispatch` — no worker runs while slots are missing.
  - *Test:* a durable pause survives a rebuilt graph (resume from the checkpoint, not from memory).
  - *Test:* disambiguation performs **zero** mutating actions.
- [ ] **M2.5 — `router`.** Rule pre-check first (zero LLM calls on a match), classifier otherwise.
      Typed `Route`. Escalation valve from linear → decision.
- [ ] **M2.6 — `planner`.** Decision route only; posts `Plan(steps)`.
- [ ] **M2.7 — Routing functions.** All six pure functions from
      [`SYSTEM-DESIGN.md §3.2`](SYSTEM-DESIGN.md), **every branch** covered.
- [ ] **M2.8 — Graph assembly + checkpointer.** `InMemorySaver` in dev with the allowed-modules serde
      configured; SQLite behind a setting.

**Acceptance**
- "send an email to priya" (no topic) raises `AskUser` and mutates nothing.
- Answering it advances the run; the graph resumes from the checkpoint after a simulated restart.
- "archive all newsletters" routes `linear` with zero classifier calls.
- Every routing branch has a passing test.

---

## M3 — TriageWorker, streaming, and the cockpit

**Goal.** R1 and R8. Live triage visible on screen.

### Tasks

- [ ] **M3.1 — Worker interface + registry.** Adding a worker = a class + one registration line.
- [ ] **M3.2 — `TriageWorker`.** `observe → reason → act` subgraph over the email verbs
      (archive, label, snooze, mark-read, read-thread).
  - *Test:* `test_triage_worker_has_no_send_tool` — gated verbs are absent from the bound schema.
- [ ] **M3.3 — Tool specs.** Pydantic tool models with docstring descriptions, per verb, bound
      natively (never free-text parsed).
- [ ] **M3.4 — Action dispatcher.** One handler per verb; per-action timeout wall; dispatch-time
      validation (`STALE_INDEX`, `UNKNOWN_TOKEN`, `VERB_NOT_BOUND`); `undo` payload on mutating
      verbs.
- [ ] **M3.5 — Settle + re-observe.** Adaptive per-host bound (`mean + 2σ`, clamped). Indices
      rebuilt every turn.
  - *Test:* `test_indices_not_reused`.
- [ ] **M3.6 — Loop guards.** Repetition guard (nudge 3 / kill 5), stuck-signature (nudge 2 / kill
      8), runaway-output clip at ~3k chars, think-before-act, no-tool-call nudge, step budget
      injection at ≤5 remaining.
  - *Test:* one per guard, asserting the emitted `ErrorCode`.
- [ ] **M3.7 — `EventSink` + emitter + WS hub.** Every event type in
      [`WS-PROTOCOL.md §3.2`](WS-PROTOCOL.md); `astream(stream_mode="updates")` forwarding.
- [ ] **M3.8 — Run manager.** Process-level registry keyed by `thread_id`; attach/detach/replay/GC.
      **The run outlives the socket.**
  - *Test:* disconnect mid-run → the run continues → `attach` replays the buffer and goes live.
- [ ] **M3.9 — Screencast.** CDP frames → `frame` events.
- [ ] **M3.10 — Next.js cockpit.** `app/run/[threadId]/page.tsx` server shell + `CockpitClient`
      (`"use client"`), `useAgentRun`, append-only `eventStore`, `Transcript`, `Viewport` (canvas),
      `QuestionCard`, `Composer`.

**Acceptance**
- A triage task runs end to end with the LHS showing task → plan → reasoning → tool calls and the RHS
  showing live frames.
- A cockpit refresh mid-run re-attaches and replays; the browser never restarts.
- Every guard fires under its fixture and produces its typed code.

---

## M4 — ComposeWorker and the approval gate

**Goal.** R2. The headline flow, and the guarantee that nothing sends without a human.

### Tasks

- [ ] **M4.1 — `Approver` port + `ApprovalRequest` / `Decision`.**
- [ ] **M4.2 — `approval_gate` node.** Interrupt-based; payload-matched; expiry →
      `APPROVAL_TIMEOUT`; reject with no alternative → `APPROVAL_REJECTED_NO_ALT`.
  - *Test:* `test_no_send_without_approval` — every send path fails closed with the approver absent.
  - *Test:* a decision approving payload A does **not** authorize a mutated payload B.
  - *Test:* no `RemediationStrategy` can produce an approval decision.
- [ ] **M4.3 — `ComposeWorker`.** Compose → To → Subject → Body as **separate observable steps**;
      token→address resolution at dispatch only.
  - *Test:* the trajectory shows four distinct fill actions, not one atomic jump.
- [ ] **M4.4 — Draft preview.** Resolved draft rendered for the human; asserted absent from
      `messages`, the trajectory, and every LLM request body.
- [ ] **M4.5 — Edit / take-over.** Edit replaces field content and returns to the fill step; it does
      **not** approve.
- [ ] **M4.6 — `verify` for send.** Contract check: is the message in Sent, and does it match the
      intended recipient token?
- [ ] **M4.7 — `ApprovalCard`** in the cockpit: Approve / Edit / Reject with the resolved preview.

**Acceptance**
- J1 from [`PRD.md §7`](PRD.md) runs end to end.
- The approval-bypass suite is green — there is no code path to a send without a matching decision.
- The user watches each field fill live before being asked.

---

## M5 — Self-heal and the linear route

**Goal.** R4 and the zero-LLM path of R6.

### Tasks

- [ ] **M5.1 — Cause classifier.** Pure function per the table in
      [`SYSTEM-DESIGN.md §9.2`](SYSTEM-DESIGN.md); a test per row.
- [ ] **M5.2 — `RemediationStrategy` + `SkillRegistry`.** The eight v1 strategies; each unit-tested in
      isolation for `applies_to` scoring and `to_option` output.
- [ ] **M5.3 — `diagnose` node.** Cause + plain-language explanation + evidence.
- [ ] **M5.4 — `options` node.** Four ranked options, `[1]` marked Recommended, `[4]` always
      free-form; interrupt; resume with the chosen remedy.
  - *Test:* option 4 always present; free text becomes loop guidance.
  - *Test:* **anti-loop** — the same cause twice recomputes options excluding attempted strategies;
    a third occurrence finalizes instead of asking again.
- [ ] **M5.5 — `RulesStore` + matcher + soft-guidance renderer.**
- [ ] **M5.6 — `RulesWorker`.** Linear topology, batch execution.
  - *Test:* `test_linear_route_zero_llm_calls`.
  - *Test:* auto-send is off by default and cannot be enabled by config alone.
- [ ] **M5.7 — `OptionsCard`** in the cockpit.

**Acceptance**
- J3 from [`PRD.md §7`](PRD.md) runs: overlay fixture → diagnosis → four options → chosen remedy →
  loop resumes → task completes.
- A rules task records zero LLM calls in the trajectory.

---

## M6 — Calendar, hardening, benchmark

**Goal.** R9 with numbers attached.

### Tasks

- [ ] **M6.1 — `CalendarWorker`.** Extract event details from a thread → build the event → **approval
      on invite dispatch**.
- [ ] **M6.2 — Compaction.** Layer 0 (strip seen screenshots/observations), layer 1 (truncate old
      tool outputs), layer 2 (LLM-summarize the middle, keep first-2 + last-6 verbatim). Trigger at
      ~95% of the window on real API `input_tokens`.
- [ ] **M6.3 — Prompt caching.** Stable cache-marked prefix; growing memory in a later message so
      appends do not bust the cache.
- [ ] **M6.4 — Batched classification.** One classifier call scores N subjects for triage.
- [ ] **M6.5 — Benchmark harness.** Fixture mailbox + scripted tasks + expected outcomes; reports
      success rate, steps, tokens, and **% terminated with a typed code**.
- [ ] **M6.6 — Adversarial fixtures.** Overlay-blocked compose; moved button; oscillation bait;
      **the prompt-injection email** from [`SECURITY-MODEL.md §4.1`](SECURITY-MODEL.md); a provider
      forced to 429 mid-run.
- [ ] **M6.7 — `ExtensionEmailSurface`.** TS funnel + tokenizer + `chrome.debugger` dispatch + relay
      client. Swapped in for `PlaywrightEmailSurface` with **zero graph changes** — this is the SOLID
      payoff and the test that proves the architecture held.

**Acceptance**
- Benchmark reports **100%** typed-termination and ≥80% task success on the fixture suite.
- The injection fixture produces zero send-shaped dispatches.
- Swapping the surface requires exactly one line in the composition root.

---

## M7 — Polish, audit, deploy

### Tasks

- [ ] **M7.1 — Cockpit polish.** Run history (`app/history`), reattach UX, "waking" state for a
      sleeping Space, error surfaces, responsive two-pane layout.
- [ ] **M7.2 — Guardrail audit.** Walk every ❌ in [`ENGINEERING-SPEC.md §3`](ENGINEERING-SPEC.md) and
      record where each is enforced and which test proves it. Anything unproven becomes a task.
- [ ] **M7.3 — Build inspection in CI.** Assert no provider key in the frontend bundle or the packed
      extension.
- [ ] **M7.4 — Deploy.** Backend to HF Space (Docker + xvfb, port 7860); frontend to Vercel;
      Postgres checkpointer wired behind a setting.
- [ ] **M7.5 — README + demo script.** The three PRD journeys, reproducible.

**Acceptance**
- A cold visitor runs J1, J2, and J3 against the deployed stack.
- Recurring cost is $0.
- The guardrail audit table has no unproven row.

---

## 2. Post-v1 backlog (explicitly not now)

| Item | Gate |
| --- | --- |
| **Dev-assist mode** — sandboxed source reading + patch proposal with tests, human-reviewed, never in the live loop ([ADR-009](ADR.md#adr-009)) | after M7, in its own process |
| Multi-provider mail (Outlook, IMAP) via a new `EmailSurface` | after the extension surface proves the port |
| `body_summary_only` privacy mode | after M6 measures the token cost |
| Compound route (multi-worker plans) | when a real task needs it, not before |
| Team inboxes, RBAC | v2 product decision |
| Learned rules (promote repeated agent decisions into `RulesStore`) | needs trajectory volume first |

## 3. Task discipline

The rhythm every milestone follows:

1. **A dated spec before a plan.** Context, scope, an explicit **decisions log** with rationale, and a
   stated "out of scope (YAGNI)" list.
2. **A plan with numbered tasks**, each declaring the files it touches and the interfaces it
   consumes/produces, with checkboxes.
3. **One task, one commit, one green run.** Never batch.
4. **The decisions log is the artifact that ages best.** Six months on, the reasoning is worth more
   than the code — write down what was rejected and why, not only what was chosen.
