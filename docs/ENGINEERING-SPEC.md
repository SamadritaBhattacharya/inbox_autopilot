# Engineering Spec — rules, goals, and definition of done

- **Audience:** anyone (human or agent) writing code in this repo.
- **Authority:** this doc plus [`../CLAUDE.md`](../CLAUDE.md). A PR that violates a ❌ rule is rejected
  regardless of whether it works.

---

## 1. Engineering goals

Ranked. When two conflict, the higher one wins.

| # | Goal | Concretely |
| --- | --- | --- |
| 1 | **Nothing irreversible happens without a human** | No code path reaches a send/delete/invite without a recorded approval decision. Proven by test, not by inspection. |
| 2 | **The model never sees raw PII** | Tokenization happens in the funnel before serialization. Proven by leak tests over every egress. |
| 3 | **Every failure is typed** | 100% of terminal states carry an `ErrorCode`. "It just stopped" is a P0 bug. |
| 4 | **Every step is observable** | If it isn't in the event stream and the trajectory, it didn't happen. |
| 5 | **Extension without modification** | New worker / funnel stage / action / remediation / provider = a new class. The supervisor, pipeline, dispatcher, and composer do not change. |
| 6 | **The cheapest correct path first** | Deterministic before probabilistic. Small model before large. Zero calls before one call. |
| 7 | **Tests are the design pressure** | Red → green → refactor. Untestable code is a design defect, not a testing problem. |

## 2. SOLID — enforced, not aspirational

The LangGraph nodes and the composition root are the **only** places that know concrete types.

### S — Single Responsibility
One job per module. Each funnel stage, each action handler, each worker, each remediation strategy,
each provider adapter is one class with one transform.
**Trip-wire:** a file past ~300 lines, or one you cannot name the single job of, gets split.

### O — Open/Closed
Extension points and what they cost:

| To add… | Write | Touch |
| --- | --- | --- |
| a funnel stage | one `Stage` class | the pipeline's stage list (one line) |
| an action verb | one handler + one tool spec | the dispatcher registry (one line) |
| a worker | one `Worker` class | the worker registry (one line) |
| a remediation | one `RemediationStrategy` | the `SkillRegistry` list (one line) |
| an LLM provider | one adapter | the fallback chain in config (one line) |
| an email surface | one `EmailSurface` impl | the composition root (one line) |

If adding one of these requires editing the supervisor, the pipeline, or a node body, the abstraction
is wrong — fix the abstraction, not the caller.

### L — Liskov
Every `EmailSurface`, `Worker`, `LLMClient`, `Stage`, and `RemediationStrategy` implementation is
drop-in. Tests use fakes that satisfy the same contract, and the graph cannot tell the difference.
`FakeEmailSurface` and `FakeLLMClient` are **test doubles only** — they never appear on a real path.

### I — Interface Segregation
Ports stay narrow. `EmailSurface` is `observe()` + `act()` + `preview()` + `approve()` — the four things only an executor can do. `PiiVault` is `tokenize()` + `resolve()`.
No consumer depends on a method it does not call. A port that grows a method used by one caller is a
sign that caller wants its own port.

### D — Dependency Inversion
The graph depends on abstractions. Concretes are constructed in **one composition root**
(`config/container.py`) and injected. Nodes are closures over injected ports.

```python
# ✅ the shape every node takes
def build_reason_node(llm: LLMClient, emitter: EventEmitter, max_steps: int):
    async def reason(state: AgentState) -> dict:
        ...                       # returns a STATE DELTA, never mutates state
        return {"messages": [ai], "step": state.step + 1}
    return reason
```

Use a closure, not `functools.partial` — LangGraph can mis-detect a partial of a coroutine as a sync
node and fail to await it. (This is a real, previously-hit bug, not a style preference.)

## 3. Guardrails — the ❌ list

Violating any of these fails review.

**Architecture**
- ❌ No Electron or desktop dependency. This is a web app.
- ❌ No business or agent logic in `api/`. It adapts HTTP/WS to services and nothing more.
- ❌ No LLM, CDP, or DB call inside a graph node. Go through a port.
- ❌ No mutable agent state outside `AgentState`. No module globals, no service-held run state.
- ❌ No `while` loop orchestrating the agent. Orchestration is the `StateGraph`.

**Security**
- ❌ No raw DOM over the wire. Only funnel output.
- ❌ No raw PII over the wire, into logs, into trajectories, or into error reasons.
- ❌ No provider key outside the backend — not in `frontend/`, not in `bridge-extension/`.
- ❌ No `Send`, delete-forever, invite dispatch, or bulk-irreversible action without an approval
  interrupt.
- ❌ No auto-send from a rule unless that rule is explicitly whitelisted. Off by default, and the
  default cannot be flipped by config alone.
- ❌ No self-source editing in the live loop. Self-heal reads a curated `SkillRegistry`.

**Correctness**
- ❌ No reuse of previous-turn element indices. Re-observe and renumber every turn.
- ❌ No silent truncation of the observation. Always report dropped/hidden counts.
- ❌ No model re-routing inside a retry. Fallback is between attempts.
- ❌ No hardcoded model ID. Slugs are configuration.
- ❌ No task start before `context_gate` clears.
- ❌ No human-in-the-loop used as a failure fallback. HITL is for context, approval, and options.
- ❌ No hand-edited generated file (`packages/contracts/src/generated/**`, `schema/**`).

**✅ Always:** every failure ends in a typed `ErrorCode`; every action has a timeout wall; the stuck
and repetition guards are live on every decision worker.

## 4. Code conventions

| Topic | Rule |
| --- | --- |
| Typing | Full annotations on every public function. `from __future__ import annotations` at the top of each module. `X \| None`, not `Optional[X]`. |
| Async | Async throughout the backend. No blocking I/O in an async path — no `requests`, no sync file writes on the hot path. |
| Models | Pydantic v2 for anything crossing a boundary. Plain dataclasses are fine for internal value objects. |
| Wire casing | camelCase on the wire (`protocolVersion`, `screenshotRef`, `droppedCount`); snake_case in Python with Pydantic aliases; schema emitted `by_alias=True`. |
| Naming | Nodes are verbs (`observe`, `diagnose`). Ports are nouns ending in a role (`EmailSurface`, `SkillRegistry`). Builders are `build_*`. Fakes are `Fake*`. |
| Errors | Never `except Exception: pass`. Either handle it, or map it to an `ErrorCode` and emit it. |
| Logging | Structured, and passed through the PII redaction filter. A log line is an egress point. |
| Comments | Explain **why**, not what. A comment that restates the code is noise; a comment that records a non-obvious constraint (a real bug you hit, a provider quirk) is the most valuable line in the file. |
| Prompts | Live in `app/prompts/*.txt`, loaded by name via `load_prompt()`. **Never** inline a multi-line prompt string in Python. Plain text, not a template engine: none of them interpolate — state reaches the model as separate messages, which is what keeps the system prefix byte-stable and prompt caching working. |
| Imports | No `import langchain` outside `llm/`. No `import playwright` outside `surface/`. Enforced by a lint rule. |

## 5. Test strategy

Full detail in [`TESTING-AND-EVAL.md`](TESTING-AND-EVAL.md). The rules that bind code review:

| Layer | Must be tested with | Must NOT need |
| --- | --- | --- |
| Funnel stage | synthetic snapshot fixtures, one stage in isolation | a browser |
| Action handler | a fake surface, asserting the emitted `ActionCall` | a browser |
| Routing function | direct state construction, **every branch** | anything |
| Graph path | `FakeEmailSurface` + `FakeLLMClient`, scripted observation + canned tool call | a browser or an LLM |
| Worker | fakes, asserting the routed path and emitted `StepRecord`s | a browser or an LLM |
| Remediation strategy | a constructed `Cause`, asserting scoring and the produced `Option` | anything |
| Integration | `PlaywrightEmailSurface` against **local fixture HTML**, marked slow | a live Gmail account in CI |

**Every guardrail in §3 that can be tested, is tested.** Specifically these are required and named:

- `test_no_send_without_approval` — every send path with approval mocked absent must fail closed.
- `test_no_raw_pii_egress` — a fixture inbox with known addresses; assert zero occurrences across the
  `Observation`, the LLM request body, the event stream, the trajectory, and captured logs.
- `test_context_gate_blocks_dispatch` — no worker runs while `missing_slots` is non-empty.
- `test_all_terminals_typed` — every terminal path in the graph yields an `ErrorCode` or `success`.
- `test_linear_route_zero_llm_calls` — a rule-matched task records zero LLM calls.
- `test_indices_not_reused` — indices differ across turns for a mutated page.

## 6. Definition of done

A change is done when **all** of these hold:

- [ ] The failing test was written first, and it now passes.
- [ ] `just test` is green (backend + contracts + JS).
- [ ] `just check` is clean — no contract drift.
- [ ] New boundary types were added to `packages/contracts` and **regenerated**, not hand-written on
      both sides.
- [ ] Every new failure mode maps to an `ErrorCode`.
- [ ] Every new action verb declares reversibility, a timeout, and whether it is gated.
- [ ] Every new egress point (log, event, store, request) passes through PII redaction.
- [ ] The composition root is the only place a new concrete type is constructed.
- [ ] No file in the diff exceeds ~300 lines without a stated reason.
- [ ] Docs updated if behaviour visible to a user or another engineer changed.

## 7. Review checklist

Reviewers work top to bottom and stop at the first ❌.

**Blocking**
1. Does any code path reach an irreversible action without an approval decision?
2. Can raw PII reach a log, an event, the trajectory, or an LLM request?
3. Is there an LLM / CDP / DB call inside a node body?
4. Is agent state held outside `AgentState`?
5. Is a provider key reachable from `frontend/` or `bridge-extension/`?
6. Is a generated file hand-edited?
7. Does a new failure path exit untyped?

**Design**
8. Did adding a thing require editing a dispatcher, pipeline, or supervisor? (→ wrong abstraction)
9. Are routing decisions pure and fully branch-covered?
10. Does the cheapest correct path run first?
11. Is a new port narrow, or is it a grab-bag?

**Craft**
12. Are prompts in Jinja2 files rather than inline strings?
13. Do comments record constraints rather than restate code?
14. Is anything silently truncated or swallowed?

## 8. Working agreements for agentic contributors

This repo is built partly by coding agents. These rules exist because they are the failure modes that
actually occur.

1. **Read `CLAUDE.md` before writing code.** It is the contract; this doc is how the contract is
   enforced.
2. **Write the spec, then the plan, then the code.** A dated design doc with an explicit decisions
   log, then a task-by-task plan with checkboxes and per-task interfaces, then the diff. Keep both
   under `docs/` (see [`IMPLEMENTATION-PLAN.md`](IMPLEMENTATION-PLAN.md)).
3. **One task, one commit, one green test run.** Do not batch five tasks into one diff.
4. **Never widen scope silently.** If a task turns out to need something not in the plan, say so and
   finish everything that does not depend on the answer.
5. **Never disable a guardrail to make a test pass.** If a guardrail is wrong, change the guardrail
   deliberately, in its own commit, with the reasoning written down in [`ADR.md`](ADR.md).
6. **Report faithfully.** If tests fail, say so with the output. If a step was skipped, say which.
