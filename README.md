# Inbox Autopilot

**An agent that operates Gmail through a real browser** — reads the backlog, drafts replies,
files what's noise, and stops for a human before anything irreversible.

You type a task in plain English. The left pane is the conversation; the right pane is the live
browser. Nothing sends without your approval.

```
"send Priya an email about the Friday demo"
"clear out the newsletters"
"reply to the ones that actually need me"
```

<!-- TODO: replace with a 30-second recording of a real run -->

---

## The actual problem

Gmail's DOM is over **100,000 tokens**. You cannot send that to a model — not for cost, not for
latency, and not for accuracy, because the task drowns in markup.

So the interesting part of this project isn't the prompting. It's everything built to make a
hostile, constantly-moving, privacy-sensitive UI legible to a language model **without handing it
the page or the data**.

```
Gmail DOM  (100k+ tokens, thousands of nodes)
   │
   │   observation funnel — runs next to the browser, seven stages, order enforced at import:
   │   extract → visibility → occlusion → wrapper_collapse → pii_tokenize → som → reading_order
   ▼
Observation  (~1–3k tokens)   numbered · tokenized · no coordinates · no raw DOM · no raw PII
   │
   │   the model replies with a NUMBER and a TOKEN — never a selector, never an address
   ▼
manager graph  (LangGraph)
   PRE    intake → context_gate ⟲ AskUser → router (linear | decision) → planner
   IN     dispatch → Triage · Compose · Calendar · Rules → approval_gate ⟲
   POST   verify → ok  |  diagnose → 4 ranked options ⟲ → retry
   │
   │   the executor resolves index → geometry and token → address, at dispatch and nowhere else
   ▼
trusted CDP input  (isTrusted: true)  in a real, signed-in browser
```

---

## Five decisions worth reading the code for

### 1. The funnel throws away 99% of the page — and says what it dropped

Seven single-responsibility stages, each independently tested. Hidden nodes go, nodes physically
covered by an open dialog go, layout wrappers collapse to their meaningful leaf. What survives gets
an integer.

The observation **never truncates silently**: it reports `droppedCount` and a direction hint,
because an agent that believes it has seen everything concludes the button doesn't exist and gives
up. When a compose window is open, the inbox behind it is culled — a **93% cut** on that view alone.

→ [`backend/app/observation/funnel/`](backend/app/observation/funnel/)

### 2. Redaction and authorization are different problems

Every address, phone and identifier becomes a stable token before it leaves the machine holding the
DOM: `alice@corp.com → P17`. The model reasons in tokens; the executor resolves them at dispatch.
`pii_tokenize` runs *before* indexing, and `STAGE_ORDER` asserts that at import — a reordering that
would leak PII downstream fails to load, rather than failing in production.

The half most designs miss: **knowing an address is not permission to write to it.** A token minted
from a *message body* is content a stranger controls. It resolves, and it is refused as a recipient
(`UNTRUSTED_RECIPIENT`). Only a contact, a sender, or your own instruction produces a usable target.
A hostile email cannot turn itself into a send.

→ [`backend/app/security/`](backend/app/security/) · [`docs/SECURITY-MODEL.md`](docs/SECURITY-MODEL.md)

### 3. Consent binds to the words, not to the button

An approval fingerprint that covers `Send(index=108)` authorizes *that button for the rest of the
run* — edit the body afterwards, click Send again, and it matches consent given for different text.

So the fingerprint includes a hash of the exact draft the human read, recomputed from the live
compose fields at dispatch. Change one full stop and the approval no longer matches, and the gate
asks again. The pause is a checkpointed LangGraph `interrupt`, not a blocked coroutine — close the
tab, come back in ten minutes, the draft is still waiting.

And the gate triggers on **consequence, not on name**: a `Click` on Gmail's Send button sends mail
just as surely as the `Send` verb, so the check asks what the index *points at*.

→ [`backend/app/workers/approval_gate.py`](backend/app/workers/approval_gate.py)

### 4. Every terminal state carries a typed code

Twelve of them — `STUCK`, `MAX_STEPS`, `APPROVAL_TIMEOUT`, `CONTEXT_INCOMPLETE`, `SEND_UNVERIFIED`,
`NOT_SIGNED_IN`… "it just stopped" is a P0 bug, not a mystery for later: an untyped exit can't be
counted, diagnosed, or turned into a ranked remedy.

There is exactly **one** exit node, because "every failure is typed" is only true if there's a single
place it can happen. The benchmark measures the rate and the target is 100%.

Around it: a repetition guard keyed on `(verb, target, args)`, a page-signature stuck detector,
runaway-output clipping, a think-before-act check, and a step-budget warning that injects
*"call Complete() now with what you have"*.

→ [`backend/app/telemetry/records.py`](backend/app/telemetry/records.py) · [`backend/app/agent/guards.py`](backend/app/agent/guards.py)

### 5. Actions verify their own postconditions

The failure mode that cost the most runs wasn't bad reasoning — it was **actions reporting success
they hadn't earned**.

`Scroll` returned "scrolled" unconditionally; inside a dialog there's nothing to scroll, so the agent
was told six times it was making progress. It now compares scroll position across the window *and*
inner containers, and returns `SCROLL_NO_EFFECT`. Typing a recipient reported failure because Gmail
had turned the address into a chip and emptied the input, so the agent typed it twice. `Type` now
verifies against the same chip-aware read the duplicate guard uses.

A verb that lies is worse than a verb that fails, because the loop has nothing to correct against.

→ [`backend/app/surface/playwright_surface.py`](backend/app/surface/playwright_surface.py)

---

## What's built

| | Status |
| --- | --- |
| Observation funnel, 7 stages | Done · unit-tested per stage · golden-file conformance across two implementations |
| Manager graph — PRE / IN / POST | Done · routing is pure functions, tested by path |
| Triage · Compose · Calendar · Rules workers | Done |
| PII vault, tokenizer, trust boundaries | Done · stage order asserted at import |
| Approval gate + editable draft + diff view | Done · driven against real Gmail |
| Typed failure layer + recovery with ranked options | Done |
| LLM gateway — Groq → OpenRouter → Gemini | Done · fallback exercised in production runs |
| Cockpit (Next.js, two-pane, live screencast) | Done |
| `PlaywrightEmailSurface` (server-side Chrome via CDP) | Done · the default |
| `ExtensionEmailSurface` (user's own Chrome, MV3) | Built · not yet driven against live Gmail |
| Public demo deployment | Not built — see [Why there's no live demo](#why-theres-no-live-demo) |

---

## Testing

| | |
| --- | --- |
| Backend tests | **1,110** fast + **135** against real Chrome |
| Frontend tests | **24** |
| Application code | 15,073 lines |
| Test code | **16,246 lines** |
| Benchmark | 16 golden tasks · **16/16** · 100% typed termination · **0 approval-gate bypasses** |

There is more test code than application code, and that ratio is deliberate. Three things it buys
that assertions alone don't:

- **The graph is tested on fakes.** A scripted `LLMClient` and `EmailSurface` mean every routing
  path, guard and interrupt is exercised with no browser and no provider. An unscripted LLM call
  fails loudly rather than returning a plausible empty result.
- **Browser tests call the real methods against real Chrome** over synthetic DOM that reproduces
  Gmail's structure — chips, hidden legacy inputs, dialogs. A Python reimplementation of the selector
  logic would only prove two copies of the same reasoning agree.
- **Cross-layer invariants are tests, not conventions.** Every verb a worker offers must be one the
  executor accepts; every port method must be forwarded by every adapter. Both caught real bugs that
  had shipped, and both are checked by set difference over data that already exists.

```bash
just test            # fast suite: backend + contracts + frontend
just test-browser    # the real surface methods, against real Chrome
just eval            # golden-task table; exits 1 on a failure or a bypassed gate
just verify          # the full local gate — contracts, guards, lint, tests
```

---

## Running it

```bash
# 1 — install (uv fetches Python 3.12; also builds the generated contracts)
just setup

# 2 — configure backend/.env — one free provider key is enough to start
#     GROQ_API_KEY=...
#     EMAIL_SURFACE=playwright
#     CDP_ENDPOINT=http://127.0.0.1:9222

# 3 — sign in once, by hand
python scripts/chrome.py     # opens a dedicated profile; log into Gmail like a person

# 4 — run
just dev-backend             # :8000
just dev-frontend            # :3000
```

**There is no password anywhere in this system.** Google refuses its sign-in flow inside an
automation-controlled browser — by design — so the agent doesn't authenticate. You sign into a
dedicated Chrome profile once, and the agent attaches to that already-authenticated session over
CDP. It inherits a session; it never holds a credential.

Full setup, including the extension surface: [`docs/RUNNING.md`](docs/RUNNING.md)

---

## Why there's no live demo

An agent that operates *your* mailbox needs a browser already signed into *your* Gmail. Hosting that
for strangers would mean asking visitors to type their Google password into a browser I control —
which contradicts the entire premise of a project built around not trusting the agent with raw data.

The Gmail REST API isn't the escape hatch either: its scopes are restricted, requiring Google
verification plus an annually-recurring third-party CASA security assessment. And it would delete
the interesting half of this codebase — the funnel, the SoM indexing, the trusted-input dispatcher —
leaving an API wrapper.

The honest answer is a recording of a real run, and a replay mode that streams a captured event log
through the real cockpit. Both are in progress.

---

## Documentation

`docs/` is 5,880 lines and is where the reasoning lives — including the decisions that were
*rejected* and why.

| Doc | What it answers |
| --- | --- |
| [`CLAUDE.md`](CLAUDE.md) | The build contract — architecture rules and guardrails |
| [`docs/SYSTEM-DESIGN.md`](docs/SYSTEM-DESIGN.md) | Graph topology, state, funnel, workers, failure layer |
| [`docs/SECURITY-MODEL.md`](docs/SECURITY-MODEL.md) | PII vault, prompt injection, approval as a control |
| [`docs/ADR.md`](docs/ADR.md) | Every decision, what it superseded, and why |
| [`docs/TESTING-AND-EVAL.md`](docs/TESTING-AND-EVAL.md) | TDD strategy, fakes, fixtures, benchmark |
| [`docs/ENGINEERING-SPEC.md`](docs/ENGINEERING-SPEC.md) | SOLID enforcement, definition of done |
| [`docs/WS-PROTOCOL.md`](docs/WS-PROTOCOL.md) | Wire contracts and the cockpit event vocabulary |
| [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) | What is measured, and the current numbers |
| [`docs/TECH-STACK.md`](docs/TECH-STACK.md) | Every choice, and the $0 budget math |

---

## Architecture notes

**Ports and adapters throughout.** The graph depends on protocols — `EmailSurface`, `LLMClient`,
`PiiVault`, `RulesStore`, `Approver`, `EventSink`, `TrajectoryStore` — and concretes are built in a
single composition root. Two surfaces implement the same port with zero graph changes: server-side
Playwright, and the user's own Chrome via an MV3 extension.

**Contracts are generated, not duplicated.** `Observation`, `ActionCall` and `ActionResult` are
authored once in Pydantic and generated into Zod for the TypeScript side. CI fails on drift.

**Free tiers are a design constraint, not an afterthought.** The binding limit is requests per
minute, not dollars — so: a three-provider fallback chain, a small model for classification and a
large one only for reasoning, a byte-stable prompt prefix for caching, history compaction at a token
budget, and a rule that a repeated question never costs a second call.

## Stack

Python 3.12 · FastAPI · LangGraph · Pydantic v2 · Playwright + raw CDP · TypeScript MV3 extension ·
Next.js (App Router) + Tailwind + Zod · WebSocket · Groq → OpenRouter → Gemini
