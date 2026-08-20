# AI System Design — Inbox Autopilot

- **Status:** Draft v1 · **Implements:** [`PRD.md`](PRD.md) R1–R9 · **Governed by:** [`../CLAUDE.md`](../CLAUDE.md)
- **Read with:** [`SECURITY-MODEL.md`](SECURITY-MODEL.md) (PII + injection), [`WS-PROTOCOL.md`](WS-PROTOCOL.md) (the wire)

---

## 1. The load-bearing idea

Gmail's DOM is 100k–500k tokens of divs, styles, and scripts. **It never reaches the model.** The
system is a funnel that discards ~99% of the page and hands the model a short numbered list of only
what it can act on or read — with every address, phone, and (best-effort) personal name already
replaced by a stable token.

```
raw DOM  ──▶ [ funnel: 7 stages ] ──▶ Observation (~1–3k tokens)
100k+ tok        executor-side           tokenized · numbered · no coordinates
                                              │
                                              ▼
                                    LLM picks a NUMBER + a TOKEN
                                              │
                                              ▼
                        executor resolves index→geometry, token→address
                                              │
                                              ▼
                              trusted CDP input (isTrusted: true)
```

Two hidden maps stay in the executor and are the reason this is safe:

| Map | Lives in | Contains |
| --- | --- | --- |
| `index → {backendNodeId, x, y}` | SoM indexer | geometry the model must never see |
| `token → real value` | `PiiVault` | the PII the model must never see |

Everything else in this document is the machinery that makes that loop reliable, observable, and
gated.

## 2. Deployment topology

```
┌──────────────── COCKPIT — Next.js on Vercel ─────────────────────────────┐
│  LHS: chat · run history · questions · ranked options · approval cards    │
│  RHS: live browser frames · current action label                          │
└────────────────────────────── WebSocket ⇅ ───────────────────────────────┘
        Observation / ActionCall / AgentEvent  —  never raw DOM, never raw PII
┌──────────── BACKEND — THE BRAIN · FastAPI + LangGraph · HF Space/Render ──┐
│  MANAGER / SUPERVISOR GRAPH                                               │
│   [PRE]  intake → context_gate ⟲(AskUser) → router → planner              │
│   [IN]   dispatch → workers{ Triage · Compose · Calendar · Rules }        │
│                      each = observe→reason→act ⟲ → approval_gate ⟲        │
│   [POST] verify → ⟨ok⟩ finalize | ⟨fail⟩ diagnose → options ⟲ → retry     │
│  ports: LLMClient · EmailSurface · PiiVault · RulesStore · SkillRegistry  │
│         Approver · TrajectoryStore · EventSink                            │
│  checkpointer keyed by thread_id (SQLite dev / Postgres prod)             │
│  ALL provider keys · ALL reasoning · ALL trajectories live here            │
└──────────────────── authenticated relay ⇅ ───────────────────────────────┘
┌──────────── EXECUTOR — next to the DOM ──────────────────────────────────┐
│  A: PlaywrightEmailSurface  — server-side Chromium (dev/CI/demo, default) │
│  B: ExtensionEmailSurface   — user's own Chrome via chrome.debugger       │
│  funnel (incl. PII tokenization) · trusted input dispatch · screencast    │
└──────────────────────────────────────────────────────────────────────────┘
```

**The split that must not be violated.**

| Concern | Side | Why |
| --- | --- | --- |
| Agent loop, reasoning, LLM keys, trajectories | **Backend** | Per-turn latency is dominated by the LLM call (seconds), not the relay (ms). Keeping the loop server-side keeps keys and reasoning off the user's machine. |
| Funnel + PII tokenization + geometry resolution | **Executor** | Raw DOM and raw PII must never cross the wire. Tokenizing at the source means no downstream component can leak what it never held. |
| Rendering only | **Cockpit** | The cockpit displays what the backend emitted. It performs no inference and holds no secrets. |

The two sides share exactly **two** contracts: `Observation` and `ActionCall`. Both are authored once
as Pydantic and generated into Zod ([`WS-PROTOCOL.md`](WS-PROTOCOL.md)).

## 3. The manager graph

```
START
  │
  ▼
intake ──────────────► parse NL → TaskIntent (typed). No side effects.
  │
  ▼
context_gate ⟲ AskUser ─► loop until every required slot is filled and confidence ≥ τ.
  │                       Read-only observation allowed. NO mailbox mutation.
  ▼
router ──────────────► Route ∈ {linear, decision}
  │
  ├─ linear ──────────► dispatch → RulesWorker (zero LLM calls per step)
  │
  └─ decision ────────► planner → dispatch → worker subgraph
                                     │
                                     ▼
                        ┌──── observe → reason → act ────┐
                        │        ▲                 │      │
                        │        └── re-observe ◄──┘      │
                        └──────────── │ ─────────────────┘
                                      ▼
                          approval_gate ⟲ (irreversible verbs only)
  │
  ▼
verify ──── ok ────► finalize ────► END
  │
  └── fail ──► diagnose ──► options ⟲ (HITL) ──► retry | finalize
```

### 3.1 Node contracts

Every node is a **closure over injected ports**, returns a **state delta**, and contains no
LLM/CDP/DB code inline.

| Node | Phase | Reads | Calls | Writes |
| --- | --- | --- | --- | --- |
| `intake` | PRE | `task` | `LLMClient(role=classifier)` | `intent` |
| `context_gate` | PRE | `intent` | slot registry; `EmailSurface.observe()` *(read-only)*; `interrupt()` | `missing_slots`, `intent`, `status` |
| `router` | PRE | `intent`, `RulesStore` | rule pre-check, else `LLMClient(role=classifier)` | `route` |
| `planner` | PRE | `intent` | `LLMClient(role=executor)` | `plan` |
| `dispatch` | IN | `route`, `intent` | `SkillRegistry`/worker registry | `active_worker` |
| `observe` | IN | — | `EmailSurface.observe()` | `observation`, `history` |
| `reason` | IN | `messages`, `observation` | `LLMClient(role=executor)` with bound tools | `messages`, `step`, usage |
| `act` | IN | last tool call | dispatcher → `EmailSurface.act()` | `last_action`, `last_result`, `history` |
| `approval_gate` | IN | pending irreversible call | `Approver.request()` → `interrupt()` | `pending_approval`, decision |
| `verify` | POST | `intent`, `observation` | contract check + optional rubric | `status`, `error_code` |
| `diagnose` | POST | `error_code`, diff, `last_action` | cause classifier + `SkillRegistry` | `diagnosis` |
| `options` | POST | `diagnosis` | `Approver.request()` → `interrupt()` | chosen remedy |
| `finalize` | POST | terminal state | `EventSink.emit(finalize)` | `status`, `success`, `reason` |

### 3.2 Routing is pure

All edge decisions are pure functions over state — no I/O, fully unit-testable, every branch covered:

```python
route_after_gate(state)   -> "ask" | "router"
route_after_router(state) -> "linear" | "planner"
route_after_reason(state) -> "act" | "reason" | "approval_gate" | "finalize"
route_after_act(state)    -> "observe" | "verify" | "finalize"
route_after_verify(state) -> "finalize" | "diagnose"
route_after_options(state)-> "dispatch" | "finalize"
```

Two universal nudge rules, both of which earn their place on a live surface:
- A model turn with a tool call but **no reasoning** → retry once → `REASONING_MISSING`.
- A model turn with **no tool call** → nudge once → `NO_ACTION`.

## 4. State — the single source of truth

```python
class AgentState(BaseModel):
    # identity
    task: str
    thread_id: str                          # == session; the checkpointer key

    # PRE
    intent: TaskIntent | None = None
    missing_slots: list[str] = []
    route: Literal["linear", "decision"] | None = None
    plan: Plan | None = None

    # IN
    active_worker: str | None = None
    messages: Annotated[list[BaseMessage], add_messages] = []
    observation: Observation | None = None   # tokenized, indexed, from the funnel
    agent_memory: dict[str, str] = {}        # Remember/Recall scratchpad
    history: Annotated[list[StepRecord], operator.add] = []
    last_action: ActionCall | None = None
    last_result: ActionResult | None = None
    pending_approval: ApprovalRequest | None = None

    # POST
    diagnosis: Diagnosis | None = None

    # control
    status: Literal["gathering","running","awaiting_human","done","failed"] = "gathering"
    error_code: ErrorCode | None = None
    step: int = 0
    finished: bool = False
    success: bool | None = None
    reason: str = ""

    # guards
    stuck_count: int = 0
    nudge_count: int = 0
    recent_actions: list[str] = []           # rolling window, repetition guard
```

Rules: `messages` and `history` use **append reducers**. No mutable agent state exists anywhere else
— not in a service, not in a module global, not on the session object.

## 5. PRE — earning 100% context (R3)

### 5.1 Slot registry

Each intent declares what it cannot proceed without. This is a plain data structure, not prompt text
— which is what makes the gate testable.

| Intent | Required slots | Optional slots |
| --- | --- | --- |
| `send_email` | `recipient_identity`, `topic \| body_intent` | `tone`, `cc`, `subject`, `deadline` |
| `reply` | `thread_ref`, `stance \| body_intent` | `tone`, `include_quote` |
| `triage` | `scope` (inbox / label / query) | `aggressiveness`, `dry_run` |
| `archive` / `label` / `snooze` | `selector` (query, sender, or label), `target_label \| until` | — |
| `extract_event` | `thread_ref` | `calendar`, `attendees`, `duration` |
| `search` | `query` | `limit` |
| `apply_rules` | — (rules are the input) | `dry_run` |

### 5.2 The gate algorithm

```
1. Extract slots from the TaskIntent.
2. For each required slot: present? unambiguous?
3. Ambiguity may be resolved by READ-ONLY observation
   ("which Priya?" → observe the contact list → 2 candidates, offer both as tokens).
4. confidence = f(slots_filled, ambiguity_count, intent_classifier_confidence)
5. If confidence < τ (default 0.85) or any required slot is unresolved:
       emit AskUser interrupt  →  status = "awaiting_human"  →  durable pause
       resume with Command(resume=answer)  →  goto 1
6. Else proceed to router.
```

**Why an interrupt and not a blocking prompt.** The interrupt is checkpointed. The process can
restart, the cockpit can disconnect and reconnect, and the run resumes exactly where it paused. A
blocking prompt would tie the run's lifetime to one socket.

**The hard invariant:** no node downstream of `context_gate` may run before the gate emits
`status != "gathering"`. This is enforced by graph topology (there is no edge that skips it) and
asserted by a test.

### 5.3 Router — linear vs decision (R6)

```
1. Deterministic pre-check: does the task match an active Rule in RulesStore?
       yes → route = linear    (ZERO LLM calls — the cheapest correct path)
2. Else: one small classifier call → typed Route.
3. Escalation valve: a linear worker that encounters ambiguity mid-run
   returns ESCALATE; the supervisor re-dispatches on the decision path.
```

| | linear | decision |
| --- | --- | --- |
| Example | "archive all newsletters", "mark everything from X read" | "reply to the ones that need me", "book the meeting from this thread" |
| Topology | straight-line worker, batch-executed | full observe→reason→act loop |
| LLM calls per step | **0** | 1 |
| Failure mode | deterministic; a miss is a rule gap | perceptual; a miss routes to self-heal |

## 6. The observation funnel (executor-side)

Seven single-responsibility stages. Adding a capability = adding a stage class; the pipeline does not
change (Open/Closed).

| # | Stage | Transform | Drops |
| --- | --- | --- | --- |
| 1 | `Extract` | CDP `DOMSnapshot` + `Accessibility.getFullAXTree` + geometry + screenshot | — |
| 2 | `VisibilityFilter` | remove `display:none`, `visibility:hidden`, `opacity:0`, zero-size, off-screen | ~60% |
| 3 | `OcclusionCuller` | remove elements physically covered (content behind an open compose panel or modal) | ~10% |
| 4 | `WrapperCollapser` | `div>div>div>button` → `button` | ~15% |
| 5 | **`PiiTokenizer`** | addresses / phones / best-effort names → stable tokens; record `token→real` in `PiiVault` | 0 (rewrites) |
| 6 | `SoMIndexer` | assign `[N]` to each interactable; build the hidden `index → geometry` map | 0 |
| 7 | `ReadingOrderFormatter` | serialize survivors in reading order; `## Current Focus`; `changed` diff; enforce token budget | budget-driven |

**Stage 5 placement is load-bearing.** The tokenizer runs *before* indexing and formatting, so no
downstream stage — and therefore nothing that could be serialized, logged, or transmitted — ever
holds raw PII. Moving it later would be a security regression, not a refactor.

**Budget policy.** When the serialized list exceeds the token budget, drop lowest-priority items
first (off-screen, non-interactive) and **report the count**: `droppedCount: 18`, surfaced to the
model as "18 off-screen items hidden — scroll to see them." Silent truncation makes an agent
confidently wrong; this is a hard guardrail.

**Change detection.** After an action, the observation carries a `changed` summary ("compose panel
opened", "navigated to /mail/u/0/#sent") plus `isNew` flags on elements absent last turn. The model
gets a diff *and* a fresh list — never a blind full dump.

## 7. IN — supervisor and workers

### 7.1 Worker interface

```python
class Worker(Protocol):
    name: str
    topology: Literal["linear", "decision"]
    async def run(self, state: AgentState) -> WorkerResult: ...
```

| Worker | Topology | Job | Gated verbs |
| --- | --- | --- | --- |
| `TriageWorker` | decision | funnel the inbox → archive / label / snooze / read the backlog | delete-forever |
| `ComposeWorker` | decision | compose → fill fields → approval → send | **Send** |
| `CalendarWorker` | decision | extract event from a thread → create → approval on the invite | **invite dispatch** |
| `RulesWorker` | linear | apply deterministic rules, short-circuiting the LLM | auto-send (off by default) |

Adding a worker touches exactly two places: a new class, and one line in the registry. The supervisor
is closed to modification.

### 7.2 Action vocabulary

Every action targets by **index** or **token**, is resolved executor-side, and is performed as
**trusted input** (CDP `Input.*`, `isTrusted: true`) — never JS `.click()` by default. Each verb is
one dispatcher handler with a per-action timeout wall; a breach is `ACTION_TIMEOUT`.

| Verb | Reversible | Approval | Timeout |
| --- | --- | --- | --- |
| `Navigate` / `Scroll` / `WaitFor` / `Read` | n/a | no | 30 / 5 / 30 / 15 s |
| `Click` / `Type` / `Clear` / `SelectOption` / `PressKey` | contextual | no | 10 s |
| `Archive` / `Label` / `Snooze` / `MarkRead` | **yes** | no | 10 s |
| `DraftReply` / `Compose` | yes (a draft) | no | 15 s |
| **`Send`** | **no** | **yes — interrupt** | 20 s |
| **`ExtractToCalendar` → invite dispatch** | event yes / invite no | **yes on invite** | 20 s |
| **`DeleteForever`** | **no** | **yes — interrupt** | 10 s |
| Meta: `Complete` / `Remember` / `Recall` / `SetPlan` / `AskUser` | n/a | no | — |

**After every action:** settle (adaptive per-host bound, `mean + 2σ` clamped to `[min, max]`), then
**re-observe from scratch**. The engine does not track page changes — a popup simply appears in the
next fresh list, and occlusion culling hides what is behind it, so the modal becomes the salient
thing. Indices are never reused across turns.

Every mutating verb writes enough to its `StepRecord` to be undone (previous label set, previous
folder, message id token).

### 7.3 The compose ReAct flow (R2)

```
context_gate cleared: recipient_identity=P17, topic="friday demo moved to 4pm"

turn 1  reason "Compose is [12]; open it."            act Click(12)        observe → panel open
turn 2  reason "Fill the To field with P17."          act Type(14, "P17")  observe → chip resolved
        └─ executor resolves P17 → priya@… ONLY at dispatch; the model never saw the address
turn 3  reason "Subject should name the change."      act Type(17, "…")    observe
turn 4  reason "Write the body, ask if 4pm works."    act Type(21, "…")    observe
turn 5  reason "Draft is complete; Send is [27]."     act Send(27)
        └─ APPROVAL INTERRUPT ────────────────────────────────────────────────┐
             cockpit renders the RESOLVED draft (real name + address, for the │
             human to verify) with Approve / Edit / Reject                    │
        ┌────────────────────────────────────────────────────────────────────┘
        │ Approve → click Send → verify (message present in Sent?) → done
        │ Edit    → user text replaces the field → loop continues at turn 3/4
        │ Reject  → no send → offer alternative → or Complete(success=false)
```

The human sees each field fill live. Nothing sends without an explicit decision.

## 8. Approval gate mechanics (R2 + R9)

```python
class ApprovalRequest(BaseModel):
    kind: Literal["send", "invite", "delete", "bulk"]
    summary: str            # human-readable, PII RESOLVED for the human's eyes only
    payload: dict           # the exact ActionCall that will execute on approve
    reversible: bool = False
    expires_at: datetime

class Decision(BaseModel):
    verdict: Literal["approve", "edit", "reject"]
    edit: str | None = None
    reason: str | None = None
```

Rules:
- The gate is **structural**, not advisory. `Send` has no code path that reaches `EmailSurface.act()`
  without a recorded `Decision(verdict="approve")` for that exact payload.
- The resolved draft is rendered **for the human only**, over the authenticated cockpit socket. It is
  never written back into `messages`, the trajectory, or any LLM request.
- Timeout → `APPROVAL_TIMEOUT`. Reject with no alternative → `APPROVAL_REJECTED_NO_ALT`. Both are
  typed terminal states, not silent stalls.
- An `Edit` decision replaces field content and returns the loop to the relevant fill step — it does
  **not** approve the send.

## 9. POST — verify, diagnose, options (R4)

### 9.1 verify

Two layers, cheapest first:

1. **Contract check** (deterministic, free): is the message present in Sent? does the thread now carry
   the label? did the archived count match the selection? is the draft persisted?
2. **Rubric check** (one small LLM call, only if the contract check is inconclusive): does the final
   observation satisfy the stated goal?

### 9.2 Cause classification

A pure function: `(error_code, last_action, observation_diff, page_signature) → Cause`.

| Signal | Cause | Plain-language diagnosis |
| --- | --- | --- |
| `STUCK` + unchanged signature + new modal in diff | `OVERLAY_BLOCKING` | "A dialog is covering the button." |
| `STUCK` + unchanged signature, no modal | `TARGET_MOVED` | "The button I was aiming at isn't where I expected." |
| `ACTION_TIMEOUT` + rising latency | `SLOW_RENDER` | "The page is still loading." |
| `ACTION_TIMEOUT` + navigation in diff | `NAVIGATED_AWAY` | "The page changed under me mid-action." |
| repetition guard fired | `OSCILLATION` | "I kept repeating myself without progress." |
| `droppedCount > 0` + target absent | `OFF_SCREEN` | "What I need is below the fold." |
| provider 429 / quota | `PROVIDER_EXHAUSTED` | "The model provider is rate-limiting me." |
| `REASONING_MISSING` | `MODEL_DEGRADED` | "The model stopped explaining itself." |
| `APPROVAL_*` | `HUMAN_BLOCKED` | "I'm waiting on you / you declined." |

### 9.3 SkillRegistry → four ranked options

```python
class RemediationStrategy(Protocol):
    name: str
    def applies_to(self, cause: Cause) -> float: ...   # 0.0–1.0 confidence
    def to_option(self) -> Option: ...
    async def execute(self, state: AgentState) -> StateDelta: ...
```

v1 registry (curated, versioned, unit-tested in isolation):

| Strategy | Applies to | Does |
| --- | --- | --- |
| `DismissOverlay` | `OVERLAY_BLOCKING` | find + close the topmost modal, retry the original action |
| `ScrollAndRetry` | `OFF_SCREEN`, `TARGET_MOVED` | scroll toward the target, re-observe, retry |
| `WidenObservation` | `TARGET_MOVED`, `OFF_SCREEN` | raise the token budget for one turn, re-observe |
| `WaitAndRetry` | `SLOW_RENDER` | extend the settle bound, re-observe |
| `KeyboardShortcut` | `TARGET_MOVED` (Gmail) | use Gmail's keyboard shortcut instead of clicking |
| `ReloadAndResume` | `NAVIGATED_AWAY`, `OSCILLATION` | reload Gmail, replay the plan from the last verified step |
| `SwitchProvider` | `PROVIDER_EXHAUSTED` | force the next provider in the fallback chain |
| `AskUser` | any | ask the human what to do (always available as the floor) |

**Options assembly:**

```
strategies = SkillRegistry.strategies_for(cause)      # sorted by applies_to() desc
option[1] = strategies[0]  → labelled "Recommended"
option[2] = strategies[1]
option[3] = strategies[2]
option[4] = FreeForm       → always present; user text becomes loop guidance
```

**Anti-loop rule:** if the same `Cause` is diagnosed twice within one run, options 1–3 are recomputed
excluding already-attempted strategies, and a third occurrence finalizes with the accumulated
diagnosis rather than asking again. Self-heal must terminate.

## 10. Failure and loop engineering

All of these are required and all produce typed codes. No failure exits untyped.

| Guard | Trigger | Response |
| --- | --- | --- |
| **Action repetition** | signature `(verb, target_token, args_hash)` repeats in a rolling window | hard nudge at 3; kill at 5 → `STUCK`. Excludes distinct-target `Archive`/`Snooze`/`Read`, and all `scroll`/`wait`/`read`. |
| **Stuck signature** | observation unchanged N turns | soft nudge at 2; kill at 8 → `STUCK` |
| **Runaway output** | reasoning > ~3k chars | clip before it enters history (an unclipped blob poisons every later turn *and* feeds the model its own repetition) |
| **Think-before-act** | tool call with empty reasoning | retry once → `REASONING_MISSING` |
| **No tool call** | model returned prose only | nudge once → `NO_ACTION` |
| **Step budget** | ≤5 steps remaining | inject "call `Complete()` now with your findings" — a well-explained partial beats a `MAX_STEPS` timeout |
| **Action timeout** | per-verb wall breached | `ACTION_TIMEOUT` → one retry via `observe`, then diagnose |
| **Approval timeout** | decision never arrives | `APPROVAL_TIMEOUT` |
| **Approval rejected** | rejected, no alternative | `APPROVAL_REJECTED_NO_ALT` |

```python
class ErrorCode(str, Enum):
    STUCK = "STUCK"
    ACTION_TIMEOUT = "ACTION_TIMEOUT"
    REASONING_MISSING = "REASONING_MISSING"
    MAX_STEPS = "MAX_STEPS"
    NO_ACTION = "NO_ACTION"
    APPROVAL_TIMEOUT = "APPROVAL_TIMEOUT"
    APPROVAL_REJECTED_NO_ALT = "APPROVAL_REJECTED_NO_ALT"
    CONTEXT_INCOMPLETE = "CONTEXT_INCOMPLETE"
    PROVIDER_EXHAUSTED = "PROVIDER_EXHAUSTED"
    SURFACE_UNAVAILABLE = "SURFACE_UNAVAILABLE"
```

There is **no** human-in-the-loop failure fallback. HITL is used for three deliberate purposes —
AskUser (context), approval (irreversible), options (self-heal) — never as a way to avoid typing a
failure.

## 11. LLM gateway (R9, cost)

One port, one composite implementation, an ordered chain:

```python
FallbackLLMClient([
    GroqClient(...),        # primary  — best free throughput/min
    OpenRouterClient(...),  # fallback — OpenAI-compatible base_url swap, :free models
    GeminiClient(...),      # fallback — free tier
])
# On 429 / quota / 5xx → advance to the next provider.
# NEVER re-route models INSIDE a retry; fallback happens between attempts.
```

| Role | Model size | Used by | Why |
| --- | --- | --- | --- |
| `classifier` | small | `intake`, `router`, batched triage scoring | high volume, low difficulty |
| `executor` | large | `reason`, `planner` | the actual judgment |
| `validator` | small | `verify` rubric layer | binary check |

Rules: keys are **server-side only**; model slugs come from config and are **never hardcoded** (the
`:free` roster rotates); every call is metered (tokens by role+provider, latency) into a
`StepRecord`; transient errors retry with backoff respecting `Retry-After`.

**The real constraint is rate, not dollars.** Mitigations, in order of impact: the linear route
(0 calls), batched classification (one call scores N subjects instead of N calls), the fallback chain,
a small classifier model for triage, prompt caching on a stable prefix (instructions + tool schema +
task cache-marked; growing memory in a later message so appends don't bust the cache), and a lean
loop.

## 12. Streaming, persistence, telemetry

**Streaming.** `graph.astream(stream_mode="updates")` → every node update → `EventSink` → WS hub →
cockpit. Reasoning streams token-by-token. Browser frames stream over the same socket via CDP
screencast.

**Run lifetime is decoupled from socket lifetime.** A process-level run registry keyed by `thread_id`
means a cockpit refresh **detaches the view** while the run and its browser keep going; reconnecting
with `{type: "attach", thread_id}` replays the buffered history and goes live again. Runs last
minutes and contain human pauses that outlive a browser tab — tying run lifetime to a socket is the
difference between a demo and a product.

**Persistence.** LangGraph checkpointer keyed by `thread_id` (SQLite dev, Postgres prod) buys
trajectory persistence, durable pause/resume, and all three HITL interrupts for free.

**Telemetry.** Every step writes a `StepRecord{step, node, worker, action, result, error_code,
provider, role, input_tokens, output_tokens, latency_ms}`. The ordered records **are** the
trajectory: replayable, auditable, and the substrate for the benchmark.

## 13. Ports (dependency inversion)

Nodes and the composition root are the only places that know concrete types.

```python
class LLMClient(Protocol):
    async def complete(self, *, role: str, messages, tools) -> LLMResult: ...

class EmailSurface(Protocol):
    async def observe(self) -> Observation: ...
    async def act(self, call: ActionCall) -> ActionResult: ...

class PiiVault(Protocol):
    def tokenize(self, text: str) -> str: ...
    def resolve(self, token: str) -> str: ...

class Worker(Protocol):            name: str; topology: str
                                   async def run(self, state) -> WorkerResult: ...
class RulesStore(Protocol):        def active(self) -> list[Rule]: ...
class SkillRegistry(Protocol):     def strategies_for(self, cause) -> list[RemediationStrategy]: ...
class Approver(Protocol):          async def request(self, req: ApprovalRequest) -> Decision: ...
class TrajectoryStore(Protocol):   async def save(self, thread_id, rec: StepRecord) -> None: ...
class EventSink(Protocol):         async def emit(self, event: AgentEvent) -> None: ...
```

**Two `EmailSurface` implementations, swappable with zero graph changes** — this is the SOLID payoff
and it is the main architectural bet of the project:

| Impl | Runs | Use |
| --- | --- | --- |
| `PlaywrightEmailSurface` | server-side headful Chromium under xvfb | dev, CI, demo — **default** |
| `ExtensionEmailSurface` | the user's own Chrome via `chrome.debugger` | the real product: real profile, real session, real IP |

## 14. Scaling

- **Stateless brain replicas** behind the WS hub; all run state lives in the checkpointer (Postgres,
  keyed by `thread_id`) → horizontal scale and resume across restarts.
- **One browser context per session**; pool and cap concurrent browsers; queue beyond the cap.
- **Provider fallback** absorbs free-tier rate limits; a small classifier keeps the large model for
  judgment only.
- **Prompt caching** on a stable prefix keeps per-turn input cost roughly flat as history grows.
- **Compaction** at ~95% of the context window: strip old screenshots and observations once seen
  (layer 0), truncate old tool outputs (layer 1), LLM-summarize the middle keeping first-2 + last-6
  verbatim (layer 2).

## 15. Sequence — a full compose run

```
cockpit ──{start, task}──▶ backend
backend: intake ──▶ classifier LLM ──▶ TaskIntent
backend: context_gate ──▶ slots incomplete
backend ──{question}──▶ cockpit                        [interrupt · durable pause]
cockpit ──{answer}──▶ backend  ──▶ Command(resume=…)
backend: context_gate ──▶ confidence 0.93 ≥ τ ──▶ router ──▶ decision ──▶ planner
backend ──{plan_update}──▶ cockpit
loop {
  backend: observe  ──▶ EmailSurface.observe()  [funnel + tokenize, executor-side]
  backend ──{observation, frame}──▶ cockpit
  backend: reason   ──▶ executor LLM (tools bound)
  backend ──{stream…, reasoning, tool_call}──▶ cockpit
  backend: act      ──▶ EmailSurface.act()  [resolve index+token, trusted input]
  backend ──{frame}──▶ cockpit
}
backend: approval_gate ──{approval_request}──▶ cockpit  [interrupt · durable pause]
cockpit ──{decision: approve}──▶ backend ──▶ Command(resume=…)
backend: act(Send) ──▶ verify (message in Sent?) ──▶ finalize
backend ──{finalize, run_complete}──▶ cockpit
```
