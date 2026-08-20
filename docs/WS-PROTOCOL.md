# Wire Protocol — contracts and the cockpit event vocabulary

- **Source of truth:** `packages/contracts/src_py/` (Pydantic v2). Everything else is **generated**.
- **Never hand-edit** `packages/contracts/schema/*.json` or `packages/contracts/src/generated/*.ts`.

---

## 1. Contracts pipeline

```
packages/contracts/src_py/          Pydantic v2  ← the ONLY place these types are authored
        │  model_json_schema(by_alias=True)
        ▼
packages/contracts/schema/*.json    JSON Schema  (generated, committed)
        │  json-schema-to-zod
        ▼
packages/contracts/src/generated/   Zod + TS     (generated, committed)
        │
        ├─▶ frontend/            (Next.js cockpit)
        └─▶ bridge-extension/    (MV3 executor)

backend/  takes a uv path-dependency on src_py/ — it imports the Python models directly.
```

`just check` regenerates and fails on drift. CI runs it. A PR with drift does not merge.

**Casing:** camelCase on the wire; snake_case in Python with Pydantic aliases; schema emitted
`by_alias=True`. Every message carries `protocolVersion`; a mismatch is rejected with a clear error
rather than silently coerced.

## 2. The two boundary contracts

These are the **only** types the backend and the executor share.

### `Observation` — what the model is allowed to see

```python
class Viewport(BaseModel):
    width: int
    height: int
    scroll_x: int = Field(0, alias="scrollX")
    scroll_y: int = Field(0, alias="scrollY")

class Element(BaseModel):
    index: int                    # the SoM number the model references
    role: str                     # AX role: button, textbox, link, listitem…
    name: str = ""                # ALREADY TOKENIZED
    value: str | None = None      # ALREADY TOKENIZED
    is_new: bool = Field(False, alias="isNew")   # absent in the previous turn

class MailContext(BaseModel):
    """Email-surface semantics layered on the generic observation."""
    view: Literal["inbox","thread","compose","search","sent","drafts","calendar"]
    thread_token: str | None = Field(None, alias="threadToken")
    unread_count: int | None = Field(None, alias="unreadCount")
    compose_open: bool = Field(False, alias="composeOpen")

class Observation(BaseModel):
    protocol_version: str = Field(PROTOCOL_VERSION, alias="protocolVersion")
    context_id: str = Field(alias="contextId")
    title: str = ""
    viewport: Viewport
    elements: list[Element] = []
    mail: MailContext | None = None
    screenshot_ref: str | None = Field(None, alias="screenshotRef")
    changed: str | None = None          # human-readable diff vs last turn
    dropped_count: int = Field(0, alias="droppedCount")
```

**Invariants — each is a test:**

| # | Invariant |
| --- | --- |
| 1 | **No coordinates.** Geometry stays in the executor's `index → {x, y, backendNodeId}` map. |
| 2 | **No raw DOM.** Not a fragment, not a selector, not an `outerHTML`. |
| 3 | **No raw PII.** `name` and `value` are post-tokenizer. |
| 4 | **No URL.** A Gmail URL leaks message identifiers; `contextId` is an opaque token instead. |
| 5 | **Indices are per-turn.** Valid only for the observation that produced them. |
| 6 | **`droppedCount` is honest.** If the budget dropped items, it says how many. |

> Note invariant 4. A general-purpose page observation would naturally carry `url`, but on an email
> surface the URL is itself an identifier — it leaks message and thread ids. It is replaced by an
> opaque `contextId` plus the semantic `mail` block, which is what the agent actually needs.

### `ActionCall` / `ActionResult` — what the model is allowed to do

```python
class ActionCall(BaseModel):
    name: str                      # the verb
    args: dict[str, Any] = {}      # targets by INDEX and TOKEN only

class ActionResult(BaseModel):
    success: bool
    reason: str = ""
    error_code: str | None = Field(None, alias="errorCode")
    undo: dict[str, Any] | None = None   # enough to reverse a mutating verb
```

Dispatch-time validation, executor-side, before anything touches the page:

1. Every `index` argument must exist in the current turn's index map → else `STALE_INDEX`.
2. Every `token` argument must exist in the vault → else `UNKNOWN_TOKEN`. **A literal email address
   in an argument is rejected** — that is the signature of an injected recipient.
3. The verb must be in the active worker's bound schema → else `VERB_NOT_BOUND`.
4. A gated verb must carry a matching approval decision → else `APPROVAL_REQUIRED`.

### `Envelope` — the relay frame

```python
class Envelope(BaseModel):
    protocol_version: str = Field(PROTOCOL_VERSION, alias="protocolVersion")
    type: str
    id: str | None = None       # correlation id: a request carries one, its response echoes it
    payload: dict[str, Any] = {}
```

## 3. Cockpit socket

```
ws://<backend>/ws/run
```

One run per connection lifecycle is the common case, but the connection MAY stay open and accept a
new `start`. While a run is active, `answer` / `decision` / `stop` are routed to it.

### 3.1 Client → Server

```jsonc
{ "type": "start",    "task": "send an email to priya about the friday demo",
                      "threadId": "optional — server generates one otherwise" }

{ "type": "attach",   "threadId": "ws-a1b2c3d4" }        // reconnect to a live run

{ "type": "answer",   "answer": "the first one — tell him it moved to 4pm" }

{ "type": "decision", "requestId": "ap-7",
                      "verdict": "approve" | "edit" | "reject",
                      "edit": "optional replacement text",
                      "reason": "optional" }

{ "type": "choice",   "requestId": "op-3",
                      "option": 1 | 2 | 3 | 4,
                      "text": "required when option == 4" }

{ "type": "stop" }
```

### 3.2 Server → Client

Every frame is an `AgentEvent`:

```jsonc
{ "event": "<type>", "data": { ... }, "ts": "<UTC ISO-8601>" }
```

| `event` | `data` | Cockpit rendering |
| --- | --- | --- |
| `status` | `{ phase, message }` | top status line (`gathering` / `running` / `awaiting_human` / …) |
| `intent` | `{ action, slots, confidence }` | LHS: "I understood this as…" |
| `question` | `{ requestId, question, context, candidates? }` | **LHS QuestionCard** — blocks until answered; `candidates` may offer tokenized choices |
| `route` | `{ route, why }` | small badge: `linear` / `decision` |
| `plan_update` | `{ steps }` | LHS plan list, checked off as steps complete |
| `stream` | `{ token }` | live thinking — typewriter into the current bubble |
| `reasoning` | `{ text }` | finalized reasoning for the turn; freezes the streamed bubble |
| `evaluation` | `{ text }` | the model's self-assessment of its previous action |
| `tool_call` | `{ name, args }` | one line: `→ Type(14, "P17")` |
| `observation` | `{ contextId, elements, droppedCount, changed, mail }` | RHS element count + "18 hidden" badge |
| `frame` | `{ jpegBase64, seq }` | **RHS live browser view** (canvas) |
| `action_label` | `{ text }` | RHS overlay: "typing → subject" |
| `approval_request` | `{ requestId, kind, summary, preview, reversible, expiresAt }` | **ApprovalCard** — `preview` carries the RESOLVED draft for human eyes |
| `approval_result` | `{ requestId, verdict }` | collapses the card |
| `diagnosis` | `{ cause, plain, evidence }` | "A dialog is covering the button." |
| `options` | `{ requestId, options: [{n, label, detail, recommended}] }` | **OptionsCard** — 1 Recommended, 2, 3, 4 free-form |
| `usage` | `{ provider, role, inputTokens, outputTokens, latencyMs }` | small meter |
| `context_status` | `{ inputTokens, budget, compacted }` | context gauge |
| `memory_update` | `{ key, value }` | memory chips |
| `error` | `{ message, errorCode? }` | error banner |
| `finalize` | `{ success, reason, errorCode? }` | terminal card |
| `run_complete` | `{ success?, reason?, stopped? }` | **server sentinel** — the stream for this run has ended |
| `run_absent` | `{ threadId }` | an `attach` target no longer exists; cockpit resets |

**`stream` vs `reasoning`:** same content, different timing. `stream` is the per-token delta during
generation; `reasoning` is the full text emitted once the turn's LLM call returns. Accumulate
`stream` into the current thinking bubble and freeze it on the next `reasoning` or `tool_call`. The
think-before-act retry can produce a **second** burst of `stream` tokens within one turn — the
cockpit must handle that without duplicating the bubble.

**`finalize` vs `run_complete`:** `finalize` is the agent's own terminal statement and may be absent
on some failure paths. `run_complete` is the server's sentinel and is **always** sent. The cockpit
keys its end-of-run state off `run_complete`.

## 4. The three human-in-the-loop flows

All three are LangGraph `interrupt()`s: durable, checkpointed, and resumable across a process
restart.

### 4.1 AskUser — context gate (R3)

```
context_gate finds a missing/ambiguous slot
  → server emits { event: "question", data: { requestId, question, context, candidates } }
  → status becomes "awaiting_human"; nothing else streams
  → cockpit renders QuestionCard
  → user submits → { type: "answer", answer: "…" }
  → server resumes with Command(resume=answer) → gate re-evaluates → loop or proceed
```

### 4.2 Approval — before anything irreversible (R2)

```
worker reaches a gated verb
  → server emits { event: "approval_request", data: { requestId, kind: "send", summary,
                                                      preview: <RESOLVED draft>, expiresAt } }
  → cockpit renders ApprovalCard with Approve / Edit / Reject
  → user submits → { type: "decision", requestId, verdict, edit? }
  → approve → the exact payload executes → verify
    edit    → the field is replaced; the loop returns to the fill step (NOT an approval)
    reject  → no execution; offer an alternative or Complete(success=false)
  → no decision before expiresAt → APPROVAL_TIMEOUT
```

`preview` is the one place resolved PII crosses to the cockpit. It is for the human's eyes and is
never written back into `messages`, the trajectory, or any LLM request.

### 4.3 Options — self-heal (R4)

```
verify fails / a typed error occurs
  → diagnose classifies a Cause
  → server emits { event: "diagnosis", data: { cause, plain, evidence } }
  → server emits { event: "options", data: { requestId, options: [
        { n: 1, label: "Dismiss the dialog and retry Compose", recommended: true },
        { n: 2, label: "Reload Gmail and start the compose over" },
        { n: 3, label: "Use the keyboard shortcut c instead" },
        { n: 4, label: "Other — tell me what to do", freeform: true } ] } }
  → user submits → { type: "choice", requestId, option: 1 }
    (option 4 requires `text`, which becomes loop guidance)
  → the chosen strategy executes → re-enter the loop, or finalize
```

## 5. Run lifetime and reconnection

The run outlives the socket. This is a product requirement, not an optimization.

| Event | Behaviour |
| --- | --- |
| Cockpit refresh / disconnect | The **view detaches**. The run, its browser, and any pending interrupt continue. |
| Reconnect with `{type:"attach", threadId}` | Buffered events replay in order, then the socket goes live. Replay is the same code path as live rendering. |
| `attach` to an unknown/GC'd run | `run_absent` → the cockpit resets to the hero state. |
| `stop` | The run is cancelled, `run_complete{stopped:true}` is emitted, the browser is torn down. |
| Terminal run, within TTL | Still attachable; replays through `run_complete`. |
| Terminal run, past TTL | Garbage-collected; `attach` yields `run_absent`. |

A pending interrupt survives a disconnect. Reattaching re-renders the QuestionCard, ApprovalCard, or
OptionsCard exactly as it was — the run is genuinely paused, not waiting on a socket.

## 6. Executor relay socket

```
ws://<backend>/ws/bridge
```

Used by `ExtensionEmailSurface` (the user's own Chrome). Request/response over `Envelope`, correlated
by `id`:

| Direction | `type` | Payload |
| --- | --- | --- |
| ext → backend | `register` | `{ capabilities, protocolVersion }` |
| backend → ext | `observe` | `{}` → response `{ observation }` |
| backend → ext | `act` | `{ call: ActionCall }` → response `{ result: ActionResult }` |
| ext → backend | `frame` | `{ jpegBase64, seq }` (unsolicited) |
| ext → backend | `detached` | `{ reason }` — the user took their tab back |

**The extension runs the funnel and the tokenizer.** Raw DOM and raw PII never enter this socket. The
extension ships no provider key and never talks to a model.

## 7. Versioning

- `PROTOCOL_VERSION` lives in `packages/contracts` and rides on every message.
- A mismatch between backend and cockpit/extension is rejected loudly at handshake — never coerced.
- Additive fields are a **minor** bump. Removing or retyping a field is a **major** bump and requires
  regenerating both sides in the same commit.
