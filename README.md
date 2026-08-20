# Inbox Autopilot

An email agent that **operates your Gmail through a real browser**. Type a task in plain English —
*"send an email to Priya about the Friday demo"*, *"clear out the newsletters"*, *"reply to the ones
that actually need me"* — and watch it happen: the left pane is the conversation, the right pane is
the live browser screen.

Three properties define it:

- **It won't start half-informed.** If the task is missing something the action requires, it asks
  before touching the mailbox.
- **The model never sees your data.** Addresses, phones, and identifiers become stable tokens
  (`alice@corp.com → P17`) *before* anything leaves the machine holding the DOM. The executor
  resolves them only at dispatch.
- **Nothing irreversible happens without you.** Send, delete, and calendar invites pause for
  approval — enforced by graph topology, not by a line in a prompt.

> **Status: design complete, implementation not started.** The full documentation set is written; the
> code is not. Start at [`docs/README.md`](docs/README.md).

## How it works

```
Gmail DOM (100k+ tokens)
   │  observation funnel, executor-side: visibility → occlusion → collapse
   │  → PII-tokenize → Set-of-Marks index → reading order
   ▼
Observation (~1–3k tokens)   ·   numbered   ·   tokenized   ·   no coordinates, no raw DOM
   │
   ▼  the model picks a NUMBER and a TOKEN
manager graph (LangGraph)
   PRE   intake → context_gate ⟲ AskUser → router (linear | decision) → planner
   IN    dispatch → Triage · Compose · Calendar · Rules → approval_gate ⟲
   POST  verify → ok | diagnose → 4 ranked options ⟲ → retry
   │
   ▼  executor resolves index → geometry, token → address
trusted CDP input (isTrusted: true) in a real browser
```

## Documentation

| Doc | What it answers |
| --- | --- |
| [`CLAUDE.md`](CLAUDE.md) | **The build contract.** Read before writing code. |
| [`docs/PRD.md`](docs/PRD.md) | What we're building, for whom, and how we know it works |
| [`docs/SYSTEM-DESIGN.md`](docs/SYSTEM-DESIGN.md) | Graph topology, state, funnel, workers, failure layer |
| [`docs/TECH-STACK.md`](docs/TECH-STACK.md) | Every choice, why, and the $0 budget math |
| [`docs/ENGINEERING-SPEC.md`](docs/ENGINEERING-SPEC.md) | Rules, SOLID enforcement, definition of done |
| [`docs/SECURITY-MODEL.md`](docs/SECURITY-MODEL.md) | PII vault, prompt injection, approval as a control |
| [`docs/WS-PROTOCOL.md`](docs/WS-PROTOCOL.md) | Wire contracts and the cockpit event vocabulary |
| [`docs/IMPLEMENTATION-PLAN.md`](docs/IMPLEMENTATION-PLAN.md) | M0–M7 with tasks and acceptance criteria |
| [`docs/TESTING-AND-EVAL.md`](docs/TESTING-AND-EVAL.md) | TDD strategy, fakes, fixtures, benchmark |
| [`docs/ADR.md`](docs/ADR.md) | Decisions, rejections, and reasoning |

## Stack

Python 3.12 · FastAPI · **LangGraph** · Pydantic v2 · Playwright + raw CDP · TypeScript MV3 extension ·
**Next.js (App Router)** + Tailwind + Zod · WebSocket · **Groq → OpenRouter → Gemini** (free tiers) ·
**Cost target: $0**.
