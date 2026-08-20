# Architecture Decision Records

Each record states the decision, what was rejected, and **why** — the reasoning ages better than the
code. A decision is only reversed by a new ADR, never by a quiet refactor.

**Status key:** ✅ accepted · 🔁 superseded · ⏸ deferred

---

## ADR-001 — Browser-driven, not API-driven ✅

**Decision.** v1 operates Gmail's **web UI** through a real browser. The `EmailSurface` port keeps an
API adapter possible later; it is not built now.

**Rejected.** The Gmail REST API.

**Why.** Three reasons, in order of weight:
1. **Reach.** The UI can do everything a user can do. The API cannot — and every gap becomes a
   feature we cannot ship.
2. **The hard parts generalize.** The observation funnel, Set-of-Marks indexing, trusted-input
   dispatch, and the typed-failure layer are surface-agnostic — they are the expensive, reusable core
   of any UI-driving agent. An API adapter throws all of it away to solve a narrower problem.
3. **Consent friction.** Broad Gmail OAuth scopes are exactly what users refuse to grant. Driving
   their own already-logged-in browser asks for nothing new.

**Cost accepted.** The UI is a moving target and automation of it is detectable. Mitigated by a
role/geometry-based funnel (not selectors), typed failures routing into self-heal, and headful +
stealth by default.

---

## ADR-002 — Manager graph above a worker loop ✅

**Decision.** The `observe → reason → act` loop is **one worker**. Everything new —
intake, context gate, routing, dispatch, approval, verify, self-heal — is a **supervisor graph above
it**.

**Rejected.** Extending the existing single loop with more tools and more prompt.

**Why.** The new requirements are not perceptual, they are **procedural**: "don't start without full
context", "route linear vs decision", "gate irreversible actions", "diagnose and offer options".
Cramming procedure into a prompt makes it unenforceable and untestable. As graph topology it is
structural — a test can prove that no path skips the gate, which is a guarantee a prompt can never
give.

**Consequence.** Adding a worker is adding a class. The supervisor never changes.

---

## ADR-003 — LangGraph `StateGraph`, not a `while` loop ✅

**Decision.** Orchestration is an explicit state machine.

**Why.** Three things fall out for free and are individually hard to build: the **checkpointer**
(durable pause/resume keyed by `thread_id`), **`interrupt()`** (all three HITL flows), and
**`astream(stream_mode="updates")`** (real-time visibility, R8). A `while` loop would need all three
hand-rolled and would still not be inspectable.

**Consequence.** Nodes must be thin closures returning state deltas. Use a closure, not
`functools.partial` — LangGraph can mis-detect a partial of a coroutine as a sync node and fail to
await it. (Observed, not theoretical.)

---

## ADR-004 — Structured tool calls, not code execution ✅

**Decision.** Actions are schema-validated tool calls (`ActionCall{name, args}`). No CodeAct, no
sandbox, no `run_python`.

**Rejected.** Code execution as the action mechanism.

**Why.** On an adversarial surface the benefit/cost inverts:
- **Email bodies enter the model's context.** Prompt injection on CodeAct escalates to **backend
  RCE**. Sandbox escapes are a perennial class of bug; this is not a risk worth taking for
  convenience.
- **The composition benefit is weak here anyway.** SoM indices go stale the moment the page mutates,
  and the design mandates re-observe-and-rebuild every turn — so multi-step code in one turn is
  operating on indices it cannot trust.
- Tool calls are schema-validated, observable, and map 1:1 onto the wire contract.

Composition is recovered through richer tools (`SearchPage`, `Extract`, `WaitFor`) and parallel tool
calls. Every mainstream browser- and computer-control agent converged on structured actions rather
than code execution for the same reasons.

---

## ADR-005 — PII tokenization inside the funnel, before indexing ✅

**Decision.** `PiiTokenizer` is funnel **stage 5**, running in the executor before `SoMIndexer` and
`ReadingOrderFormatter`.

**Rejected.** Redacting at the API boundary, or in the backend before the LLM call.

**Why.** Redaction at a boundary protects **that** boundary. Tokenizing at the source means no
downstream component ever *holds* raw PII, so no downstream component can leak it — through a log, an
exception message, a checkpoint, a trajectory dump, or a future feature nobody has written yet. It
converts an ongoing discipline into a structural property.

**Consequence.** Stage ordering is security-critical. A test asserts it; reordering the pipeline
fails the build.

---

## ADR-006 — Approval as structure, not as prompt ✅

**Decision.** Irreversible verbs are gated by a LangGraph `interrupt()` bound to a specific payload.
There is no code path from a gated verb to `EmailSurface.act()` without a matching
`Decision(verdict="approve")`.

**Rejected.** A system-prompt rule ("always ask before sending").

**Why.** The model reads attacker-controlled text. Any guarantee expressed only in the prompt is
negotiable by that text. A guarantee expressed in graph topology is not. This is the single most
important design decision in the project — the one that makes it safe to point at a real mailbox.

**Consequence.** A decision authorizes one exact payload. Approving draft A does not authorize
mutated draft B, and a test proves it.

---

## ADR-007 — Two routing classes, not three ✅

**Decision.** `Route ∈ {linear, decision}`. No `compound` class in v1.

**Why.** Two classes capture the actual cost cliff: *does this need an LLM call per step, or not?*
Multi-worker plans are already expressible — the supervisor dispatches sequentially. A third class
would add a classifier failure mode without removing a constraint.

**Escape valve.** A linear worker that hits ambiguity returns `ESCALATE` and is re-dispatched on the
decision path. Misrouting is recoverable, so the classifier does not need to be perfect.

**Revisit when** a real task cannot be expressed as a sequence of dispatches.

---

## ADR-008 — Provider chain Groq → OpenRouter → Gemini ✅

**Decision.** `FallbackLLMClient` over an ordered chain, all OpenAI-compatible behind one port.

**Why this order.** Groq has the best free requests-per-minute and the lowest latency, which matters
disproportionately in a per-step agent loop where latency compounds. OpenRouter is a `base_url` swap
with a `:free` roster and no card. Gemini has a generous daily tier and suits the small `validator`
role.

**Rules.** Fallback happens **between** attempts, never inside a retry — a retry uses the same model
the user's config chose. Model slugs are configuration; **never hardcode a `:free` ID**, because that
roster rotates and a hardcoded slug is a time bomb.

> Supersedes the two-provider chain (Groq → Gemini) in
> [`SOLUTION-PRE-IN-POST.md §7`](SOLUTION-PRE-IN-POST.md). OpenRouter was inserted in the middle to
> widen the free-tier envelope.

---

## ADR-009 — Self-heal reads a curated registry; source-editing is deferred ⏸

**Decision.** v1 self-healing operates at the **task** level: classify the cause, consult a curated
`SkillRegistry`, offer four ranked options. It does **not** read or edit its own source in the live
loop.

**The requirement this partially defers.** The stated ask was "load skills, see codebase and try to
fix." The first half ships in v1. The second half is real and wanted — it is deferred, not dropped,
and here is the design.

**Why deferred.** The combination is what makes it dangerous, not either half alone: an agent that
(a) reads attacker-controlled email text, (b) writes code, and (c) runs that code in its own process
is the worst arrangement in this system. A single successful injection becomes arbitrary code
execution with the operator's mailbox already open. Blast radius is unbounded and there is no gate
that meaningfully contains it *inside the live loop*.

**v1.1 design — dev-assist mode**, which delivers the capability safely:

| Property | Requirement |
| --- | --- |
| Process | A **separate** process. Never the live agent loop. |
| Trigger | Human-initiated from a failed trajectory — never self-triggered. |
| Input | The trajectory, the typed error code, the diagnosis, and read-only source access. |
| Output | A **proposed diff plus a failing-then-passing test**. Nothing is applied. |
| Gate | Human review + full CI, exactly like any other contribution. |
| Isolation | No mailbox access, no provider keys beyond its own, no write access to the running deployment. |

This gives the real value — the system diagnoses its own defects and proposes fixes — without putting
a code-writing loop inside the process that reads hostile input.

**Revisit when** dev-assist has a track record and the sandbox story is independently proven.

---

## ADR-010 — Two `EmailSurface` implementations, extension is the product ✅

**Decision.** Ship both `PlaywrightEmailSurface` (server-side Chromium; dev, CI, demo — the default)
and `ExtensionEmailSurface` (the user's own Chrome via `chrome.debugger`). One port, zero graph
changes between them.

**Why both.** They serve different masters. The server surface is reproducible, CI-friendly, and
demoable by a stranger with a URL. The extension surface is the real product: the user's own profile,
own session, own IP — the configuration least likely to surprise Google and the only one that works
without handing over credentials.

**Why this is also the architecture test.** If swapping the surface requires more than one line in
the composition root, dependency inversion did not actually hold and the SOLID claims in this repo
are decoration. M6.7 exists to prove it.

**Account safety.** Dev and CI use recorded fixtures and, where a live account is unavoidable, a
dedicated one. Never a personal mailbox.

---

## ADR-011 — Next.js App Router for the cockpit ✅

**Decision.** The cockpit is Next.js (App Router), not React + Vite.

**Why.** User requirement, and it pays for itself: per-run URLs (`/run/[threadId]` is shareable and
directly reattachable), Server Components for the static shell and run history, and a one-push
deploy on Vercel.

**Design direction (confirmed).** Minimal monochrome — black, white, and grey tones only, one
consistent theme throughout. Rounded corners, sleek and modern, with smooth transitions; the
interface should recede so the agent's work is the only thing competing for attention. A live agent
cockpit is already dense with signal, so colour is reserved for meaning (pending approval, failure,
recommended option) and never used for decoration.

**Component strategy (confirmed).** Hand-rolled Tailwind for the cockpit's bespoke surfaces — the
two-pane layout, the transcript, the canvas viewport — where a component library would be fought
rather than used. `shadcn/ui` (copy-in Radix primitives) for the interaction-heavy, accessibility-
sensitive pieces: the approval card, question card, options card, and any dialog or popover. Copy-in
rather than a dependency means those primitives are ours to restyle into the monochrome theme.

**Cost accepted.** A live WebSocket cockpit is not Next.js's happy path. Mitigations are structural
and non-negotiable:
- **Exactly one `"use client"` root** (`CockpitClient`); everything live hangs beneath it.
- **The socket connects directly to the backend host** via `NEXT_PUBLIC_WS_URL` — Vercel's serverless
  functions cannot hold long-lived WebSockets, so no route-handler proxy is attempted.
- **Frames render to a `<canvas>`**, off the React reconciliation path.
- **The frontend holds exactly one env var.** A second one is a review-blocking finding.

> Supersedes the React + Vite cockpit specified in the original `CLAUDE.md` §19.

---

## ADR-012 — The run outlives the socket ✅

**Decision.** A process-level run registry keyed by `thread_id`. A cockpit disconnect **detaches the
view**; the run, its browser, and any pending interrupt continue. Reattaching replays the buffered
events and goes live.

**Rejected.** Tying run lifetime to the WebSocket connection.

**Why.** Runs are minutes long and contain human-in-the-loop pauses that may last longer than a
browser tab. A refresh killing the run — and the browser — makes the product unusable and makes the
durable-interrupt design pointless. A run is a server-side object; a socket is just a view onto it.

**Consequence.** Replay must be the **same code path** as live rendering, or the two drift and
reattach becomes a source of bugs. The append-only event store in the cockpit exists for this reason.

---

## ADR-013 — No human-in-the-loop failure fallback ✅

**Decision.** HITL is used for exactly three purposes: **AskUser** (missing context), **approval**
(irreversible action), **options** (self-heal). It is never a way to avoid typing a failure.

**Why.** "Ask the human when confused" is how an agent stops having a measurable reliability number.
Every failure landing on an `ErrorCode` is what makes "100% typed termination" a benchmark metric
rather than an aspiration — and a typed failure is what lets `diagnose` produce a *useful* question
instead of a shrug.

**Consequence.** Self-heal asks a *specific* question grounded in a classified cause, with ranked
remedies. That is strictly more useful than "I'm stuck, what now?"
