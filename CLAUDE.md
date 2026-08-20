# CLAUDE.md — Inbox Agent (browser-driven email agent, web)

> An agent that **operates the user's Gmail through a real browser** — reads the backlog, archives
> noise, drafts replies, extracts calendar events from threads, and surfaces the few things that
> need the human — while **never sending anything without approval**. It reasons in the cloud
> (FastAPI + LangGraph), drives Gmail's web UI through Playwright/CDP, talks to LLMs through a
> **free multi-provider gateway (Groq → OpenRouter → Gemini)**, and streams every step to a
> two-pane cockpit over WebSocket.
>
> Two layers. The **worker layer** is the observation **funnel**, **Set-of-Marks** indexing,
> **trusted input**, and the **typed-failure** engine — a surface-agnostic loop pointed at mail.
> Above it sits the **manager/supervisor graph**: intake, 100%-context gating, linear-vs-decision
> routing, worker dispatch, approval gates, and self-healing recovery.
>
> This file is the contract for how to build and work in this repo. Read it before writing code.
> Build in the order in §18. Do not deviate from the guardrails in §17.

---

## 0. Fill-in slots (the only per-surface part)

| Slot | This project |
| --- | --- |
| `<TARGET_SYSTEM>` | Gmail's web UI, driven in a real browser |
| `<RAW_STATE>` | Gmail's DOM (100k+ tokens) — **never sent to the LLM** |
| `<OBSERVATION>` | a compact, **PII-tokenized**, SoM-indexed element list (~1–3k tokens) |
| `<ACTION vocabulary>` | click / type / scroll / navigate / read + email verbs (compose, archive, label, snooze, send*, extract-event*) — `*` = gated |
| `<CONTROL_API>` | Playwright/CDP `Input.*` (server-side) or `chrome.debugger` (user's browser), behind the `EmailSurface` port |

**The one rule that generates everything:** the LLM never sees `<RAW_STATE>` or raw PII. It sees the
tokenized `Observation` and references elements **by index**; it refers to people/addresses **by
token**. The `index → geometry` and `token → real value` maps stay in the executor, next to the
browser.

---

## 1. The core idea

Gmail's DOM is enormous; you never send it to the model. The system is a **funnel** that throws away
~99% of the page and hands the model a short numbered list of only what it can act on or read — with
every email address, phone, and (best-effort) personal name replaced by a stable token first.

The model picks a **number** and a **token**; it never sees a coordinate or a real address. The
executor resolves both and performs a **trusted** input event. The genius is the funnel + the
failure/recovery layer around it — that is what you build.

---

## 2. Architecture — a manager graph over a browser-driving worker

The observe→reason→act loop is **one worker**. Everything new is a supervisor **above** it.

```
┌──────────────── COCKPIT (Next.js App Router, Vercel) ────────┐
│ LHS: chat · run history · questions · ranked options          │
│ RHS: live browser screen (screenshots) · current action       │
└───────────────────────── WebSocket ⇅ ────────────────────────┘
                              │ Observation / ActionCall / Event  (never raw DOM or raw PII)
┌──────────────────── BACKEND — THE BRAIN (FastAPI + LangGraph) ─┐
│ MANAGER / SUPERVISOR GRAPH                                      │
│  [PRE]  intake → context_gate ⟲(AskUser) → router → planner    │
│  [IN]   dispatch → workers{ Triage · Compose · Calendar · Rules}│
│                    each = observe→reason→act → approval_gate    │
│  [POST] verify → ⟨ok⟩ finalize | ⟨fail⟩ diagnose → options ⟲(HITL)│
│  gateway: LLMClient(Groq→OpenRouter→Gemini)  ·  checkpointer     │
│  ports: EmailSurface · PiiVault · RulesStore · SkillRegistry ·   │
│         Approver · TrajectoryStore · EventSink                   │
└─────────────────── authenticated relay ⇅ ─────────────────────┘
┌──────────── EXECUTOR — Playwright, next to Gmail ─────────────┐
│ funnel: extract → visibility → occlusion → collapse →          │
│         PII-tokenize → SoM-index → format                      │
│ trusted dispatch via Playwright/CDP Input.*                    │
└────────────────────────────────────────────────────────────────┘
```

### The load-bearing split (do not violate)
- **The loop + all reasoning + all keys run in the backend.** Latency is dominated by the LLM call,
  not the relay. Keeps keys/reasoning/trajectories off the user's machine.
- **The funnel (including PII tokenization) runs in the executor**, next to the DOM. **Raw DOM and
  raw PII never cross the wire** — only tokenized `Observation`s do.
- **The two sides share only two contracts:** `Observation` and `ActionCall` (§12).

---

## 3. Pre / In / Post — the phase model the graph implements

- **PRE** — earn 100% context, classify, plan, arm security. *No mailbox mutation.* (§6–§8)
- **IN** — manager dispatches workers; the ReAct loop operates Gmail live; irreversible actions gate.
  (§4–§5, §9)
- **POST** — verify the outcome; on failure, diagnose + offer ranked HITL options; record; scale.
  (§10–§11, §14)

---

## 4. The agent loop — LangGraph state machine

Orchestrate as an explicit `StateGraph`, not a `while` loop. **Nodes are thin** — each is a closure
over injected ports, returns a state delta, and contains **no** LLM/CDP/DB code inline.

```
START → intake → context_gate ⟲ → router → planner
      → dispatch → (worker loop: observe → reason → act → observe …) → approval_gate ⟲
      → verify → ⟨ok⟩ finalize → END
                 ⟨fail⟩ diagnose → options ⟲ → (retry | finalize)
```

Worker loop nodes:
- **observe** — `EmailSurface.observe()` → fresh tokenized `Observation`; diff vs last turn; page
  signature; append to history. **Renumber every turn.**
- **reason** — ReAct: history + bound tool schema → LLM → reasoning text + a structured tool call by
  **index/token** (native tool-calling, never free-text parsing). Rules soft-guidance injected here.
- **act** — dispatcher → `EmailSurface.act()` or an internal state update; record a `StepRecord`;
  settle; re-observe.

**Routing = pure, unit-testable functions.** `route_after_reason`, `route_after_act`,
`route_after_verify` read `status`/`error_code`/tool-call presence. Nudge once on a missing
tool-call or missing reasoning; then finalize.

**Checkpointer = persistence + pause/resume + all three HITL interrupts.** Compile with a
checkpointer (`MemorySaver` dev, SQLite/Postgres prod); `thread_id == one session`.

**Streaming:** drive with `graph.astream(stream_mode="updates")`; forward every node update through
`EventSink` → WS hub.

### Do NOT
- ❌ Call the LLM/CDP directly inside a node — go through ports.
- ❌ Keep mutable agent state outside `AgentState`.
- ❌ Reuse previous-turn indices — re-observe and rebuild every turn.

---

## 5. State — single source of truth

```python
class AgentState(TypedDict):
    task: str
    thread_id: str                       # == session; persistence key
    intent: TaskIntent | None            # parsed action + slots
    missing_slots: list[str]             # drives context_gate
    route: Literal["linear","decision"] | None
    plan: Plan | None
    active_worker: str | None
    messages: Annotated[list[Message], add]
    observation: Observation | None      # tokenized, indexed — from the funnel
    agent_memory: dict[str, str]         # Remember/Recall scratchpad
    history: Annotated[list[StepRecord], add]
    last_action: ActionCall | None
    last_result: ActionResult | None
    pending_approval: ApprovalRequest | None
    diagnosis: Diagnosis | None          # root cause + ranked options
    status: Literal["gathering","running","awaiting_human","done","failed"]
    error_code: ErrorCode | None
    step: int
    finished: bool
    success: bool | None
    reason: str
    stuck_count: int
    nudge_count: int
    recent_actions: list[str]            # rolling window for repetition guard
```

Use append reducers for `messages` and `history`.

---

## 6. PRE — intake, context-gate (100% context), router, planner

### 6.1 intake
Parse the NL task → `TaskIntent { action, targets, topic?, thread_ref?, tone?, constraints[] }` via
the executor model. No side effects.

### 6.2 context_gate — "won't start until 100% context"
Each `action` declares a **required-slots schema** (e.g. `send_email` needs
`{recipient_identity, topic|body_intent}`). Compute `missing_slots` + a confidence score. If any
required slot is missing/ambiguous → emit an `AskUser` **interrupt** (LHS asks; RHS may highlight
candidates); resume with `Command(resume=…)`. Loop until confidence ≥ threshold. Read-only
observation is allowed here to resolve references ("which contact named X?"). Only then proceed.

### 6.3 router — linear vs decision
Classify execution topology → typed `Route`:
- **linear** — deterministic, single-shot ("archive all newsletters", "mark all read from X"):
  straight-line worker, **skip the reasoning loop**.
- **decision** — needs perception + judgment per step ("reply to the ones that need me"): full
  observe→reason→act worker.

### 6.4 planner (decision only)
Post a lightweight `Plan(steps)` to the cockpit; the loop may revise it.

### 6.5 security init
Open a per-session `PiiVault` (§13). Every observation is tokenized before it reaches the brain.

---

## 7. The observation funnel (in the executor)

Pipeline of single-responsibility stages (add capability = add a stage class):

1. **Extract** — DOM + AX roles/names + geometry + screenshot.
2. **VisibilityFilter** — drop hidden/zero-size/off-screen.
3. **OcclusionCuller** — drop elements physically covered (content behind an open compose/modal).
4. **WrapperCollapser** — collapse layout wrappers to the meaningful leaf.
5. **PiiTokenizer** — replace email addresses, phones, and best-effort personal names with stable
   per-session tokens; record `token → real` in the `PiiVault` (executor-side). **Runs before
   indexing so nothing downstream ever holds raw PII.**
6. **SoMIndexer** — assign each interactable `[N]`; build the hidden `index → {backendNodeId, x, y}`
   map (executor-side).
7. **ReadingOrderFormatter** — serialize survivors in reading order; `## Current Focus`; `changed`
   summary; `droppedCount`. Enforce hard token budget; drop lowest-priority first and **log what was
   dropped** ("18 off-screen items hidden"). Never truncate silently.

**Output = `Observation` (§12):** numbered tokenized list + screenshotRef + scroll hints + `changed`
+ `droppedCount`. **No raw DOM, no coordinates, no raw PII cross the wire.**

---

## 8. IN — manager/supervisor + workers

### 8.1 Supervisor
Owns the run: dispatches to worker subgraphs, aggregates, routes approvals, streams. Workers
implement a common `Worker` interface (add a worker = add a class + register).

| Worker | Topology | Job |
| --- | --- | --- |
| `TriageWorker` | decision | funnel inbox → archive/label/snooze/read backlog |
| `ComposeWorker` | decision (ReAct) | compose → fill fields → **approval** → send |
| `CalendarWorker` | decision | extract event from thread → create → **approval** on invite |
| `RulesWorker` | linear | apply deterministic user rules, short-circuiting the LLM |

### 8.2 Compose ReAct flow ("send email on <topic> to <recipient>")
After `context_gate` fills slots: `click Compose[12]` → `type recipient-token` (executor resolves
token→real address at dispatch) → `type subject` → `type body` → `Send[27]` → **approval interrupt**
→ cockpit shows the rendered draft → human Approve / Edit(take-over) / Reject → resume. The user
watches each field fill live; nothing sends without approval.

---

## 9. Actions — trusted, reversible, typed, gated

Targets by **index/token**, resolved executor-side, performed as **trusted** input (Playwright/CDP
`Input.*`, `isTrusted:true`) — never JS `.click()` by default. Each action = one dispatcher handler
implementing a common `Action` interface; per-action timeout wall → `ACTION_TIMEOUT`.

| Verb | Reversible | Gated |
| --- | --- | --- |
| navigate / scroll / read / wait_for | n/a | no |
| click / type / clear / select | (context) | no |
| Archive / Label / Snooze | yes | no |
| DraftReply / Compose | yes (draft) | no |
| **Send** | no | **approval interrupt** |
| **ExtractToCalendar → invite-send** | event yes / invite no | **approval on invite** |
| Delete-forever | no | **approval interrupt** |

Meta verbs: `Complete(success, reason)`, `Remember`/`Recall`, `SetPlan`, `AskUser`. Every mutating
verb logs enough to **undo**. **After every action: settle (adaptive `mean+2σ` per host), then
re-observe from scratch.**

---

## 10. POST — verify + self-healing recovery (ranked HITL options)

### 10.1 verify
Did the action achieve the goal? Contract check (mail in **Sent**? draft persisted?) + optional
visual/rubric check on the screenshot → `ok` or a typed failure.

### 10.2 diagnose → options
On any typed failure or `verify` fail:
1. **Root-cause classification** — map `(error_code, context, last_action, observation-diff)` → a
   `Cause`.
2. **Consult `SkillRegistry`** — a curated, versioned set of `RemediationStrategy` objects
   (scroll-and-retry, dismiss-overlay, switch-provider, widen-observation, ask-user). This is the
   safe reading of "load skills."
3. **Emit 4 ranked options** via an `options` event + `Approver` **interrupt**:
   `[1] Recommended`, `[2]`, `[3]`, `[4] Other (free-form — user types what to do)`.
4. Resume with the chosen remedy → re-enter the loop or finalize.

**Scope guardrail:** v1 self-healing recovers *tasks* and reads a *curated* `SkillRegistry`. It does
**NOT** edit its own running source — that is out of v1 by design (unbounded blast radius). A
sandboxed dev-assist mode may propose source patches for human review later; keep it out of the live
loop.

---

## 11. Failure & loop engineering (all required, typed)

- **Action-repetition guard** (dominant on this surface): signature `(action, target-token,
  args-hash)` over a rolling window; nudge at 3, kill at 5 → `STUCK`. Exclude distinct-target
  Archive/ReadFull/Snooze and scroll/wait/read.
- **Stuck-signature detection**: observation unchanged N turns → `STUCK`; soft-nudge at 2.
- **Runaway-output clip**: clip reasoning past ~3k chars before it enters history.
- **Think-before-act**: tool call with no reasoning → retry once → `REASONING_MISSING`.
- **No-tool-call nudge**: nudge once → finalize (`NO_ACTION`).
- **Budget awareness**: ≤5 steps left → inject "call `Complete()` now with findings".
- **Approval edge cases**: never arrives → `APPROVAL_TIMEOUT`; rejected + no alternative →
  `APPROVAL_REJECTED_NO_ALT`.
- **Typed codes only.** `ErrorCode ∈ {STUCK, ACTION_TIMEOUT, REASONING_MISSING, MAX_STEPS,
  NO_ACTION, APPROVAL_TIMEOUT, APPROVAL_REJECTED_NO_ALT}`. No HITL-as-failure-fallback.

---

## 12. Boundary contracts (single source of truth)

Author once (Pydantic) → generate Zod/TS for the extension/frontend; CI fails on drift; every
message carries `protocolVersion`.

```
Observation = { protocolVersion, contextId, title, viewport,
                elements:[{ index, role, name, value?, isNew? }],   # names/values already tokenized
                screenshotRef?, changed?, droppedCount? }           # NO coordinates, NO raw DOM/PII
ActionCall  = { name, args }                                        # targets by index/token only
ActionResult = { success, reason, error_code? }
```

---

## 13. Security — PII vault (no raw data to the AI)

`PiiTokenizer` (funnel stage §7.5) + `PiiVault` (executor) replace addresses/phones/(best-effort)
names with stable per-session tokens (`alice@x.com → P17`). The LLM reasons over tokens; actions
reference tokens; the executor resolves `token → real` **only at dispatch**. Tokens are sanitized
out of logs, trajectories, and error reasons. Keys never leave the backend.

---

## 14. LLM gateway — free, multi-provider fallback

One gateway; the graph/services see only the `LLMClient` port. Impl = `FallbackLLMClient` over an
ordered chain, all OpenAI-compatible via LangChain:

```
FallbackLLMClient([
    GroqClient(...),        # primary — best free-tier throughput/min
    OpenRouterClient(...),  # fallback — OpenAI-compatible base_url swap; :free models, no card
    GeminiClient(...),      # fallback — free tier
])   # on 429/quota/5xx → next provider. Same model within a single retry (no mid-attempt reroute).
```

- **Keys server-side only.** Model per role (`classifier` small, `executor` large, `validator`
  small) from **config**, never hardcoded — and never hardcode a `:free` model ID (the roster
  rotates).
- **The real free-tier constraint is rate/quota, not dollars** (OpenRouter free = ~20 req/min,
  50/day at $0). Mitigate with: the fallback chain, a cheap classifier for triage, prompt caching
  (stable prefix cache-marked; growing memory in a later message), a lean loop, batched
  classification.
- **Meter every call** (tokens by role+provider, latency) → `StepRecord`. Retry transient errors
  with backoff.

---

## 15. Persistence · streaming · telemetry

- **Checkpointer** keyed by `thread_id` (SQLite dev / Postgres prod) → trajectory persistence +
  durable pause/resume + all three HITL interrupts for free.
- **Streaming** via `EventSink` port (buffer in tests, WS in prod). Event types: `reasoning`,
  `action`, `screenshot`, `question`, `options`, `approval_request`, `status`.
- **Trajectory store** — every step a `StepRecord` (step, node, worker, action, result, error_code,
  tokens, latency). The ordered records **are** the trajectory: replayable, auditable, eval fodder.

---

## 16. SOLID / ports & adapters

Nodes and the composition root are the only places that know concretes.

```python
class LLMClient(Protocol):      async def complete(self, *, role, messages, tools) -> LLMResult: ...
class EmailSurface(Protocol):   async def observe(self) -> Observation: ...
                                async def act(self, call: ActionCall) -> ActionResult: ...
class Worker(Protocol):         name: str; async def run(self, state) -> WorkerResult: ...
class RemediationStrategy(Protocol): def applies_to(self, cause)->float: ...; def to_option(self)->Option: ...
class PiiVault(Protocol):       def tokenize(self, text)->str: ...; def resolve(self, token)->str: ...
class RulesStore(Protocol):     def active(self) -> list[Rule]: ...
class SkillRegistry(Protocol):  def strategies_for(self, cause) -> list[RemediationStrategy]: ...
class Approver(Protocol):       async def request(self, req) -> Decision: ...
class TrajectoryStore(Protocol):async def save(self, thread_id, rec) -> None: ...
class EventSink(Protocol):      async def emit(self, event) -> None: ...
```

- **S** one job per class (each funnel stage, action handler, worker, strategy, provider adapter).
- **O** extend by adding a class; supervisor/pipeline/dispatcher/composer don't change.
- **L** every `EmailSurface`/`Worker`/`LLMClient` impl is drop-in; tests use fakes.
- **I** narrow ports; no consumer depends on a method it doesn't call.
- **D** the graph depends on abstractions; concretes built in **one composition root** and injected.

**Two `EmailSurface` impls** behind the port: `PlaywrightEmailSurface` (server-side headless Chromium
driving Gmail — dev/CI/demo, default) and `ExtensionEmailSurface` (user's own Chrome via
`chrome.debugger` bridge — real-user product). Swappable with zero graph changes.

---

## 17. Guardrails — do NOT

- ❌ No Electron/desktop deps — this is a web app.
- ❌ No raw DOM or raw PII over the wire — only tokenized funnel output.
- ❌ No LLM/CDP/DB calls inside graph nodes — go through ports.
- ❌ No business/agent logic in `api/` — it only adapts HTTP/WS to services.
- ❌ No provider key outside the backend.
- ❌ No `Send` / delete / bulk-irreversible action without an approval interrupt.
- ❌ No auto-send from rules unless a rule is explicitly whitelisted (off by default).
- ❌ No model re-routing *inside* a retry; fallback is between attempts. No hardcoded `:free` IDs.
- ❌ No reuse of previous-turn indices — re-observe and renumber every turn.
- ❌ No silent truncation of the observation — always log dropped/hidden counts.
- ❌ No mutable agent state outside `AgentState`.
- ❌ No self-source editing in the live loop (self-heal reads a curated `SkillRegistry` only).
- ❌ No task start before `context_gate` clears (100%-context rule).
- ✅ Every failure ends in a typed `ErrorCode`; per-action timeout wall; stuck + repetition guards.

---

## 18. Repo layout & build order

```
inbox_autopilot/                   # monorepo
├─ backend/
│  ├─ app/
│  │  ├─ agent/        # graph build, nodes, routing, state, prompts, compaction
│  │  ├─ manager/      # supervisor, intake, context_gate, router, planner
│  │  ├─ workers/      # Triage, Compose, Calendar, Rules (each a subgraph)
│  │  ├─ observation/  # Observation model + server-side funnel for Playwright surface
│  │  ├─ actions/      # ActionCall types + dispatcher + handlers
│  │  ├─ recovery/     # diagnose, RemediationStrategy impls, SkillRegistry
│  │  ├─ rules/        # RulesStore + deterministic matcher + soft-guidance renderer
│  │  ├─ surface/      # EmailSurface port; PlaywrightEmailSurface, ExtensionEmailSurface
│  │  ├─ security/     # PiiVault + PiiTokenizer
│  │  ├─ llm/          # LLMClient port + FallbackLLMClient + Groq/OpenRouter/Gemini adapters + metering
│  │  ├─ telemetry/    # StepRecord, ErrorCode, TrajectoryStore
│  │  ├─ events/       # EventSink
│  │  ├─ api/          # FastAPI routes + WebSocket hub (transport→services ONLY)
│  │  └─ config/       # settings + composition root (ONLY place wiring concretes)
│  └─ tests/           # fakes for EmailSurface + LLMClient; graph-path tests; integration; benchmark
├─ bridge-extension/   # TS Chrome extension (ExtensionEmailSurface): funnel + chrome.debugger + WS
├─ frontend/           # Next.js cockpit: LHS chat/history/questions/options, RHS live screen
│                      #   ONE "use client" root (CockpitClient); socket connects DIRECT to backend
├─ packages/contracts/ # Pydantic → generated Zod/TS
├─ docs/               # README (index) · PRD · SYSTEM-DESIGN · TECH-STACK · ENGINEERING-SPEC
│                      #   SECURITY-MODEL · WS-PROTOCOL · IMPLEMENTATION-PLAN · TESTING-AND-EVAL · ADR
└─ CLAUDE.md           # this file
```

**Where the detail lives.** This file is the contract; `docs/` is the reasoning. Start at
[`docs/README.md`](docs/README.md). `docs/ADR.md` records every decision and what it superseded —
including the Next.js cockpit (ADR-011) and the three-provider chain (ADR-008), both of which
supersede earlier text in `docs/SOLUTION-PRE-IN-POST.md`.

**Build order (one week):**
1. Contracts (§12) + `FallbackLLMClient` (Groq→OpenRouter→Gemini) + `PiiVault`/tokenizer; point the
   funnel/`PlaywrightEmailSurface` at Gmail.
2. Manager graph skeleton + `intake` + `context_gate`(AskUser) + `router`.
3. `TriageWorker` observe→reason→act + WS streaming to the two-pane cockpit.
4. `ComposeWorker` ReAct + `approval_gate` interrupt + take-over.
5. `diagnose`→`options`(1..4) + `SkillRegistry` + `Approver`; `RulesWorker`.
6. `CalendarWorker` + failure-layer hardening + benchmark harness.
7. Cockpit polish, guardrail audit, deploy (HF Space / Render + Vercel).

---

## 19. Tech stack

Python 3.12+ · FastAPI · **LangGraph** (orchestration) + LangChain core (LLM plumbing behind the
`LLMClient` port) · Pydantic v2 · `httpx` · `websockets` · Playwright (Chromium) · TypeScript +
`chrome.debugger` (extension) · **Next.js (App Router)** + React 18 + TanStack + Tailwind + Zod
(cockpit) · LangGraph
checkpointer (SQLite dev / Postgres prod). **LLM:** Groq → OpenRouter → Gemini, free tiers, via
OpenAI-compatible clients. **Real-time:** WebSocket. **Cost target:** $0.

---

## 20. Dev workflow — TDD

Red → green → refactor. Unit-test each funnel stage + each action handler + each remediation strategy
in isolation. Test the graph with **fakes** for `EmailSurface` and `LLMClient` (no real
browser/LLM): scripted observation + canned response → assert routed path + emitted `StepRecord`s.
Integration-test the loop against `PlaywrightEmailSurface` on a fixture Gmail account. Regenerate
contracts after any schema change; CI fails on Pydantic↔Zod drift. A benchmark harness over the
fixture reports success-rate / steps / tokens / % terminated-with-typed-code (target 100%).

---

## 21. Glossary

- **Funnel** — staged pipeline pruning the DOM to a compact tokenized numbered list.
- **SoM (Set-of-Marks)** — integer index per interactable so the LLM references elements by number.
- **PII vault / tokenizer** — replaces real addresses/names with stable tokens before the wire.
- **Manager / supervisor** — the LangGraph layer above the worker loop (intake, gate, route,
  dispatch, verify, recover).
- **Worker** — a subgraph that runs the observe→reason→act loop for one job.
- **Trusted input** — real synthetic events (`isTrusted:true`) via CDP; the default over JS clicks.
- **Interrupt (HITL)** — durable pause→ask→resume; used three ways: AskUser (context), approval
  (send), options (self-heal).
- **Checkpointer** — per-`thread_id` state persistence giving resume + all interrupts for free.
- **Trajectory** — ordered `StepRecord`s of a run; persisted, replayable, the eval substrate.
- **Composition root** — the one place concretes are built and injected.
