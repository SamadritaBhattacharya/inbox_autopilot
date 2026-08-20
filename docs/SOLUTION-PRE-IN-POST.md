# Email Agent — Pre / In / Post Solution (principal-level system design)

> **Status: seed document.** This is the original solution narrative that the current design grew
> from. It remains accurate on Pre/In/Post structure, SOLID shape, and guardrails. Two parts are
> **superseded** — see [`ADR.md`](ADR.md):
> - §7 provider chain (Groq → Gemini) → now **Groq → OpenRouter → Gemini** ([ADR-008](ADR.md#adr-008))
> - cockpit framework (React) → now **Next.js App Router** ([ADR-011](ADR.md#adr-011))
>
> For the current design, read [`SYSTEM-DESIGN.md`](SYSTEM-DESIGN.md).


**Author stance:** principal/staff AI engineer · **Build window:** 1 week · **Stack:** FastAPI +
LangGraph + LangChain · **LLM:** Groq (primary) → Gemini (fallback), free tiers · **Real-time:**
WebSocket · **Cost target:** $0.
**Companions:** `CLAUDE.md` (the build contract), `PRD.md`, `SYSTEM-DESIGN.md`, `ENGINEERING-SPEC.md`.

---

## 0. The one decision (settle before M0)

**v1 is browser-driven: the agent operates Gmail's web UI through a real Playwright/CDP browser.**
The `<TARGET_SYSTEM>` is Gmail's web app; `<RAW_STATE>` is its DOM. The funnel, Set-of-Marks
indexing, trusted-input dispatch, and typed-failure layer are surface-agnostic — "click compose,
type subject, type body, Send" is the same action loop, pointed at mail. The RHS live screen is real
browser frames streamed over `/ws/run`.

An `EmailSurface` port keeps an API-driven adapter possible later; it is **not** built in v1 because
the Gmail API is narrower than the UI and would discard the expensive, reusable core.

---

## 1. Architecture — a manager graph over a browser-driving worker

The load-bearing idea: the observe→reason→act loop becomes **one worker**. Everything new
(context-gating, routing, approval, self-heal) is a **manager/supervisor LangGraph** *above* it.

```
┌──────────────────────────── COCKPIT (React, Vercel) ────────────────────────────┐
│ LHS: chat + run history + questions + ranked options   RHS: live browser screen  │
└───────────────────────────────── WebSocket ⇅ ───────────────────────────────────┘
                                       │  (Observation / ActionCall / Event — never raw DOM/PII)
┌──────────────────────────────── BACKEND — THE BRAIN ─────────────────────────────┐
│ MANAGER GRAPH (LangGraph supervisor)                                              │
│                                                                                    │
│  [PRE]  intake → context_gate ⟲(AskUser) → router(linear|decision) → planner       │
│  [IN]   dispatch → { TriageWorker · ComposeWorker · CalendarWorker · RulesWorker } │
│                       each = observe→reason→act loop → approval_gate(interrupt)     │
│  [POST] verify → ⟨ok⟩ finalize   |   ⟨fail⟩ diagnose → options(1..4) ⟲(HITL) → retry │
│                                                                                    │
│  ports: LLMClient(Groq→Gemini) · EmailSurface · PiiVault · RulesStore ·            │
│         SkillRegistry · Approver · TrajectoryStore · EventSink                     │
│  checkpointer (SQLite dev / Postgres prod), keyed by thread_id                     │
└───────────────────────────── authenticated relay ⇅ ──────────────────────────────┘
┌──────────────────────── EXECUTOR — Playwright, next to Gmail ────────────────────┐
│ funnel (DOM → visibility → occlusion → collapse → PII-tokenize → SoM → format)     │
│ trusted action dispatch (Playwright/CDP Input.*)                                   │
└───────────────────────────────────────────────────────────────────────────────────┘
```

**Split that must not be violated:** the loop + all reasoning + keys stay in the backend; the funnel
(incl. PII tokenization) runs in the executor next to the DOM; **raw DOM and raw PII never cross the
wire** — only tokenized `Observation`s do.

---

## 2. Requirement → architecture map

| # | Requirement | Where it lives |
| --- | --- | --- |
| 1 | Two-pane UI (LHS chat/history, RHS live email screen) | Cockpit + `EventSink`→WS; RHS renders streamed screenshots + current action |
| 2 | "send email on X to Y" → compose→confirm→act (ReAct) | `ComposeWorker` ReAct loop + `approval_gate` interrupt before Send |
| 3 | Won't start until 100% context, else asks | `context_gate` node — slot-completeness check + `AskUser` interrupt loop |
| 4 | Self-heal: root-cause + 4 ranked options (1 recommended, 4th free-form) + HITL | `diagnose` → `options` subgraph + `SkillRegistry` + `Approver` interrupt |
| 5 | Manager workflow / proper AI workflow | LangGraph **supervisor** dispatching worker subgraphs |
| 6 | Choose linear vs decision agents, route accordingly | `router` node — task-topology classifier |
| 7 | No raw data (email ids etc.) to the AI | `PiiVault` tokenizer stage in the funnel; executor holds reverse map |
| 8 | Real-time, user sees what's happening | `graph.astream(stream_mode="updates")` → `EventSink` → WS, every node |
| 9 | Guardrails | approval gates + typed failures + no-auto-send + PII redaction + timeouts (§8) |

---

## 3. PRE — earn 100% context, classify, plan, secure

*Nothing mutates the mailbox in this phase. Read-only observation is allowed to resolve references.*

### 3.1 `intake`
Parse the natural-language task into a typed `TaskIntent { action, targets, topic?, thread_ref?,
tone?, constraints[] }` using the executor LLM. No side effects.

### 3.2 `context_gate` (requirement 3 — "100% context")
Each `action` declares a **required-slots schema** (e.g. `send_email` needs
`{recipient_identity, topic|body_intent}`). The node computes missing/ambiguous slots and a
confidence score:
- If any required slot is missing or ambiguous ("which *Priya*? two contacts match") → emit an
  `AskUser` **interrupt**; the LHS chat asks, the RHS may highlight candidates; resume with
  `Command(resume=…)`.
- Loop until confidence ≥ threshold. Only then proceed. This is the "won't start until 100%
  context" guarantee, implemented as a durable interrupt loop — not a blocking prompt.

### 3.3 `router` (requirement 6 — linear vs decision)
Classify execution topology:
- **Linear** — deterministic, single-shot, low-ambiguity ("archive all newsletters", "mark all read
  from X"). Route to a straight-line worker; **skip the reasoning loop** (cheaper, faster, no LLM
  per step).
- **Decision** — needs perception + judgment per step ("reply to the ones that need me", "book the
  meeting from this thread"). Route to a full observe→reason→act worker.
The router is a small classifier call whose output is a typed `Route`; edges are pure functions.

### 3.4 `planner` (decision tasks only)
Post a lightweight `Plan(steps)` to the cockpit so the human sees intent before action. Not a rigid
script — the loop can revise.

### 3.5 Security init (requirement 7)
Open a per-session `PiiVault`. From here on, every observation is tokenized before it reaches the
brain/LLM (§5.3).

---

## 4. IN — manager dispatches workers; ReAct loop; live; gated

### 4.1 Manager / supervisor (requirement 5)
The supervisor owns the run: it dispatches to one or more **worker subgraphs**, aggregates results,
routes approvals, and streams everything. Workers implement a common interface so adding one is
adding a class (Open/Closed).

| Worker | Topology | Job |
| --- | --- | --- |
| `TriageWorker` | decision | funnel the inbox → archive/label/snooze/read the backlog |
| `ComposeWorker` | decision (ReAct) | compose → fill fields → **approval** → send |
| `CalendarWorker` | decision | extract event from thread → create → **approval** on invite |
| `RulesWorker` | linear | apply deterministic user rules, short-circuiting the LLM |

### 4.2 The worker loop
`observe → reason → act → observe …`:
- **observe** — `EmailSurface.observe()` runs the funnel over Gmail's DOM → a tokenized, SoM-indexed
  `Observation` (~1–3k tokens). Renumber every turn.
- **reason** — Groq (fallback Gemini) gets history + bound tool schema → reasoning + a structured
  tool call referencing elements **by index**. Native tool-calling, not free-text parsing.
- **act** — dispatcher resolves index→coordinate (executor-side) → trusted Playwright input. Settle,
  then re-observe from scratch.

### 4.3 The compose ReAct flow (requirement 2)
"send email on \<topic\> to \<recipient\>" (after `context_gate` filled the slots):
```
reason: "click Compose [12]"     → act → observe (compose panel open, live on RHS)
reason: "type recipient token"   → act (executor resolves token→real address) → observe
reason: "type subject"           → act → observe
reason: "type body"              → act → observe
reason: "Send [27]"              → APPROVAL INTERRUPT
        → cockpit shows the fully-rendered draft
        → human Approve / Edit(take-over) / Reject
        → resume: on approve, click Send; on edit, revise; on reject, alternative or Complete(false)
```
The user watches each field fill in real time; nothing sends without an explicit approval.

### 4.4 Real-time streaming (requirement 8)
Drive with `graph.astream(stream_mode="updates")`; forward each node update through the `EventSink`
port to the WS hub. Event types: `reasoning`, `action`, `screenshot`, `question`, `options`,
`approval_request`, `status`. LHS renders reasoning/history/questions; RHS renders the browser frame
+ the action being performed.

---

## 5. POST — verify, self-heal, learn, scale

### 5.1 `verify`
Did the action achieve the goal? Contract check (is the mail in **Sent**? did the draft persist?) +
optional visual/rubric check on the screenshot. Produces `ok` or a typed failure.

### 5.2 `diagnose` → `options` — self-healing with HITL (requirement 4)
On any typed failure or `verify` fail:
1. **Root-cause classification** — map `(error_code, context, last_action, observation-diff)` to a
   likely cause (e.g. `STUCK`+unchanged page → "Compose button moved / overlay blocking";
   `ACTION_TIMEOUT` → "slow render / network"; `REASONING_MISSING` → "prompt/model issue").
2. **Consult the `SkillRegistry`** — a curated, versioned set of remediation *skills/playbooks*
   (scroll-and-retry, dismiss-overlay, switch-model, widen-observation, ask-user). This is the
   safe reading of "load skills."
3. **Emit 4 ranked options** via an `options` event + `Approver` **interrupt**:
   - **[1] Recommended** — the highest-confidence remedy.
   - **[2], [3]** — plausible alternatives.
   - **[4] Other** — free-form: the user types what to do; the manager folds it back into the loop.
4. Resume with the chosen remedy; re-enter the loop or finalize.

> **Scope guardrail on "see codebase and fix":** v1 self-healing operates at the *task* level
> (recover a stuck run) and reads a *curated* `SkillRegistry`. It does **not** edit its own running
> source — an agent that rewrites its own code is an unbounded-blast-radius capability and is out of
> v1 by design. A separate, sandboxed "dev-assist" mode can propose source patches for human review
> later; keep it out of the live email loop.

### 5.3 PII vault — never give raw data to the AI (requirement 7)
A `PiiTokenizer` funnel stage (runs in the executor, before the wire) replaces every email address,
phone, and where possible personal name with a **stable per-session token** (`alice@x.com` → `P17`,
"Aritra Sen" → `C4`). The `PiiVault` holds the reverse map executor-side. The LLM reasons over
tokens; actions reference tokens; the executor resolves token→real value only at dispatch. Tokens
are sanitized out of logs, trajectories, and error reasons too. Result: a demonstrable "the model
never saw a real address" security story.

### 5.4 Telemetry & trajectory
Every step writes a `StepRecord` (node, action, result, error_code, tokens by role+provider,
latency, cost=$0-but-tracked-as-tokens). The ordered records **are** the trajectory — replayable,
auditable, the eval substrate.

### 5.5 Eval / benchmark
A harness over a **fixture Gmail account** (or recorded-DOM fixtures) with scripted tasks + expected
outcomes reports success-rate / steps / tokens / % terminated-with-typed-code (target 100%).
Include adversarial cases that force the failure layer: overlay-blocked compose, moved button,
oscillation bait.

### 5.6 Scaling
- **Stateless brain replicas** behind the WS hub; all run state in the checkpointer (Postgres, keyed
  by `thread_id`) → horizontal scale + resume across restarts.
- **One Playwright context per session**; pool/limit concurrent browsers; a session queue.
- **Provider fallback** absorbs free-tier rate limits (§7); cheap classifier model for triage,
  bigger model only for reasoning.
- **Prompt caching**: stable prefix (instructions + tool schema + task) cache-marked; growing memory
  in a later message so appends don't bust the cache.

---

## 6. SOLID / OOP — the concrete shape

Nodes and the composition root are the only places that know concretes; everything else depends on
narrow ports.

```python
# ---- Ports (Protocols/ABCs) ----
class LLMClient(Protocol):
    async def complete(self, *, role, messages, tools) -> LLMResult: ...

class EmailSurface(Protocol):                 # the browser engine behind a port
    async def observe(self) -> Observation: ...
    async def act(self, call: ActionCall) -> ActionResult: ...

class Worker(Protocol):                        # supervisor dispatches these (OCP: add a class)
    name: str
    async def run(self, state: AgentState) -> WorkerResult: ...

class RemediationStrategy(Protocol):           # self-heal options (OCP)
    def applies_to(self, cause: Cause) -> float: ...
    def to_option(self) -> Option: ...

class PiiVault(Protocol):
    def tokenize(self, text: str) -> str: ...
    def resolve(self, token: str) -> str: ...

class RulesStore(Protocol):   def active(self) -> list[Rule]: ...
class SkillRegistry(Protocol): def strategies_for(self, cause) -> list[RemediationStrategy]: ...
class Approver(Protocol):     async def request(self, req) -> Decision: ...   # HITL interrupt
class TrajectoryStore(Protocol): async def save(self, thread_id, rec) -> None: ...
class EventSink(Protocol):    async def emit(self, event) -> None: ...        # WS in prod, buffer in tests
```

- **S** — one job per class: each funnel stage, each action handler, each worker, each remediation
  strategy, each provider adapter.
- **O** — extend by adding a class: new worker / stage / rule matcher / remediation / provider; the
  supervisor, pipeline, dispatcher don't change.
- **L** — every `EmailSurface` / `Worker` / `LLMClient` impl is drop-in; tests use fakes.
- **I** — ports are narrow; no consumer depends on a method it doesn't call.
- **D** — the graph depends on abstractions; concretes are built in **one composition root** and
  injected. Nodes are closures over injected ports — **no LLM/CDP/DB inline**.

---

## 7. Free stack — Groq → Gemini, and the real constraint

The LLM gateway is a `FallbackLLMClient` composing ordered providers behind the one `LLMClient` port:

```
FallbackLLMClient([ GroqClient(...),      # langchain_groq.ChatGroq — fast, free, primary
                    GeminiClient(...) ])  # langchain_google_genai — free, fallback
# on 429 / quota / 5xx from Groq → transparently fall to Gemini. Same model within a retry.
```

- **Model per role, from config** — `classifier` (small: `llama-3.1-8b-instant`), `executor`
  (`llama-3.3-70b`), `validator` (small). Slugs are configuration, never hardcoded.
- **The real free-tier constraint is rate/quota, not dollars.** Mitigations: fallback chain, cheap
  classifier for triage, prompt caching, a lean loop (few tokens/turn), and batching the
  classification pass.
- **Rest of stack, all free:** Playwright Chromium; Gmail via the logged-in browser session;
  FastAPI + LangGraph; SQLite (dev) / Neon or Supabase Postgres free tier (prod); backend on HF
  Space / Render free / Fly free; frontend on Vercel; WebSockets for real-time.

---

## 8. Guardrails — consolidated (requirement 9)

- ❌ No raw DOM or raw PII over the wire — only tokenized funnel output.
- ❌ No LLM/CDP/DB calls inside graph nodes — go through ports.
- ❌ No LLM/provider key outside the backend.
- ❌ No `Send` / delete / bulk-irreversible action without an approval interrupt.
- ❌ No auto-send from rules unless a rule is explicitly whitelisted (off by default).
- ❌ No reuse of previous-turn indices — re-observe and renumber every turn.
- ❌ No silent truncation of the observation — always log dropped/hidden counts.
- ❌ No model re-routing *inside* a retry; fallback is between attempts, not mid-attempt.
- ❌ No mutable agent state outside `AgentState`.
- ❌ No self-source editing in the live loop (self-heal reads a curated `SkillRegistry` only).
- ❌ No task start before `context_gate` clears (100%-context rule).
- ✅ Every failure ends in a typed `ErrorCode`; per-action timeout wall; stuck + repetition guards.

---

## 9. One-week build order (reusing the browser engine)

| Day | Ships | Proves |
| --- | --- | --- |
| 1 | Contracts + `FallbackLLMClient` (Groq→Gemini) + `PiiVault` stage; point the funnel/session at Gmail | model calls work free; DOM→tokenized observation over mail |
| 2 | Manager graph skeleton + `intake` + `context_gate`(AskUser) + `router`(linear/decision) | "won't start until 100% context"; routing |
| 3 | `TriageWorker` observe→reason→act + WS streaming to the two-pane cockpit | live triage on screen |
| 4 | `ComposeWorker` ReAct + `approval_gate` interrupt + take-over | "send on X to Y", nothing sends without me |
| 5 | `diagnose`→`options`(1..4) + `SkillRegistry` + `Approver`; `RulesWorker` | self-heal with ranked HITL options |
| 6 | `CalendarWorker` + failure layer hardening + benchmark harness | reliability numbers, gated invite |
| 7 | Cockpit polish (LHS/RHS), guardrail audit, deploy (HF + Vercel) | end-to-end demo |

---

## 10. Open decisions (flag before M0)

- **Gmail access model:** drive the user's logged-in browser (extension/CDP, as your repo does) vs a
  dedicated test account in a controlled Playwright profile. The extension model is the real product;
  the test account is the safe demo — support both behind the `EmailSurface` port.
- **PII name tokenization depth:** addresses/phones are clear-cut; personal *names* in bodies are
  fuzzier — decide v1 coverage (addresses+phones first, names best-effort).
- **Router granularity:** two classes (linear/decision) for v1; a third "compound" class (multi-
  worker plans) can come later.
- **Calendar target:** Google Calendar via the same browser UI vs an `.ics` draft for v1.
