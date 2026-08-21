# Inbox Autopilot — documentation index

> A browser-driven email agent. It operates the user's Gmail through a real browser, reasons in the
> cloud over a **manager/supervisor LangGraph**, never sees raw PII, and never sends anything without
> human approval.

Read in this order.

| # | Doc | What it answers |
| --- | --- | --- |
| 0 | [`../CLAUDE.md`](../CLAUDE.md) | **The contract.** How to build and work in this repo. Read before writing code. |
| — | [`RUNNING.md`](RUNNING.md) | **Start here to use it.** Pointing it at your real Gmail, and why you cannot sign in inside the agent's browser. |
| 1 | [`PRD.md`](PRD.md) | What we are building, for whom, and how we know it works. |
| 2 | [`SYSTEM-DESIGN.md`](SYSTEM-DESIGN.md) | The AI system design: graph topology, state, nodes, workers, funnel, failure layer. |
| 3 | [`TECH-STACK.md`](TECH-STACK.md) | Every technology, why it was chosen, and the $0 budget math. |
| 4 | [`ENGINEERING-SPEC.md`](ENGINEERING-SPEC.md) | Engineering rules, goals, SOLID enforcement, definition of done, review checklist. |
| 5 | [`SECURITY-MODEL.md`](SECURITY-MODEL.md) | PII vault, threat model, prompt injection from hostile email bodies, approval gates. |
| 6 | [`WS-PROTOCOL.md`](WS-PROTOCOL.md) | The wire: `Observation` / `ActionCall` contracts + the cockpit event vocabulary. |
| 7 | [`IMPLEMENTATION-PLAN.md`](IMPLEMENTATION-PLAN.md) | Phased milestones with tasks, interfaces, and acceptance criteria. |
| 8 | [`TESTING-AND-EVAL.md`](TESTING-AND-EVAL.md) | TDD strategy, fakes, fixtures, the benchmark harness and its metrics. |
| 9 | [`ADR.md`](ADR.md) | Decision records — what we chose, what we rejected, and why. |
| — | [`SOLUTION-PRE-IN-POST.md`](SOLUTION-PRE-IN-POST.md) | The original Pre/In/Post solution narrative that seeded this design. |

## The one-paragraph version

Gmail's DOM is 100k+ tokens; the LLM never sees it. An **observation funnel** running next to the
browser prunes the page to ~1–3k tokens, tokenizes every email address / phone / personal name into
stable per-session tokens, and numbers each interactable element `[N]`. The model picks a **number**
and a **token** — never a coordinate, never a real address. Above that worker loop sits a
**manager graph**: it refuses to start until it has 100% context, routes linear work away from the
LLM entirely, dispatches typed workers, interrupts for human approval before anything irreversible,
verifies the outcome, and on failure diagnoses the root cause and offers four ranked options with a
free-form escape hatch.

## Doc conventions

- **Normative language.** MUST / MUST NOT / SHOULD carry their RFC 2119 meaning. A ❌ bullet is a
  hard guardrail; violating one fails review.
- **Contracts are code.** Any schema shown here is illustrative; the source of truth is
  `packages/contracts/src_py/`. If a doc and the code disagree, the code is a bug **or** the doc is
  — resolve it, don't route around it.
- **Every doc states its scope.** If something is out of v1, it says so explicitly rather than
  leaving it ambiguous.
