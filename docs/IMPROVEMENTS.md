# Improvements

Where this codebase actually stands, and what is still wrong with it.

Written against the working tree, not from memory. Every "done" below is backed by a passing
test; every "not done" is a real gap rather than a nice-to-have. The companion document,
[IMPROVEMENT-PLAN.md](IMPROVEMENT-PLAN.md), says how to close them.

---

## Where things stand

| Suite | Passing |
| --- | --- |
| Backend (`pytest`) | 711 |
| Bridge extension (`vitest`) | 130 |
| Contracts | 23 |
| Cockpit | 8 |

Ruff clean. Both funnels agree on the shared conformance fixtures. The extension builds to a
loadable `dist/`.

**Two surfaces exist behind one four-method port.** `PlaywrightEmailSurface` drives a
server-side Chromium and is the right choice for fixtures, CI, and the benchmark.
`ExtensionEmailSurface` drives the user's own Chrome through a bridge extension and is the
only one that can be deployed for other people.

---

## What is fixed

These were real defects found by running the thing, not by reading it. They are listed
because the same classes of bug will recur, and the notes are the cheapest defence.

### Safety

- **The approval gate was bypassable.** Gating was a set of verb names, but the compose
  worker also has `Click`, and Gmail's Send button is an ordinary indexed element.
  `Click(index=108)` sent mail with no approval at all. Now decided by *consequence* —
  including `Ctrl+Enter`, which has no button to inspect.
- **Approval bound to a button, not an email.** `Send(index=108)` says where the button is,
  not what the message says, so one "yes" authorised that button for the rest of the run.
  The fingerprint now covers the previewed content, re-read from the live fields at dispatch.
- **Passwords could reach the model.** The extractor returned `el.value` for any element that
  had one — including `<input type="password">`. Redacted in the page now, at the earliest
  point where the guarantee can be total.
- **Raw PII reached the model.** Intake tokenized the intent slots but not the task text, so
  the worker was handed a real address *and* told literal addresses were forbidden — a
  deadlock it could not reason out of. It also put the address in browser history via the
  run URL. Both closed.
- **One shared pairing code meant one shared mailbox.** A second user's extension replaced
  the first in the registry, so user A's next run would have driven user B's mail. Pairing is
  now per-user with a durable bridge token.

### Perception

- **The agent could not see the compose window.** ~6 compose fields lost the token budget to
  ~140 inbox rows, so Subject and Send were trimmed before the model saw them. It then
  scrolled a dialog that does not scroll until the stuck guard killed the run. An open dialog
  now outranks everything behind it.
- **It could not tell a field was already filled.** A committed recipient becomes a *chip*, a
  separate node, so the To input read empty and the address was typed twice. `MailContext`
  now reports `toFilled` / `subjectFilled` / `bodyFilled` — booleans only, never content.
- **Every run cold-booted Gmail.** A fresh tab per run cost **37 seconds** before the first
  action, and `domcontentloaded` returned so early the first observation saw **4 elements**.
  Now reuses an open Gmail tab and waits for the UI to render: **2.1s, 174 elements**.
- **Typing a long body timed out and self-destructed.** 190 characters key-by-key took ~9.5s
  against a 10s wall; the agent then cleared the field and retried in chunks, breaching it
  again — writing correct text and deleting it forever. Bulk insert plus a payload-scaled
  wall: **4.53s → 0.48s**.

### Routing and providers

- **Compose tasks were routed to the rules worker**, which has no perception loop, so they
  produced nothing and reported "the model stopped choosing actions" — a routing mistake
  blamed on the model. Topology is now clamped by what the worker can actually do.
- **Gemini 400s and 404s.** Tool-call ids did not match their results (invisible on OpenAI-
  shaped providers, fatal on Gemini's shim), and one model roster was sent to all three
  providers. Both fixed; providers now bench themselves when rate-limited and say so in the
  cockpit.
- **A dispatch rejection's typed code vanished before it reached the trajectory.** Found
  while building the eval harness (B0), not by reading the code. The dispatcher already
  refused a stale index, an unminted token, an unbound verb, and a second compose window —
  correctly, and the model was correctly told why. But `StepRecord.error_code` was typed as
  the run-*termination* code vocabulary, and a dispatch code like `STALE_INDEX` isn't a
  member of it; assigning one would raise a `ValidationError`. Two call sites had
  independently discovered this and silently written nothing rather than crash — one
  `hardcode`d `None`, the other omitted the field. Every hallucinated referent this session
  fixed was therefore invisible to `TrajectoryStore`, offline mining, and this benchmark
  alike. Fixed by widening the field to `str | None`; see B1 in
  [IMPROVEMENT-PLAN.md](IMPROVEMENT-PLAN.md).

---

## What is still wrong

Ordered by what blocks what. Nothing here is speculative.

### 1. Nothing is committed

**39 modified files and 3 new ones sit in the working tree.** Every fix above, the entire
bridge extension, and the whole auth layer exist only on one disk with no history. A stray
`git checkout` ends the project.

This is the highest-severity item on the list and the cheapest to fix.

### 2. The extension has never driven real Gmail

Every layer is unit-tested and the contracts validate at the boundary, but `chrome.debugger`
against a live single-page app is precisely where the surprises are. Until one run completes
end to end through the bridge, "it works" is an inference from tests, not an observation.

### 3. `/ws/run` is open by default

`AUTH_MODE` defaults to `off`. That is correct for a laptop and a breach on a public URL. The
startup banner shouts about it, which is a mitigation and not a fix. Anything deployed must
set `AUTH_MODE=google` — and nothing currently prevents deploying without it.

### 4. No rate limiting anywhere

Every user spends **your** Groq/OpenRouter/Gemini allowance. Groq's daily token cap was
reached twice in a single afternoon by one person. A public URL with no per-user budget is a
free-tier account that dies in an hour, and there is no cap on steps, tokens, or concurrent
runs.

### 5. The live browser pane is blank on the extension surface

Runs work; the right-hand view shows nothing, because `Page.startScreencast` was never wired
through `chrome.debugger`. Users can watch their own Gmail tab instead, which is arguably
better — but the cockpit currently looks broken rather than deliberately empty.

### 6. The funnel exists twice

Python for Playwright, TypeScript for the extension. The conformance suite pins them to
shared fixtures and has already caught one real divergence, but it only covers six cases.
Anything outside those cases can still drift silently.

### 7. Pairing codes do not survive a restart

They are in-memory with a ten-minute TTL, which is correct for their lifetime — but a backend
restart mid-pairing means the user clicks the button again with no explanation. Already-paired
browsers are unaffected.

### 8. Smaller, but real

- **No `Navigate` verb**, deliberately — folder changes cost three steps instead of one. The
  alternative is an agent that can be talked into loading an arbitrary URL, which is worse.
- **Send-button matching is language-dependent.** Names are matched in English; a Gmail set to
  another language would not trip `IRREVERSIBLE_NAMES` on a click (the `Send` verb and
  `Ctrl+Enter` still would).
- **A stale root `.env`** shadowed `backend/.env` for most of a day. Settings are now anchored
  to the module, but the stale file is still on disk and should be deleted.
- **One flaky test** — `test_ask_user.py::test_the_answer_comes_back_to_the_model` failed once
  mid-session and has passed on every run since. Cause unknown.
- **`pnpm run check` exits 134** under this Node/PowerShell combination even though every step
  passes individually. Cosmetic, but it makes the drift guard useless in CI as written.

---

## What you have to do

In order. Items 1–3 are prerequisites for anything else; 4–6 are what "production" means;
7–10 are quality.

| # | Task | Why now | Size |
| --- | --- | --- | --- |
| 1 | **Commit everything** | ~2,700 lines with no history | minutes |
| 2 | **Run one compose task end to end** on the Playwright surface | Confirms the whole afternoon of fixes | minutes |
| 3 | **Delete the stale root `.env`** | It will confuse you again | seconds |
| 4 | **Drive real Gmail through the extension** | The only untested layer, and the deployable one | hours |
| 5 | **Add per-user rate limiting and budgets** | One user already exhausted a daily cap twice | ~1 day |
| 6 | **Make `AUTH_MODE=off` refuse to bind a public interface** | A banner is not a control | ~2 hours |
| 7 | **Wire screencast through `chrome.debugger`** | The live pane is the demo | ~half a day |
| 8 | **Widen the conformance fixtures** | Six cases is thin for two implementations | ~half a day |
| 9 | **Fix the `pnpm run check` exit code** | The drift guard cannot be trusted in CI | ~1 hour |
| 10 | **Chase the flaky test** | One unexplained failure is one too many | ~1 hour |

Deferred on purpose, with reasons: a `Navigate` verb (attack surface outweighs three saved
steps), localised Send matching (needs real non-English Gmail to test against), and retiring
the Python funnel (only worth it once the extension is the primary surface).

See [IMPROVEMENT-PLAN.md](IMPROVEMENT-PLAN.md) for how each of these gets done.

---

## The agent-intelligence gaps

Separate from the operational list above, and larger. These are about how well the agent
thinks, not whether it survives.

| # | Gap | Symptom you see | Plan |
| --- | --- | --- | --- |
| 11 | ~~**No eval harness**~~ **DONE** — §20 promised one; built as `backend/tests/bench/` | No way to tell whether a change helped | B0 |
| 12 | ~~**Hallucinated referents are not typed failures**~~ **DONE, but not as scoped** — they already were; the real gap was that the typed code never reached the trajectory (see B1) | "It clicked the wrong thing" with no metric | B1 |
| 13 | **Ambiguity is resolved in the loop, not in PRE** | Cannot ask "together or separately?" | B2 |
| 14 | **The funnel is not scoped to the active region** | 120 inbox rows sent while composing | B3 |
| 15 | **~900 tokens of prompt scar tissue** | Slow, expensive, and dilutes every other rule | B4 |
| 16 | **No procedural memory** | Re-derives where Send is on every single run | B5 |
| 17 | **Recovery strategies are hand-ranked, never learned** | Self-healing does not heal better over time | B6 |
| 18 | **The feedback loop is built but not closed** — `ENDORSEMENT` is never recorded, `RuleCandidate` is computed and never read, nothing asks how a run went | It only learns from complaints, and never from what worked | B7 |
| 19 | **Assessments are recorded but change nothing** | `NO_EFFECT` is spotted and then repeated until a guard fires | B8 |
| 20 | **Reliability and latency are adjectives, not numbers** — `latency_ms` is captured and never reported | No way to notice a regression | B9 |

The unifying diagnosis: `worker.txt` is full of English patches for defects that belong in the
action layer. Every one of those lines is a tax paid on every turn and enforced only
probabilistically. Migrating them into typed code cuts tokens, cuts hallucination, and opens
the seam where memory plugs in — which is why items 12–16 are one programme rather than five
chores.

See [IMPROVEMENT-PLAN.md](IMPROVEMENT-PLAN.md) Part B. **Item 11 gates all the others.**
