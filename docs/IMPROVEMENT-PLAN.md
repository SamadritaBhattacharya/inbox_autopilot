# Improvement plan

How the gaps in [IMPROVEMENTS.md](IMPROVEMENTS.md) get closed, in the order that makes each
step verifiable by the one before it.

Two programmes run here. **Part A** is operational — what makes the working system survivable
and deployable. **Part B** is the agent-intelligence programme: flexibility, token cost,
procedural memory, self-improvement. Part A is days; Part B is weeks, and its running order is
not negotiable, because step 0 is what makes every later claim checkable.

---

## The principle that orders everything

Read `backend/app/prompts/worker.txt` as an archaeologist rather than an author. Most of its
lines are fossils of past defects:

- "ONE compose window per email…"
- "Typing a recipient COMMITS it for you…"
- "A FILLED field is done — typing into it again adds a SECOND copy"
- "never ask the operator to supply a token…"

That is roughly 900 tokens of **prompt scar tissue** — English patches for defects that belong
in the action layer.

> **Every rule in a system prompt is a runtime tax paid on every turn, enforced
> probabilistically. Every rule in code is paid once, enforced deterministically.**

A prompt line saying "don't do X twice" is strictly worse than a dispatcher that refuses X the
second time. It also degrades compliance with the other forty rules — instruction dilution is
real, so adding a rule to fix a bug is statistically breaking something else.

The work below is largely one move repeated: **migrate scar tissue from the prompt into typed
code.** Each migration cuts tokens, cuts hallucination, and creates the seam where procedural
memory later plugs in. Target: `worker.txt` under 300 tokens.

---

# Part A — operational

## A1. Commit everything · minutes · do this first

39 modified files and 3 new ones exist on one disk with no history.

Suggested sequence, smallest reviewable units first:

1. `feat(auth)` — `app/auth/`, `api/auth_routes.py`, `SignInGate`, `PairBrowser`
2. `feat(bridge)` — `bridge-extension/`, `api/bridge_ws.py`, `surface/bridge.py`,
   `surface/extension_surface.py`
3. `fix(safety)` — consequence-based gating, content-bound approval fingerprint, password
   redaction, task-text tokenization
4. `fix(perception)` — focus-box priority, filled-field flags, tab reuse, bulk insert
5. `fix(llm)` — tool-call ids, per-provider rosters, cooldown, provider events
6. `test(conformance)` — fixtures and the golden generator
7. `docs` — this file and its companion

**Acceptance:** `git status` clean; every suite green on a fresh clone.

## A2. Prove the fixes on the Playwright surface · minutes

One compose task, watched end to end: draft written before the browser opens, one compose
window, recipient committed as a chip, subject and body filled once each, approval card showing
the real text, send only after approval.

**Acceptance:** a run whose `StepRecord` trail shows no repeated `Type` on a filled field and no
second `Compose` click.

## A3. Delete the stale root `.env` · seconds

Settings are anchored to `backend/.env` now, but the stale file will confuse the next person who
runs uvicorn from the repo root — which is what happened for most of a day.

## A4. Drive real Gmail through the extension · hours

The only untested layer, and the only deployable one. Sequence: load unpacked → pair with a code
→ observe only, no mutations → compare the observation against what Playwright produces for the
same mailbox → then one archive → then one compose to yourself.

**Watch for:** `chrome.debugger` banner interactions, MV3 suspension mid-run, Gmail's iframe
boundaries. Expect at least one surprise; that is the point of doing it.

**Acceptance:** a compose task completes through the bridge, and the funnel output matches the
Playwright funnel on the same page within the conformance tolerance.

## A5. Per-user rate limiting and budgets · ~1 day

Today every user spends the owner's free-tier allowance, and one person exhausted Groq's daily
cap twice in an afternoon.

Three independent caps, because they fail differently:

| Cap | Scope | On breach |
| --- | --- | --- |
| Requests per minute | per user | queue, then refuse with a typed code |
| Tokens per day | per user | refuse new runs, finish the running one |
| Concurrent runs | per user | refuse the second |

Meter from `StepRecord`, which already carries tokens by role and provider — no new measurement,
only enforcement. Surface breaches through the existing `provider` event, which already renders
in the cockpit.

**Acceptance:** a test user hitting the daily cap gets a clear cockpit message, and other users
are unaffected.

## A6. Make `AUTH_MODE=off` refuse a public interface · ~2 hours

A startup banner is a mitigation, not a control. `off` should bind loopback only and refuse to
start when bound to `0.0.0.0`, naming `AUTH_MODE=google` as the fix.

**Acceptance:** a test asserting startup fails on a non-loopback bind with auth off.

## A7. Screencast through `chrome.debugger` · ~half a day

`Page.startScreencast` → frames over the bridge → the existing `screenshot` event. The cockpit
already renders these on the Playwright surface, so this is transport only.

If it proves unreliable, the honest alternative is to say so in the pane — "you are watching your
own Gmail tab" — rather than leave it blank and looking broken.

## A8. Widen the conformance fixtures · ~half a day

Six cases for two funnel implementations is thin, and one real divergence has already slipped
through. Add: threaded conversation view, search results, label sidebar, compose with a chip
already committed, and an injection case with an address in a subject line.

## A9. CI hygiene · ~2 hours

`pnpm run check` exits 134 despite every step passing, which makes the drift guard useless as
written. Chase the flaky `test_ask_user.py::test_the_answer_comes_back_to_the_model` at the same
time — one unexplained failure is one too many.

---

# Part B — the agent-intelligence programme

## B0. The eval harness · the gate for everything below · ~2 days · DONE

**Build this first. Not in parallel, not after.**

`CLAUDE.md` §20 promises a benchmark harness. It does not exist. Without it, every claim below —
"fewer tokens", "smarter", "self-improving" — is unfalsifiable, and self-improvement without an
eval gate is not improvement, it is drift.

**What it is:** ~20 golden tasks over fixture mailboxes, run against fakes for `LLMClient` where
behaviour should be deterministic, and against the Playwright surface where it should not.

**What it reports, per task and in aggregate:**

| Metric | Why |
| --- | --- |
| Success rate | the headline |
| Steps to completion | the superlinear cost driver |
| Tokens by role and provider | the bill |
| % terminated with a typed code | target 100%; an untyped exit is an unknown failure mode |
| Invalid-referent rejections | the hallucination rate, once B1 lands |
| Approval gates hit vs bypassed | must be 100% / 0%, permanently |

**Promotion criterion, written down now:** no macro, learned locator, or prompt change ships
unless it beats baseline on success rate without regressing steps or tokens.

**Acceptance:** `make bench` prints a table, and today's numbers are recorded as the baseline in
`docs/BENCHMARKS.md`.

**Status:** built as `backend/tests/bench/` — 15 golden tasks, a metering adapter over the
`LLMClient` port, and 22 tests covering the harness itself (including a deliberately broken
gating predicate, to prove the bypass detector actually detects). Run with
`python -m tests.bench.run` from `backend/`. Baseline recorded in
[docs/BENCHMARKS.md](BENCHMARKS.md): 15/15, 100% typed termination, 0 gate bypasses.

Two pre-existing gaps surfaced along the way, left as findings rather than fixed here so the
baseline reflects the system as it actually is:

- The approval gate never sets `status: awaiting_human` (the context gate does), so the
  highest-stakes pause in the system reads as `running` to anything checking status.
- `workers/loop.py`'s act node hardcodes `error_code=None` on its `StepRecord` even though
  `ActionResult.error_code` is populated, so a dispatch rejection's typed reason never
  reaches the trajectory. `invalid_referents` is measured at the surface boundary instead.

Both are noted in BENCHMARKS.md and worth a follow-up fix before B1 widens referent checking.

## B1. Dispatcher as validator · ~1 day · DONE

Turn hallucination from an unbounded outcome into a counted, recoverable event.

Any referent the model produces that was not in **this turn's** observation — an index, a token,
a verb outside the bound set — is rejected *before* touching the browser, and returned as a typed
result the model reads verbatim. Literal addresses are already refused; extend the same treatment
to every referent.

Two payoffs: a wrong click becomes a one-turn correction, and you acquire a hallucination metric.
**You cannot fix a rate you do not measure.**

**Acceptance:** a fake LLM emitting `Click(index=999)` produces a typed rejection, no browser
call, and a counted event in the benchmark.

**Status:** the premise was wrong in one respect — `app/surface/dispatch.py`'s `ActionValidator`
already refused every referent this plan asked for: `STALE_INDEX` for an index outside this
turn's geometry, `UNKNOWN_TOKEN` for an unminted or literal address, `VERB_NOT_BOUND` for a verb
outside the worker's schema, `UNTRUSTED_RECIPIENT` for a token minted from page content rather than
a real correspondent, plus a `COMPOSE_ALREADY_OPEN` idempotency guard neither this plan nor
IMPROVEMENTS.md knew about. Every rejection already reached the model as a typed one-turn
correction. So "a counted event in the benchmark" was the actual gap, and it turned out to be a
real, well-hidden bug rather than missing plumbing:

`StepRecord.error_code` was typed as the run-termination `ErrorCode` enum (CLAUDE.md §11's
vocabulary). A dispatch-rejection code like `STALE_INDEX` is not a member of it, so assigning
`result.error_code` directly would have raised a `pydantic.ValidationError` on the first
hallucinated referent of any real run. Two call sites had independently discovered this the hard
way and silently worked around it: the `act` node (`workers/loop.py`) hardcoded `error_code=None`,
and the linear `rules` worker (`workers/rules_worker.py`) omitted the field outright. Both were the
same defect in different topologies — every dispatch rejection reached the model correctly and
then vanished from `TrajectoryStore` before anyone (a person replaying a run, or B6's offline
mining) could count it.

Fixed by widening `StepRecord.error_code` to `str | None` — safe, since every existing caller
already passes an `ErrorCode` member or `None`, and a `StrEnum` serialises identically either way
— then setting the real code at both call sites. Three new regression tests drive the full graph
and assert the code survives into `state["history"]`, including one that pins the exact crash this
avoids. The bench harness (B0) was itself relying on a boundary-measurement workaround for this
same reason; with the root cause fixed, `invalid_referents` now reads from the real trajectory, and
`test_the_trajectory_and_the_boundary_agree` guards against the bug class recurring silently.

No new validation logic was written — B2 and B3 build on a dispatcher that was already doing this
job; what it lacked was a way to remember that it had.

## B2. Conditional slots + planner fan-out · ~2 days · *the "A and B" ask* · DONE (with one deliberate deviation)

`slots.py` is good design — a table, not prompt text, with alternative groups and thresholds
priced by blast radius. Extend it in the same shape rather than teaching the worker anything.

**Add a second table: conditional requirements.**

```
(action, predicate over the intent, slot, options, recommended default)
```

For the case you raised: `send_email`, predicate `recipient_count > 1`, slot `delivery_mode ∈
{one_thread, separate}`, default `one_thread`. A new ambiguity later is a new row — the gate
never changes.

**Three rules that separate a good agent from an annoying one:**

1. **Propose, don't interrogate.** Never a bare question. Emit the recommended default *and* the
   alternative: "I'll put both on one email — or send separately?" Conditional slots join the
   existing batched question in `question_for`; they must not add a second round trip.
2. **Price the ask by reversibility.** Already done in spirit (`READ_ONLY_THRESHOLD = 0.5` vs
   `0.85`); make it explicit. Cheap to undo → take the default and announce it. Irreversible →
   ask. `delivery_mode` gates a Send, so it is ask-worthy. Tone is not.
3. **The ambiguity must never reach the worker.** This is the architectural point.
   `delivery_mode` is not a fact the ReAct loop reasons about — it changes the *shape of the
   plan*. `separate` → the planner fans out into N compose sub-tasks, each dispatched with a
   single `recipient_identity`, each with its own approval gate. `one_thread` → one dispatch with
   `P1, P2`. Either way the worker sees an unambiguous single job, and its prompt learns nothing
   new.

Teaching the worker "sometimes there are two recipients, decide how to handle it" would instead
be paid on every turn of every run forever — and still be got wrong on turn 7.

**Acceptance:** a fixture table of ~40 utterances → expected
`(action, missing_slots, conditional_asks, plan_shape)`, asserted with no LLM and no browser.

**Status:** built substantially as specified — `app/manager/slots.py` now has `CONDITIONAL_SLOTS`
alongside `REQUIRED_SLOTS`, in exactly the shape asked for: `ConditionalSlot(slot, predicate,
prompt, default, options)`, keyed by `Action`. `recipient_count()` counts vault tokens first (the
common case, since an operator-supplied address is already minted by the time the gate runs) and
falls back to a naive split for a name with no address yet. `outstanding_slots()` evaluates
conditional requirements only once every required group already clears — asking "one email or
separate?" before the gate even knows the topic would be backwards, and would cost a second round
trip for a task that needs both answers anyway — and `question_for` phrases the conditional ask
with its recommended default baked in ("I'll send one email to everyone unless you say
separately"), never as a bare noun phrase. `send_email` and `forward` both carry the rule. A
19-case fixture table plus targeted edge cases (53 tests) covers this in isolation, no LLM, no
browser — the plan's own acceptance criterion.

**Rule 3, "the worker sees an unambiguous single job," is honoured — by a lighter mechanism than
literal N-way graph dispatch.** The plan's wording ("the planner fans out into N compose
sub-tasks, each dispatched to ComposeWorker") describes real parallel/sequential subgraph
invocation — LangGraph's `Send` API or an equivalent. This graph has no fan-out concept anywhere
today (not even `CalendarWorker` or `TriageWorker` use it), and building one for this feature alone
would be a real architecture change with its own failure modes: nested checkpointing, approval
interrupts crossing a subgraph boundary, a new class of test to write. That risk was not
justified by this feature alone.

Instead, `context_gate` resolves `delivery_mode` exactly as planned, and `workers/rendering.py`'s
`task_block` turns the resolution into a concrete, numbered instruction the *existing* ReAct loop
carries out within one worker run — e.g. for three recipients, chosen separately:

```
This goes to 3 people SEPARATELY — 3 separate emails, never one email with more than
one of them in it. Send them one at a time, in this order:
  1. P1
  2. P2
  3. P3
Compose, fill, and Send ONE of these completely — including its own approval — before
opening the next. Reuse the same subject and body for each; only the recipient changes.
```

The property the plan actually cares about still holds: **the worker never decides**
together-vs-separate — that decision is made once, upstream, and handed down as an instruction
it only executes. And because `approval_fingerprint` is content-bound (§9, `app/surface/dispatch.py`),
each of the N sequential `Send` calls carries different resolved text and therefore needs its own
separate human approval — "each with its own approval gate" holds in substance, just not via N
separate graph dispatches. `test_multi_recipient_delivery.py` drives the real graph end to end and
proves the chain connects: two recipients → the gate asks exactly one extra, correctly batched
question → the human's free-text answer resolves it → the worker's own prompt states the decision,
never re-derives it.

If real per-recipient fan-out (independent retries, independent progress per email, one recipient's
failure not blocking another's) becomes worth its own risk later, `CONDITIONAL_SLOTS` and
`resolved_delivery_mode` do not change — only what `dispatch` does with `delivery_mode == "separate"`
would.

## B3. Region-of-interest funnel scoping · ~1 day · biggest single token win · DONE (funnel half; the tool-binding half didn't apply)

When `composeOpen`, the worker does not need 120 inbox rows. Scope the funnel to the compose
subtree.

Plausibly a **70% cut to the largest block in the prompt**, and it deletes an entire class of
failure — clicking something in the inbox while composing — by making those elements *absent*
rather than discouraged.

Pairs with **phase-scoped tool binding**: once compose is open, `Compose` leaves the bound set. A
tool schema costs 100–200 tokens and is a hallucination surface; removing an illegal verb is free
correctness. This is where "ONE compose window per email" stops being a sentence and becomes a
precondition.

**Status:** the funnel half was a real, well-hidden gap — the tool-binding half wasn't, because it
described a mechanism this codebase doesn't have.

*The funnel.* A focus box (`meta.focus_box`, set only when `composeOpen`) already existed and
already made compose fields win a *priority tie-break* against inbox rows in
`reading_order.py`/`readingOrder.ts` — that was the earlier fix for the subject-field-trimmed bug.
But priority alone only decides ORDER within one shared 2000-token budget, and a compose dialog's
half-dozen fields cost so little that most of that budget was still unspent. Measured against a
realistic 140-row inbox: **99 of 140 background rows were still in the observation while
composing** — the model could still see, and still reason about clicking, almost the entire
mailbox behind the dialog it had just opened. Priority won the tie-break; it never came CLOSE to
being tested, because the budget was never actually tight.

Fixed with a second, much smaller allowance (`OUTSIDE_FOCUS_BUDGET_FRACTION = 0.1`, i.e. 10% of
the main budget) reserved specifically for whatever is OUTSIDE the focus box, evaluated in the
same priority-sorted pass. With the same 140-row inbox: **10 of 140 rows survive (93% cut)**, and
all compose fields still fully survive regardless. Mirrored in both funnels — `reading_order.py`
and `readingOrder.ts` — with matching constants and matching tests (3 new cases each, plus a new
`region_scope` conformance fixture proving the two languages agree on a real end-to-end scenario,
not just their own isolated unit tests).

**Two pre-existing bugs surfaced building this, both fixed as prerequisites:**
- `backend/tests/observation/test_conformance.py`'s `run()` and `scripts/gen_funnel_goldens.py`'s
  `run_case()` never threaded a fixture's `focusBox` (or `toFilled`/`subjectFilled`/`bodyFilled`,
  in the test harness) into `PageMeta` at all. No fixture could ever have exercised focus-box
  behaviour through either path, regardless of what its `meta` claimed — `modal.json`'s own
  `focusBox: null` was consistent with a harness that would have ignored it either way. Fixed by
  passing all four through; confirmed as a true no-op against every existing golden (only the new
  `region_scope` fixture is new — `git diff` on the other six is empty).
- The two languages disagree on `focusBox`'s wire shape: `extract.py`'s real executor-side JS
  sends `{x,y,width,height}` (an object), but the TS conformance test casts a fixture's `meta`
  straight to `PageMeta`, whose `focusBox` field is a 4-tuple. A fixture written as an object
  parses fine on the Python side and throws `TypeError: box is not iterable` on the TS side —
  caught immediately by running both suites against the same new fixture, which is exactly what
  this conformance mechanism is for. Fixtures now store `focusBox` as a 4-element array;
  `test_conformance.py` and `gen_funnel_goldens.py` both do `tuple(focus)` before constructing
  `PageMeta`.

*The tool binding.* There is no `Compose` tool to remove from a bound set — opening compose is an
ordinary `Click(index=N)`, and `Click` is the generic pointer verb used for every clickable element
including the compose fields themselves; unbinding it would break composing, not just re-composing.
The protection this bullet was reaching for already existed, built before this plan, as
`COMPOSE_ALREADY_OPEN` in `app/surface/dispatch.py` — a dispatch-time refusal keyed on the specific
target (an element named like "Compose") and the current state (`mail.compose_open`), which is a
more precise lever than a blanket verb removal would have been anyway. Nothing needed building
here; B1's writeup already covers it.

One caution against overclaiming, corrected during review: the funnel fix does **not** generally
make the Compose button itself vanish from the observation while composing. A single button is
cheap enough to comfortably survive the outside-focus allowance alongside a few background rows —
confirmed in the `region_scope` fixture, where "Compose" is the first surviving element. The fix's
real effect is on background CONTENT (rows, threads, list items), not on making every background
control disappear; `COMPOSE_ALREADY_OPEN` remains the actual, and sufficient, defence against a
second click on Compose specifically.

## B4. Prompt diet · ~1 day · after B1–B3, never before · DONE

Only now delete the scar tissue, because B1–B3 have made each line redundant in code. Then verify
against B0: if the double-compose bug returns, a migration was incomplete.

Also confirm `intake`, `router`, and `verify` actually use the `classifier` role rather than
`executor`, and that the cacheable system prefix is contiguous and first — anything variable
inserted above it voids the cache.

**Acceptance:** `worker.txt` under 300 tokens with no regression in the B0 table.

**Status:** `worker.txt` cut from 898 tokens to 327 (a 64% reduction) — close to, not quite under,
the 300 target; the remainder didn't yield to further cutting without removing content nothing
else enforces (see below). Went rule by rule against what B1–B3 actually built, not by feel:

**Cut, because something else now says the same thing, dynamically, only when it's true:**
- *"ONE compose window per email… do not click Compose again"* — triply redundant. The dispatcher
  already refuses a second click with a typed correction (`COMPOSE_ALREADY_OPEN`, pre-existing,
  covered in B1's writeup), and `observation_block` already prints *"A COMPOSE WINDOW IS ALREADY
  OPEN… Do not click Compose again"* every turn a dialog is open. A static rule buried among a
  dozen others is a weaker signal than a live one attached to the state that made it true.
- *"The observation tells you which compose fields are FILLED… typing again adds a SECOND
  copy"* — byte-for-byte duplicated by `observation_block`'s own `To: FILLED · Subject: empty…`
  line, which only appears while it's relevant. Kept a one-clause pointer ("trust the FILLED/empty
  state") in the static prompt so the concept isn't introduced cold; the specifics live dynamically.
- *"Typing a recipient COMMITS it… you do not need a separate PressKey"* — this explained a
  MECHANISM (why a chip appears) rather than corrected a violation. Now that the FILLED flags tell
  the model the outcome directly, it doesn't need to understand the mechanism to act correctly.
- *"You will never see a real address, and a literal address in an action is rejected"* —
  `UNKNOWN_TOKEN` already returns a clear correction ("carries a literal address rather than a
  token…") the one time this might come up. Compressed to a clause rather than fully deleted, since
  a first-attempt mistake here still costs a turn even though it self-corrects.

**Kept, verified as still load-bearing — not cut on a hunch:**
- *"Never ask the operator for a token you were already given"* and *"Only use AskUser for
  something you cannot see or infer"* — no code stops a redundant `AskUser` call; nothing
  repetition-guards differently-worded questions. Still the only defence against this failure mode.
- *"Prefer `Send` over clicking a Send button… the verb describes itself on the approval card"* —
  checked, not assumed: `approval.py`'s `KIND_FOR_VERB` maps only `Send`/`SendInvite`/
  `DeleteForever` to a human-readable summary ("Send this email"); anything else defaults to `"Run
  {verb}"`. `Click(index=108)` genuinely produces a worse approval card ("Run Click") than `Send`
  does. This rule is true and nothing makes it redundant.
- *"Call exactly one tool per turn"* — kept after finding a real reason to: `loop.py`'s reason node
  takes `result.tool_calls[0]` unconditionally, and no request ever sets `parallel_tool_calls:
  false`. If a provider ever returned more than one call, the rest would be **silently dropped**
  with no signal to the model at all — worse than a typed correction, and not something to cut
  guidance for. Recorded as its own finding below; not fixed here, since fixing a silent-drop with
  no benchmark coverage of the failure is a different, separately-scoped task.
- *"Assessment: <did the last action work?>"* — kept short. Not enforced (that's B8's job), but
  `agent/assessment.py`'s `split_assessment` already parses it and `FeedbackKind.ASSESSMENT`
  already records it (B7's territory); dropping the instruction would silently stop that channel.

**Verified, not assumed:**
- `intake` and `router` already use `role="classifier"` — no drift to fix.
- `verify` makes **no LLM call at all** in v1 (`recovery_nodes.py`: "Deterministic only, and
  deliberately so… a rubric check costs a model call at exactly the moment the provider is most
  likely to be the thing that broke"). The plan's question doesn't apply to it.
- Every node's cacheable system message is genuinely first and alone at that position — confirmed
  by reading each node's `messages = [...]` construction, not by grepping for the word "cacheable".

**One finding this check surfaced that the plan didn't anticipate:** `Message.cacheable` is set to
`True` on every system message across all six nodes and is **never read anywhere** —
`grep -rn "\.cacheable\b" app/` returns nothing. CLAUDE.md §14 names prompt caching as a primary
free-tier mitigation ("stable prefix cache-marked"); the marking exists, the mechanism that would
act on it does not. Whether Groq/OpenRouter/Gemini's OpenAI-compatible endpoints need an explicit
`cache_control`-style parameter or already auto-cache long repeated prefixes server-side is a real
question this session didn't answer — worth its own scoped investigation before assuming either
way. Not fixed here: B4's job was cutting prompt bulk, and this is a wiring question, not a wording
one. Recorded as item 21 in IMPROVEMENTS.md.

## B5. Procedural memory · ~1 week · *the "remember where Send is" ask* · PARTIAL — the store is done; the live wiring is not, and cannot be from here

Do not build "a memory". There are three, with different lifetimes and trust levels:

| Kind | Lifetime | Where | Status |
| --- | --- | --- | --- |
| Working — this run's scratchpad | one run | `AgentState.agent_memory` | exists |
| Episodic — what happened | forever, offline | `TrajectoryStore` | exists |
| **Procedural — where Send is, how to compose** | across runs, online | nothing | **build this** |

**Never cache indices or coordinates.** Indices are per-turn by construction; coordinates die on
resize. What is stable is a **semantic descriptor** —
`(host, view signature, role, accessible-name pattern, container path)` — keyed by page
signature, never by URL.

**It lives in the executor, not the brain.** Only the executor sees the DOM, and keeping it there
preserves the surface-agnostic port: the brain stays ignorant, which is exactly what keeps
`PlaywrightEmailSurface` and `ExtensionEmailSurface` swappable.

**The flow:**

1. The brain emits a *semantic verb* — `Send`, not `Click(27)`.
2. The executor looks up the cached locator, **verifies it still matches** (role + name +
   visible), and dispatches trusted input.
3. On a miss: fall back to funnel + LLM, and on success write the locator back with provenance
   and a confidence score.

Combine with **macro actions** — `OpenCompose`, `FillRecipient`, `FillSubject`, `FillBody` as one
executor-side procedure with a postcondition check. Compose is ~5 LLM round trips today; this
makes it 1. The token problem and the latency problem, solved by the same mechanism.

**Five rules that keep a cache from becoming a confident liar:**

1. **A cached locator is a hypothesis, never a fact.** Verify before every use. One DOM query is
   free; a wrong trusted click on Send is unbounded.
2. **Provenance is typed.** Curated-by-human and learned-from-a-run are different trust levels,
   and learned entries never silently outrank curated ones. Keep this store **separate from
   `recovery/registry.py`** — that holds human-authored remediation strategies, and mixing
   auto-written locators in collapses two trust levels into one.
3. **Decay and evict.** Track hit/miss; N consecutive misses → demote → delete. Gmail ships UI
   changes, and a stale cache is worse than none because it is fast and wrong.
4. **Memory never shortcuts a gate.** A learned `Send` macro still hits the approval interrupt.
   No exceptions, no config flag.
5. **No PII in the store, ever.** It lives beside the vault; keys and values must be token-safe by
   construction.

**Acceptance:** compose completes in ≤2 LLM round trips on a warm cache, and a deliberately
corrupted cache entry produces a verified miss and a correct fallback — not a wrong click.

**Status — read this before building on top of it.** The plan bundles two genuinely different
pieces of work under one heading, and only one of them can be done from here at all.

**Built: `app/surface/memory.py` — the store, and its full discipline.** All five rules from this
section, each with its own test, 26 tests total:

1. *A cached locator is a hypothesis, never a fact.* `recall()` takes the DOM check as a caller-
   supplied `verify` callable and calls it on every lookup, hit or miss — there is no code path
   that returns a descriptor without it being re-checked against something live.
2. *Provenance is typed.* `Provenance.CURATED` / `Provenance.LEARNED`; a `LEARNED` write can never
   overwrite a `CURATED` entry for the same key, proven directly rather than assumed. Deliberately
   its own module, not folded into `recovery/registry.py` — same reasoning as that file's own
   docstring: mixing a human-authored trust level with a self-written one collapses the distinction
   that makes either trustworthy.
3. *Decay and evict.* `MAX_CONSECUTIVE_MISSES = 2` (one transient failure — an animation mid-
   render — must not evict a locator that is actually still correct); a curated entry decays on
   the *identical* schedule as a learned one, because a UI redesign does not spare hand-written
   locators either.
4. *Memory never shortcuts a gate.* Enforced structurally, not by convention: this class has no
   `act`, `approve`, `preview`, `dispatch`, or `send` method, pinned by a test that checks exactly
   that. It cannot bypass the approval gate because it has no path to the surface at all.
5. *No PII in the store, ever.* `PageSignature` and `LocatorDescriptor` refuse construction outright
   (`UnsafeMemoryValue`) if a field matches this project's own email/phone patterns — reused from
   `security/patterns.py`, not reimplemented. Vault tokens (`P17`) are explicitly let through: a
   token is a *reference* to PII, not PII itself, and refusing it would make it impossible to
   remember anything about a recipient-shaped field.

The second half of the plan's acceptance criterion — *"a deliberately corrupted cache entry
produces a verified miss and a correct fallback"* — is exactly what
`test_consecutive_misses_evict_the_entry` and the `never_matches`-verifier tests prove. That half
is done.

**Not built, and not attempted: everything that requires a live DOM to mean anything.**

- **The brain still emits `Send(index=27)`, never bare `Send`.** Making the index optional and
  letting the executor resolve it from a remembered locator means widening `tools.py`'s schema and
  `ActionValidator._resolve_index` in `app/surface/dispatch.py` — the exact code path that keeps
  every action's approval fingerprint tied to a real, currently-visible element. That is not a
  change to make speculatively; it needs a real page to prove `verify()` actually rejects a stale
  locator rather than rubber-stamping it.
- **Nothing calls `recall()` or `remember()` from `PlaywrightEmailSurface`.** The store exists,
  fully tested, with no consumer — matching this project's own established pattern for every other
  store (`InMemoryRulesStore`, `InMemoryTrajectoryStore`): build the port and the in-memory
  implementation first, wire a durable backing store in only once there is a real caller. Building
  the durable half now, with nothing to persist yet, would be dead code with no way to prove it
  correct.
- **Macro actions (`OpenCompose` → `FillRecipient` → `FillSubject` → `FillBody` as one dispatch)
  were not attempted at all**, deliberately. This is the largest, least-reversible piece of the
  plan: it moves part of the observe→act loop from the graph into the executor, and any bug in
  that boundary is a bug in the exact mechanism that currently guarantees `Send` always pauses for
  a human. Building it without a live Gmail session to exercise the failure modes against would be
  guessing at an architecture change to the single most safety-critical part of this system.
- **The first half of the acceptance criterion — "compose completes in ≤2 LLM round trips on a
  warm cache" — is consequently unverified and cannot be claimed.** That number can only come from
  running a real compose flow against a real page, which is exactly the gap
  [IMPROVEMENTS.md](IMPROVEMENTS.md) item 2 already names: the extension has never driven real
  Gmail, in this session or any prior one.

**What unblocks the rest:** a live browser session (Playwright against a fixture Gmail account, or
the extension paired to a real one — either satisfies item 2 and item 4/A4 at the same time). Once
one exists, the remaining work is: implement `verify()` for `PlaywrightEmailSurface` (a role +
accessible-name query against the current page), widen `Send`/`Click` to accept an optional index,
and *only then* consider macro actions — each step individually checkable against that same live
session rather than three risky changes landing at once.

## B6. Self-improvement, in three tiers · ongoing

**Tier 1 — in-run recovery.** Exists (`causes.py`, `strategies.py`, ranked HITL options). The
upgrade: make ranking **empirical**. Every remedy attempt writes `(cause, strategy, outcome)` to
the trajectory store; `applies_to()` reads observed success rates. The recovery layer then
improves without anyone editing it.

**Tier 2 — cross-run learning.** Offline, never in the live loop. A periodic job over trajectories
that mines successful runs into candidate macros and locators, clusters failures by cause to
surface the top-5 failure modes (which is also your roadmap), and **emits proposals, never live
changes**. Every proposal passes B0's promotion criterion before it ships.

**Tier 3 — self-modifying source.** Out of scope, deliberately, per `CLAUDE.md` §17. Unbounded
blast radius, no rollback story, no way to attribute a regression. A sandboxed dev-assist mode may
propose patches for human review; it stays out of the live loop.

## B7. Close the feedback loop · ~2 days · *the one the docs missed entirely* · DONE (two acceptance items deliberately not built — see Status)

**Start by reading `backend/app/feedback/`, because it is further along than a plan would
assume.** Four kinds already exist — `ASSESSMENT` (the model's own verdict), `CORRECTION`,
`ENDORSEMENT`, `REJECTION` — mid-run corrections are already injected into the loop
(`loop.py:254-258`) and tracked with an `applied` flag so unapplied human feedback is a
broken promise rather than an assumption. `RuleCandidate` already carries a promotion
threshold and a written prompt.

**Three ends dangle, and each one severs the loop:**

1. **`ENDORSEMENT` is never recorded.** Nothing in the codebase constructs one. So the
   system can only ever learn from complaints — it has no idea what it got *right*. The
   approval gate is the natural source: Approve is an endorsement, Reject is a rejection.
   Both are already human decisions on a specific action; they are simply not being kept.
2. **`RuleCandidate` is computed and nothing reads it.** The prompt is written —
   *"You've told me this 3 times — shall I make it a standing rule?"* — and never reaches a
   human. This is the entire promotion path from repeated correction to standing rule, and
   it is one wire away from working.
3. **Nothing asks how the run went.** No post-run satisfaction ask exists in the backend or
   the cockpit. The run ends and the outcome is whatever the agent said about itself.

**Four rules:**

- **Feedback you do not consume is collection theatre.** Wire the consumer before adding
  another collector. All three items above are consumers for signal you already have.
- **Ask once, at the end, cheaply.** A per-step thumbs is noise, and it interrupts the thing
  being judged.
- **A rating never gates a send.** It is learning signal, not authorization. Keep it
  structurally incapable of unblocking an approval.
- **The human label is B0's ground truth.** Without it, your success metric is the agent's
  own `Complete(success=True)` — the agent grading its own homework. This is the wire that
  makes the eval harness mean something, which is why B7 is not optional decoration.

**Acceptance:** an endorsement recorded when a human approves; the promotion prompt reaching
the cockpit and writing to `RulesStore` on accept; a post-run label landing on the trajectory
and appearing as a column in the B0 table.

**Status:** DONE, with two parts of the acceptance criterion deliberately not built — one
because it would be unsafe, one because it is not achievable as written.

**End 1 — the approval gate now produces feedback.** `build_approval_gate_node` takes the
`FeedbackStore` the graph already had and files every verdict: approve → `ENDORSEMENT`,
reject → `REJECTION`, edit → `CORRECTION`. That last mapping is the one that matters and it
is not the two-way mapping this plan suggested: `candidates()` counts *corrections*, so filing
an edit anywhere else would leave the promotion counter reading zero forever — and "add
regards", "shorter please", said across three runs, is exactly the standing-rule signal the
promotion path exists to catch.

**The preview is never stored, and that is load-bearing.** `preview` is the *resolved* draft —
real addresses, real body text, un-tokenized on purpose so a human can verify what they are
approving. The feedback store is persisted and read back across threads by `candidates()`.
Putting a resolved draft in it would undo the vault one approval at a time. What is stored is
`request.summary` ("Send this email", from a fixed table keyed on the verb) plus the verb —
both structural. The single exception is an edit, whose text is the human's own words, which is
what the existing mid-run feedback channel already records. Pinned by a test that asserts
neither the address nor the body text appears anywhere in the store's serialized contents.

Approval feedback is recorded `applied=True`: the human said it *to* the gate and the gate
acted on it in the same turn, so leaving it pending would have the loop replay their own
decision back at them as fresh guidance next turn.

**End 2 — rule candidates reach a human.** `emitter.rule_candidate` and the `RULE_CANDIDATE`
protocol event both already existed and were called by nothing. `_offer_rule_candidates` in
`api/ws.py` now runs at the end of a run — never during it, since a "shall I make this a
rule?" prompt mid-flight competes with the thing the user is actually watching, and the answer
does not change this run's outcome. Only the single strongest candidate is offered; four
stacked suggestions get none of them read. It is wrapped so a store failure cannot report a
successful run as failed. The cockpit renders it (`types.ts`, `useAgentRun.ts`,
`Transcript.tsx`), where it previously would have arrived and been silently dropped.

**Not built: "writing to `RulesStore` on accept".** Deliberately. `feedback/store.py`'s own
docstring is explicit that the system proposes and the human disposes, and ADR-006 turns on
nothing gaining capability without a person saying yes. A one-click "accept" at the moment a
run ends is a reflexive click that permanently changes behaviour on a surface that sends
email — the wrong shape for that decision. The suggestion is rendered as a *note*, not a
prompt with buttons. A considered accept flow (a rules screen, showing what the rule would
match, reversible) is real work and belongs with its own UI, not bolted to the end of a run.

**End 3 — a verdict on a whole run.** New `FeedbackKind.RUN_RATING`, deliberately not reusing
`ENDORSEMENT`/`REJECTION` now that those come from the approval gate and mean something
narrower ("this specific send was right"). A run rating is the only signal in the system that
judges the *outcome* rather than a step, which makes it the only thing that can tell you
whether `Complete(success=True)` was actually true.

Two properties it needed and now has, both tested: it is recorded `applied=True` so it can
never sit in `pending()` and be replayed to the model as an instruction ("that run went badly"
is a label, not something to act on); and `rating()` returns `None` for an unrated run rather
than anything falsy-but-negative, because "nobody said" and "somebody said it was bad" are
different facts and an evaluation that conflates them scores every unattended run as a
failure. The existing `feedback` socket message carries it — a finished run stays attachable
for a TTL, so the channel already reaches it — and unknown kinds from a newer cockpit now fall
back to `CORRECTION` rather than raising.

**Not built: "appearing as a column in the B0 table".** Not deferred — not achievable as
written. Every B0 golden task runs against a *scripted* `LLMClient` with no human anywhere in
the loop, so there is no one to produce a rating. A human-label column would be structurally
empty for all 16 tasks. The mechanism now exists so that *real* runs carry labels, which is
what a future evaluation over real trajectories (B6's tier 2) would read; the scripted bench
is the wrong consumer for it. What is missing to close that properly is real runs to label,
which is [IMPROVEMENTS.md](IMPROVEMENTS.md) item 2 again.

**Also not built: the cockpit does not yet ASK for a rating.** The backend accepts and stores
one, and the transport is proven, but nothing prompts the user at the end of a run. That is a
small piece of UI work with a real design question attached (how to ask once, cheaply, without
nagging) and it is honest to name it as outstanding rather than count the channel as the
feature.

## B8. Self-awareness — calibrated confidence that changes control flow · ~2 days

**The substrate exists.** `agent/assessment.py` has `split_assessment` / `derive_outcome` /
`outcome_note`, `worker.txt` requires an opening `Assessment:` line, and `Outcome` includes
the sharp case: `NO_EFFECT` — the action *succeeded*, no error, and the page did not change.
That combination is invisible to a success flag and is the most common way a run is wasted.

**The gap: the assessment is recorded but does not change what happens next.**
Self-awareness that does not alter control flow is a diary.

- **Escalate on `NO_EFFECT`.** First: try a different approach. Second: widen the
  observation. Third: ask the human. Today the repetition and stuck guards catch this, but
  late — after the turns are already spent.
- **Measure calibration in B0.** Bucket the model's claimed confidence against actual
  outcome. If they are uncorrelated, the `Assessment:` line is decoration costing tokens on
  every single turn, and the correct response is to delete it. Be willing to take that
  result — an uncalibrated confidence signal is worse than none, because it gets trusted.
- **Prefer contract checks over self-report.** "Is it in Sent?" beats "I think I sent it."
  Self-report is the cheapest and least reliable signal available: use it to *trigger* a
  check, never to replace one.

**Acceptance:** a `NO_EFFECT` streak provably changes the next action rather than repeating
it, and the B0 table carries a calibration column.

## B9. Reliability and latency as budgets, not adjectives · ~1 day

Neither is a feature. Both are numbers you either track or lose.

**Reliability.** Typed `ErrorCode`s and the guards already exist, and `CLAUDE.md` §11 states
"100% terminated with a typed code" — as an aspiration, which cannot regress because nothing
measures it. B0 turns it into a figure. Track alongside it: p50/p95 steps, retry rate,
invalid-referent rate (from B1), and **gate-bypass count, which must be 0 permanently.**
That last one is the number that must never move, because it is the one that sends mail.

**Latency.** `StepRecord.latency_ms` is captured (`telemetry/records.py:89`) and never
reported anywhere. Per `CLAUDE.md` §2, latency is dominated by the LLM call, not the relay —
so the lever is *fewer round trips*, which is precisely B3, B4, and B5. Nothing here needs a
new mechanism; it needs the number surfaced so those three can be judged.

Two budgets worth naming: **time-to-first-action** (already moved 37s → 2.1s by tab reuse)
and **time-to-completion per golden task**.

**Acceptance:** both appear as columns in the B0 table, with today's figures recorded as the
baseline.

---

## Running order

| # | Work | Gated by | Payoff |
| --- | --- | --- | --- |
| A1 | Commit | — | the work survives |
| A2–A3 | Verify + tidy | A1 | today's fixes proven |
| **B0** | **Eval harness** | A2 | **every claim below becomes checkable** |
| B1 | Dispatcher as validator | B0 | hallucination becomes a metric |
| B2 | Conditional slots + fan-out | B0 | the A-vs-B behaviour |
| B3 | ROI scoping + phase binding | B0 | biggest token cut — DONE (93% cut on background at realistic scale) |
| B4 | Prompt diet | B1–B3 | the rest of the token cut — DONE (898 -> 327 tokens, 64%) |
| A4 | Extension on real Gmail | A1 | the deployable surface |
| A5–A6 | Budgets + auth binding | A4 | safe to expose |
| B5 | Macros + procedural memory | B0–B4 | 5 round trips → 1 — PARTIAL (store built; live wiring blocked on A4) |
| B6 | Empirical ranking, offline mining | B0, B5 | improves without you |
| B7 | Close the feedback loop | B0 | human labels = eval ground truth — DONE (rating channel built; cockpit does not yet ask) |
| B8 | Calibrated confidence | B0 | assessments change control flow |
| B9 | Reliability + latency budgets | B0 | adjectives become numbers |
| A7–A9 | Screencast, fixtures, CI | — | polish |

**The one ordering rule:** B0 precedes every B. A learning system without an eval gate does not
get better, it gets weirder — and you will not be able to tell which.
