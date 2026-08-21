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
- [x] **M0.7 — Verification gate.** `pnpm run verify` = contract-drift guard → security guards →
      lint → tests → cockpit typecheck. **Run it before every commit.**
      `scripts/guards.py` enforces: no provider-key pattern in any file git would track, `.env`
      untracked, and `frontend/` referencing exactly one `NEXT_PUBLIC_*` variable.
      *Hosted CI is deliberately not wired up yet* — the gate runs locally and the same commands
      drop into a workflow unchanged whenever the repo gets a remote.

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
- [x] **M1.2 — Provider adapters.** `GroqClient`, `OpenRouterClient`, `GeminiClient`, all
      OpenAI-compatible behind the port. Model slugs from settings, per role.
  - *Test:* no model ID string appears anywhere outside settings (a grep-based test).
- [x] **M1.3 — Metering.** `UsageTracker` → `StepRecord{provider, role, input_tokens,
      output_tokens, latency_ms}` on **every** call, including classifier and validator roles.
- [x] **M1.4 — `PiiVault` + `PiiTokenizer`.** Deterministic tokenization of addresses, phones, and
      identifiers; best-effort names. Stable within a session, never reused across sessions,
      in-memory only.
  - *Test:* stability (same input → same token within a run), isolation (new session → new
    numbering), completeness on the deterministic classes.
  - *Test:* `resolve()` on an unknown token raises rather than returning a passthrough.
- [x] **M1.5 — Redaction filters.** Install on the logger, the trajectory writer, the event emitter,
      and error-reason construction — all eight egress points in
      [`SECURITY-MODEL.md §2.4`](SECURITY-MODEL.md).
> **M1 progress: gateway and security done (M1.1–M1.5). The funnel is next (M1.6–M1.8).**
>
> **What the live smoke test caught that mocks could not.** Two real bugs, both invisible to a
> hermetic suite, both found by exactly one call per provider:
>
> 1. **The model roster rotated out from under the config.** `llama-3.3-70b-versatile` and
>    `llama-3.1-8b-instant` no longer exist on the account; the whole Llama family is gone. This is
>    [ADR-008](ADR.md#adr-008)'s "never hardcode a slug" rule proving itself inside the project's own
>    first week. Defaults moved to `openai/gpt-oss-120b` / `openai/gpt-oss-20b`, and `.env.example`
>    now carries the one-liner for listing what a key can actually reach. Note the failure classified
>    **correctly**: a 404 became `ProviderBadRequest`, which does *not* fall through — so the chain
>    did not waste OpenRouter and Gemini on the same bad slug.
> 2. **Reasoning models return no `content` at all.** On a tool-calling turn, `gpt-oss` puts its
>    chain of thought in `message.reasoning` and leaves `content` empty — 99 output tokens billed,
>    empty text parsed. Reading only `content` would have failed think-before-act on **every turn**
>    (`REASONING_MISSING`) while showing the cockpit nothing, and the cause would have looked like a
>    model problem rather than a parsing one. `LLMResult` now carries `text` *and* `reasoning`, and
>    `explanation` prefers the latter. Covered hermetically for both `reasoning` and
>    `reasoning_content` spellings, so it cannot regress without a live key.

- [x] **M1.6 — Funnel stages.** `Extract`, `VisibilityFilter`, `OcclusionCuller`,
      `WrapperCollapser`, `PiiTokenizer`, `SoMIndexer`, `ReadingOrderFormatter` — one class each,
      composed into `run_funnel()`.
  - *Test:* each stage in isolation against synthetic snapshot fixtures.
  - *Test:* **stage ordering** — the tokenizer runs before the indexer and formatter. Reordering the
    pipeline must fail a test, because that reordering is a security regression.
  - *Test:* the budget path reports `droppedCount` and never truncates silently.
- [x] **M1.7 — `PlaywrightEmailSurface`.** `EmailSurface` port over Playwright + raw CDP; headful +
      stealth; `observe()` runs the funnel, `act()` resolves index and token then dispatches trusted
      `Input.*`.
- [x] **M1.8 — Gmail fixtures.** Recorded-DOM fixtures for inbox, thread, and compose views. These
      are what CI runs against; no live account in CI.

> **M1 complete.** 234 hermetic tests + 17 browser tests + 2 live provider calls.
>
> **What the browser found that synthetic fixtures could not.** I built precise hit-testing
> into the extractor (`elementFromPoint` — "would a click here actually reach you?") and then
> had the occlusion stage ignore it and re-derive coverage *geometrically*. Against a real
> open dialog that gets the answer exactly backwards: the blocking layer is a full-viewport
> overlay, which my own backdrop heuristic **exempts** from being an occluder, so every row
> behind the dialog stayed listed and clickable. Geometry cannot distinguish a transparent
> scrim from a real cover. The stage now believes the browser when it has an answer and
> falls back to geometry only for elements whose centre is off-screen and cannot be tested.
>
> **Environment note.** `playwright install` **exits 0 even when the download fails**, so a
> missing browser surfaces much later as an unrelated "executable doesn't exist". Here it
> failed because the machine had ~0.1 GB free. `resolve_chromium()` now finds the newest
> build already on disk (`PLAYWRIGHT_CHROMIUM_PATH` overrides), which is why the browser
> suite runs at all on this machine; the integration tests skip cleanly when none is found.
>
> **Still open:** the fixture covers inbox + compose. A **thread view** fixture is needed
> before the reply and calendar flows (M4/M6).

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

- [x] **M2.1 — `AgentState`.** Pydantic model per [`SYSTEM-DESIGN.md §4`](SYSTEM-DESIGN.md), append
      reducers on `messages` and `history`.
  - *Test:* checkpoint serde round-trip including `ErrorCode` **inside** a `StepRecord`. Custom types
    nested in checkpointed state are the classic silent-drop bug: the checkpoint of a *failed* run
    quietly loses its error code, so the failure path is exactly where the gap hides.
- [x] **M2.2 — `intake`.** NL → `TaskIntent` via the classifier role. No side effects.
- [x] **M2.3 — Slot registry.** The table in [`SYSTEM-DESIGN.md §5.1`](SYSTEM-DESIGN.md) as data, not
      prompt text.
- [x] **M2.4 — `context_gate`.** Compute `missing_slots` + confidence; `AskUser` interrupt; loop
      until confidence ≥ τ. Read-only observation permitted for disambiguation.
  - *Test:* `test_context_gate_blocks_dispatch` — no worker runs while slots are missing.
  - *Test:* a durable pause survives a rebuilt graph (resume from the checkpoint, not from memory).
  - *Test:* disambiguation performs **zero** mutating actions.
- [x] **M2.5 — `router`.** Rule pre-check first (zero LLM calls on a match), classifier otherwise.
      Typed `Route`. Escalation valve from linear → decision.
- [x] **M2.6 — `planner`.** Decision route only; posts `Plan(steps)`.
- [x] **M2.7 — Routing functions.** All six pure functions from
      [`SYSTEM-DESIGN.md §3.2`](SYSTEM-DESIGN.md), **every branch** covered.
- [x] **M2.8 — Graph assembly + checkpointer.** `InMemorySaver` in dev with the allowed-modules serde
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

- [x] **M3.1 — Worker interface + registry.** Adding a worker = a class + one registration line.
- [x] **M3.2 — `TriageWorker`.** `observe → reason → act` subgraph over the email verbs
      (archive, label, snooze, mark-read, read-thread).
  - *Test:* `test_triage_worker_has_no_send_tool` — gated verbs are absent from the bound schema.
- [x] **M3.3 — Tool specs.** Pydantic tool models with docstring descriptions, per verb, bound
      natively (never free-text parsed).
- [x] **M3.4 — Action dispatcher.** One handler per verb; per-action timeout wall; dispatch-time
      validation (`STALE_INDEX`, `UNKNOWN_TOKEN`, `VERB_NOT_BOUND`); `undo` payload on mutating
      verbs.
- [x] **M3.5 — Settle + re-observe.** Adaptive per-host bound (`mean + 2σ`, clamped). Indices
      rebuilt every turn.
  - *Test:* `test_indices_not_reused`.
- [x] **M3.6 — Loop guards.** Repetition guard (nudge 3 / kill 5), stuck-signature (nudge 2 / kill
      8), runaway-output clip at ~3k chars, think-before-act, no-tool-call nudge, step budget
      injection at ≤5 remaining.
  - *Test:* one per guard, asserting the emitted `ErrorCode`.
- [x] **M3.7 — `EventSink` + emitter + WS hub.** Every event type in
      [`WS-PROTOCOL.md §3.2`](WS-PROTOCOL.md); `astream(stream_mode="updates")` forwarding.
- [x] **M3.8 — Run manager.** Process-level registry keyed by `thread_id`; attach/detach/replay/GC.
      **The run outlives the socket.**
  - *Test:* disconnect mid-run → the run continues → `attach` replays the buffer and goes live.
- [ ] **M3.9 — Screencast.** CDP frames → `frame` events.
- [x] **M3.10 — Next.js cockpit.** `app/run/[threadId]/page.tsx` server shell + `CockpitClient`
      (`"use client"`), `useAgentRun`, append-only `eventStore`, `Transcript`, `Viewport` (canvas),
      `QuestionCard`, `Composer`.

**Acceptance**
- A triage task runs end to end with the LHS showing task → plan → reasoning → tool calls and the RHS
  showing live frames.
- A cockpit refresh mid-run re-attaches and replays; the browser never restarts.
- Every guard fires under its fixture and produces its typed code.

---

> **M3 note — what the first live run taught, that 375 passing tests had not.**
>
> A real model on a real browser found five defects the suite could not see, because tests
> assert on final state while a cockpit watches a *stream*:
>
> 1. **The emitter never reached the graph.** Every node streamed into a `NullSink`; the run
>    looked frozen until it finished. Tests passed throughout.
> 2. **`compose_open` used `querySelector` without checking visibility.** Gmail's compose
>    markup lives in the DOM permanently, so the agent believed compose was open on a plain
>    inbox, forever. A hidden element has a zero-size box — measuring is the reliable test.
> 3. **Clickable rows had no name.** `nameOf` took own-text only, so the one element you
>    actually click rendered as `[4] generic: ""` — a number the model could not reason
>    about. Interactive elements now fall back to subtree text.
> 4. **`ReadThread` was bindable but not performable** — bound to the model, no handler on the
>    surface. The model found out by calling it.
> 5. **The surface enforced verb binding from its own timeout table**, not the worker's
>    capability set, so a read-only run had a dispatchable `Send`. That check moved into the
>    act node, which is the only layer that knows which worker is running.
>
> Two more emerged from watching the agent *reason*:
>
> - **`droppedCount` without a direction is half a fix.** Told "12 more items", the agent
>   scrolled down, saw the number unchanged, and scrolled down again. `Observation.hint` now
>   says "5 above, 7 below" (protocol 1.1.0) — and the agent immediately scrolled the right
>   way.
> - **Repeatable verbs can still oscillate.** Scroll-down/scroll-up/scroll-down is a loop
>   built entirely of actions the repetition guard exempts, and the page genuinely changes
>   each step so the stuck guard misses it too. Added an oscillation guard: a short cycle
>   over ≤2 distinct actions nudges, then terminates `STUCK`.
>
> And one regression **I introduced and the suite caught only because I re-ran the probe**:
> folding sender chips into their rows removed the only structured occurrence of a name, so
> person registration never saw it and names stopped being tokenized. Registration now runs
> over the raw element set before any stage can prune — *pruning decides what the model
> sees, never what the vault knows.*

## M4 — ComposeWorker and the approval gate

**Goal.** R2. The headline flow, and the guarantee that nothing sends without a human.

### Tasks

- [x] **M4.1 — `Approver` port + `ApprovalRequest` / `Decision`.**
- [x] **M4.2 — `approval_gate` node.** Interrupt-based; payload-matched; expiry →
      `APPROVAL_TIMEOUT`; reject with no alternative → `APPROVAL_REJECTED_NO_ALT`.
  - *Test:* `test_no_send_without_approval` — every send path fails closed with the approver absent.
  - *Test:* a decision approving payload A does **not** authorize a mutated payload B.
  - *Test:* no `RemediationStrategy` can produce an approval decision.
- [x] **M4.3 — `ComposeWorker`.** Compose → To → Subject → Body as **separate observable steps**;
      token→address resolution at dispatch only.
  - *Test:* the trajectory shows four distinct fill actions, not one atomic jump.
- [x] **M4.4 — Draft preview.** Resolved draft rendered for the human; asserted absent from
      `messages`, the trajectory, and every LLM request body.
- [x] **M4.5 — Edit / take-over.** Edit replaces field content and returns to the fill step; it does
      **not** approve.
- [x] **M4.6 — `verify` for send.** Contract check: is the message in Sent, and does it match the
      intended recipient token?
- [x] **M4.7 — `ApprovalCard`** in the cockpit: Approve / Edit / Reject with the resolved preview.

**Acceptance**
- J1 from [`PRD.md §7`](PRD.md) runs end to end.
- The approval-bypass suite is green — there is no code path to a send without a matching decision.
- The user watches each field fill live before being asked.

---

## M5 — Self-heal and the linear route

**Goal.** R4 and the zero-LLM path of R6.

### Tasks

- [x] **M5.1 — Cause classifier.** Pure function per the table in
      [`SYSTEM-DESIGN.md §9.2`](SYSTEM-DESIGN.md); a test per row.
- [x] **M5.2 — `RemediationStrategy` + `SkillRegistry`.** The eight v1 strategies; each unit-tested in
      isolation for `applies_to` scoring and `to_option` output.
- [x] **M5.3 — `diagnose` node.** Cause + plain-language explanation + evidence.
- [x] **M5.4 — `options` node.** Four ranked options, `[1]` marked Recommended, `[4]` always
      free-form; interrupt; resume with the chosen remedy.
  - *Test:* option 4 always present; free text becomes loop guidance.
  - *Test:* **anti-loop** — the same cause twice recomputes options excluding attempted strategies;
    a third occurrence finalizes instead of asking again.
- [x] **M5.5 — `RulesStore` + matcher + soft-guidance renderer.**
- [x] **M5.6 — `RulesWorker`.** Linear topology, batch execution.
  - *Test:* `test_linear_route_zero_llm_calls`.
  - *Test:* auto-send is off by default and cannot be enabled by config alone.
- [x] **M5.7 — `OptionsCard`** in the cockpit.

**Acceptance**
- J3 from [`PRD.md §7`](PRD.md) runs: overlay fixture → diagnosis → four options → chosen remedy →
  loop resumes → task completes.
- A rules task records zero LLM calls in the trajectory.

---

> **M4/M5 note — three bugs the graph found once the recovery layer existed.**
>
> 1. **Self-heal was unreachable from the common failures.** `reason` and `observe` routed
>    terminal states straight to `finalize`, and *most* failures are detected in `reason` —
>    STUCK, MAX_STEPS, REASONING_MISSING, NO_ACTION all end the run from there. The whole
>    recovery layer was only reachable from the rarer path where an action had already been
>    dispatched. Both now route through `verify`.
>
> 2. **`diagnose` produced a diagnosis and then finalized anyway.** The run arrives already
>    marked `finished` — that is *how* it got diagnosed — so the next router short-circuited
>    past the options it had just been diagnosed for. Diagnose now clears the flag while
>    keeping the error code, since only a chosen remedy earns the right to clear that.
>
> 3. **The approval deadline could never fire.** LangGraph re-executes a node from the top
>    on resume, so `expires_at` was recomputed into the future and had never elapsed by the
>    time it was checked. The deadline moved to the transport, which is where the waiting
>    actually happens, and `Verdict.EXPIRED` makes "nobody answered" distinct from "someone
>    declined". The same replay behaviour was flashing duplicate approval and options cards;
>    request ids are now derived from state rather than random, and the emitter drops a
>    repeat of the same pending decision.
>
> Also registered the checkpointer's msgpack allowlist. LangGraph was warning that custom
> types would be **blocked** in a future version, and the quiet failure mode is the nasty
> one: state loses a field on resume, and the field most likely to go is `error_code` —
> because a FAILED run is exactly the one being resumed. An untyped failure is the one thing
> this system is not allowed to produce.

## M6 — Calendar, hardening, benchmark

**Goal.** R9 with numbers attached.

### Tasks

- [x] **M6.1 — `CalendarWorker`.** Extract event details from a thread → build the event → **approval
      on invite dispatch**.
- [x] **M6.2 — Compaction.** Layer 0 (strip seen screenshots/observations), layer 1 (truncate old
      tool outputs), layer 2 (LLM-summarize the middle, keep first-2 + last-6 verbatim). Trigger at
      ~95% of the window on real API `input_tokens`.
- [x] **M6.3 — Prompt caching.** Stable cache-marked prefix; growing memory in a later message so
      appends do not bust the cache.
- [ ] **M6.4 — Batched classification.** One classifier call scores N subjects for triage.
- [x] **M6.5 — Benchmark harness.** Fixture mailbox + scripted tasks + expected outcomes; reports
      success rate, steps, tokens, and **% terminated with a typed code**.
- [x] **M6.6 — Adversarial fixtures.** Overlay-blocked compose; moved button; oscillation bait;
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

> **M6 note — the benchmark's first run corrected two things, one in each direction.**
>
> **A real bug in the `RulesWorker`.** Its stall guard trusted `ActionResult.success`, so a
> surface reporting success while changing nothing let it "archive" the same row two hundred
> times. It now measures the **page signature** — the same `NO_EFFECT` signal the feedback
> loop uses, and the same lesson twice: an action's own success flag cannot tell you whether
> anything happened.
>
> **A modelling error in the benchmark itself.** Every adversarial case scored as
> *untyped termination* because it ended `paused` — on an options card. But a run waiting
> for a human has not failed, it is the recovery layer working. The harness now drives
> interrupts to a real ending, with one absolute rule: **it never approves anything.** A
> benchmark that could approve a send would be a benchmark that can send email, and no
> reliability number is worth that.
>
> `expect_error` became a *set* for the same reason. Self-heal legitimately changes which
> code ends a run — a stuck agent offered a remedy, trying it, then exhausting its budget
> ends on `MAX_STEPS`. Pinning one code would measure the recovery path's incidental shape
> instead of the property that matters: that it ended typed at all.
>
> **First numbers:** typed termination **100%**, task success **100%** (4 scored,
> adversarial excluded), guardrail breaches **0**, and the rule-matched task at exactly
> **one** model call.

> **M6 refactor note — honouring two rules I had broken myself.**
>
> `ENGINEERING-SPEC` says prompts never live inline in Python, and I had put three multi-line
> system prompts straight into modules. They now live in `app/prompts/*.txt`, loaded by name.
> The spec said `jinja2`; the code uses plain `.txt`, because none of these prompts
> interpolate — state reaches the model as separate messages, which is exactly what keeps
> the system prefix byte-stable and prompt caching working. **The doc was corrected to match
> the code, not the other way round**, and the reason is written down.
>
> The same spec sets a ~300-line trip-wire. Four modules had drifted past it. Split where the
> seam was real:
> - `workers/rendering.py` — how the mailbox is described to the model: pure, no control flow
> - `workers/internal_verbs.py` — verbs the graph owns rather than the surface. The dividing
>   line: if a verb changes what is ON SCREEN the surface performs it; if it changes what the
>   RUN KNOWS, this does
> - `workers/approval_gate.py` — lifted out of `graph.py`, so a reader auditing "can anything
>   send without a human?" answers it from the edge list rather than scrolling past node bodies
>
> **Still over, and left that way on purpose:** `workers/loop.py` (411), `surface/playwright_surface.py`
> (397), `agent/graph.py` (336). Each passes the rule's real test — one nameable job — and
> splitting them further would scatter one coherent story across files to satisfy a number.
> Recording it as a known deviation rather than quietly relaxing the rule.

> **M6.1 scope — the calendar worker drafts; it does not book.**
>
> It reads a thread and emits a **proposal**: title, when, duration, attendees (as tokens),
> and the words it took them from. It creates no event and invites nobody, and the tool set
> contains no verb that could — `CALENDAR_TOOLS` is read-only.
>
> That is the documented v1 scope ([PRD Q3](PRD.md)), and it is where the value actually is.
> A mis-drafted proposal costs a glance; a mis-sent invite lands in other people's calendars
> and cannot be recalled. The `evidence` field is carried for the same reason the diagnosis
> carries its evidence — a proposal without the words it came from asks the user to take it
> on trust.
>
> **Still open:** actual invite dispatch, which would be a gated verb behind the approval
> gate with the executor resolving attendee tokens for the human, exactly as a draft email is
> resolved today.

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


### Tokenizing an address is not endorsing it as a recipient

The injection suite asserted that the attacker's address "has no token, so the instruction is
unrepresentable". Probing the real funnel against the hostile fixture disproved it: the vault held
`P1 -> attacker@evil.example`, minted from the message body, because redaction is unconditional and
the model must not read that address in the clear either.

So the address *was* nameable. Only the approval card stood between an injected instruction and a
send — which is the designed guarantee, but a thinner margin than the docs claimed.

The vault now records **provenance**. Every address is still tokenized; only addresses from a
structured position (sender/recipient/contact/chip) or from the user's own instruction are
*addressable*, and `ActionValidator` refuses the rest with `UNTRUSTED_RECIPIENT`. Provenance upgrades
but never downgrades, so quoting a colleague's address in a phishing body cannot make that colleague
unreachable.

The same change fixed a live bug from the other direction: `send an email to alice@x.com` was
unimplementable, because the address is not on the page and the dispatcher takes only tokens. Intake
now mints operator-supplied addresses as trusted. Same distinction, both directions: what matters is
not the address, it is who put it there.


### `--reload` on Windows gives you a loop that cannot start a browser

`uvicorn app.api.main:app --reload` failed with a bare `NotImplementedError` whose message
was the empty string, raised from `asyncio.create_subprocess_exec` inside Playwright's
transport. Nothing in the traceback mentions browsers.

The cause is in `uvicorn/loops/asyncio.py`:

    if sys.platform == "win32" and not use_subprocess:
        return asyncio.ProactorEventLoop
    return asyncio.SelectorEventLoop

`use_subprocess` is true when uvicorn manages processes itself, which `--reload` turns on. On
Windows only `ProactorEventLoop` can spawn a child process, and Playwright starts Chromium as
one — so the standard dev command is precisely the configuration that cannot drive a browser.

Three fixes, because the bug had three faces:

- **The cause** — `app/api/loop.py` supplies a loop factory, and `python -m app.api.dev`
  runs uvicorn with it. Reload still works.
- **The message** — `launch_surface` catches `NotImplementedError` and raises
  `SurfaceUnavailable` naming the fix. The empty string had been reaching the cockpit as
  "could not start the browser:" and then stopping.
- **The silence** — our loggers had no handler under uvicorn's logging config, so every
  `logger.warning` in the codebase went nowhere. `app/api/main.py` now configures the root
  logger if nothing else has, and logs at startup whether this process can drive a browser
  at all.

A detail worth recording: while debugging, three WebSocket probes reported the Selector-loop
failure *after* the fix was in place. The fix was fine; a stale `--reload` server from an
earlier session still held port 8000, so the probes were connecting to the old process. Two
of the three "failures" were measurements of the wrong server.


### Three bugs behind one screenshot

A single cockpit screenshot showed Gmail's "Couldn't sign you in" wall, a wall of model
reasoning, and the live view scrolled off the top. Three unrelated defects.

**1. Google rejects Chrome for Testing.** The browser in the shot was labelled "Chrome for
Testing" — Playwright's bundled build, which has no Google API keys, so Google refuses its
sign-in flow by design. No flag fixes it. `scripts/chrome.py` now launches the user's *real*
Chrome in two phases: `signin` (ordinary window, no debugging port, no automation flags) and
`serve` (same profile, port open). Nothing ever authenticates under automation. Attaching
also detects the wrong browser up front, by reading `navigator.userAgentData.brands` —
`Google Chrome` present means genuine Chrome, and its absence now produces a warning naming
the fix instead of a dead end at the login page. The check must run *after* navigation:
`userAgentData` is undefined on `about:blank`, and checking too early silently reports
nothing.

**2. `AskUser` was bound but not handled.** `handle_internal` had no branch for it, so it
fell through to `"AskUser is not handled"`. The model asked, was told the tool did not work,
reasoned at length about whether it had called it wrongly, tried again, and ran out of
steps — while a remediation strategy actively recommends that verb by name. It now raises a
real `interrupt()`, so the run pauses, the cockpit shows a question card, and the answer
comes back into the transcript. A structural test now asserts every verb in `CONTROL_TOOLS`
has a handler, because binding a tool the dispatcher ignores is a uniquely bad failure mode:
the model is told the tool exists, and then told it does not work.

**3. The page scrolled instead of the transcript.** `scrollIntoView` walks up and scrolls
*every* scrollable ancestor, so with a `min-h-full` body it scrolled the document and carried
the live browser view off the top on each new message. Fixed twice over: the transcript sets
`scrollTop` on its own container (which cannot touch ancestors) and holds position when the
user has scrolled up to read, and the cockpit is `fixed inset-0` so the document has nothing
to scroll regardless of the height chain above it. A height alone would only be as good as
that chain — one `min-h-full` anywhere and the bug returns.
