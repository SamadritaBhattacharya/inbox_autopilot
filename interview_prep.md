# Interview Prep — Inbox Autopilot

Quick-reference Q&A for talking through the project in an interview.

## Contents

1. [What is this project about?](#1-what-is-this-project-about)
2. [Why build this project?](#2-why-build-this-project)
3. [What was the inspiration?](#3-what-was-the-inspiration)
4. [What&#39;s this project&#39;s attraction?](#4-whats-this-projects-attraction--what-makes-it-interesting)
5. [Delivery tips &amp; suggested next rounds](#5-delivery-tips--suggested-next-rounds)
6. [Typed failure layer — in plain words](#6-typed-failure-layer--in-plain-words)
7. [What do you mean by surface-agnostic?](#7-what-do-you-mean-by-surface-agnostic-short-understandable-answers)
8. [How the DOM → numbered list works (7 steps)](#8-how-the-dom--numbered-list-works--7-steps)
9. [Masking vs. hiding — is this the right architecture?](#9-masking-vs-hiding--is-this-the-right-architecture)
10. [LLM fallback — how providers switch](#10-llm-fallback--how-providers-switch)
11. [What are the read-only verbs / the full verb list?](#11-what-are-the-read-only-verbs--the-full-verb-list)
12. [How does the feedback loop work?](#12-how-does-the-feedback-loop-work)
13. [Indexing vs. coordinates — who sees what?](#13-indexing-vs-coordinates--who-sees-what)
14. [Does re-indexing every turn slow the system down?](#14-does-re-indexing-every-turn-slow-the-system-down)
15. [Is any of this just DSA under the hood?](#15-is-any-of-this-just-dsa-under-the-hood)
16. [The router — linear vs. decision](#16-the-router--linear-vs-decision)
17. [What exactly does the &#34;clamp&#34; do?](#17-what-exactly-does-the-clamp-do)
18. [What rules does the system ship with?](#18-what-rules-does-the-system-ship-with)
19. [What operating rules does the whole system follow?](#19-what-operating-rules-does-the-whole-system-follow)

---

### 1. What is this project about?

**Say this first (30 seconds):**

> "It's an email agent that operates Gmail the way a person does — through the actual browser UI, not the API. You type a task in plain English, and you watch it happen on screen: it clicks Compose, fills the recipient, writes the body, and then stops at Send and asks you. The interesting part isn't the LLM. It's the three layers around it — a funnel that turns a 100k-token DOM into a 1–3k-token numbered list, a PII vault so the model never sees a real email address, and a typed-failure layer so every run ends with a reason code instead of just hanging."

**Then expand if they want more:**

- Two panes. Left is the conversation and run history. Right is the live browser screen. You see every step as it happens — that's not polish, it's the trust mechanism. If you can't see why it did something, it did the wrong thing.
- The core loop is observe → reason → act, but that loop is just one worker. Above it sits a supervisor graph that handles: did I get enough context to start, is this task deterministic or does it need judgment, which worker runs, does this action need approval, did it actually work, and if not — what do we offer the human.
- The one rule that generates the whole architecture: the model never sees the raw page and never sees real PII. It sees a numbered list like `[12] button "Compose"` and tokens like `P17` instead of `alice@corp.com`. It picks a number and a token. The executor — sitting next to the browser — resolves both and fires a real trusted input event.
- Four workers: Triage (archive/label/snooze the noise), Compose (draft → approval → send), Calendar (pull an event out of a thread), Rules (deterministic, zero LLM calls).
- Hard guarantee: nothing irreversible happens without a human. Send, delete-forever, invite dispatch — all pause. And it's not a prompt instruction, it's graph topology. There's a test that proves no path reaches dispatch without passing the gate.

**If they push "so it's a Gmail wrapper?":**

> "No — a wrapper would use the API. I deliberately didn't. The reusable core here is surface-agnostic: the funnel, the Set-of-Marks indexing, the trusted-input dispatch, and the failure engine work on any web UI. Gmail is the slot I filled in. Swapping in another surface is one adapter class, zero graph changes."

### 2. Why build this project?

**Say this first:**

> "Because email is the highest-volume, lowest-leverage thing most people touch, and every existing solution fails in a way I could name precisely. I wanted to build the thing that fails in none of those three ways — and honestly, I wanted a project where the hard problems were engineering problems, not prompt problems."

**The three failures — name them, this is what makes the answer credible:**

| What exists              | Why it doesn't work                                                                                                                                     |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Gmail filters / rules    | Purely deterministic. Can't read intent, can't draft, can't judge "does this actually need me?"                                                         |
| API-based LLM assistants | Need broad OAuth scopes people won't grant, break the moment you want something the API doesn't expose, and ship your whole mailbox to a model provider |
| Generic browser agents   | No email semantics, no approval model — they'll happily send a half-written email to the wrong person                                                  |

**The gap I was aiming at:**

- Real UI reach — it can do anything you can do in Gmail, because it's using Gmail.
- Email semantics — archive, label, snooze, draft, extract-event are first-class verbs with reversibility properties attached, not generic clicks.
- A trust model strong enough that you'd actually let it near your inbox. That's the real product.

**The engineering motivation — say this part, it's what a startup wants to hear:**

- I wanted to build an agent that has a reliability number, not a demo video. Most agent projects can't tell you their success rate, because half their runs just... stop. So I forced every terminal state to carry a typed `ErrorCode` and built a 15-task benchmark harness that runs with no browser and no network. Current baseline: 15/15, 100% typed termination, mean 1.1 steps.
- I also gave myself a $0 cost constraint — free tiers only, Groq → OpenRouter → Gemini fallback chain. That constraint was productive: it forced a cheap classifier for triage, a lean loop, prompt caching, and a deterministic route that skips the LLM entirely. "Cheapest correct path" became a design principle rather than an optimization I'd do later.

**If they ask "did you actually use it?":**

> "Yes — and the guards exist because of what I saw live, not from theory. Example: I had exempted read-only verbs like Extract from the repetition guard because they seemed harmless. Then I watched it make four identical `Extract("what's in the To field?")` calls in a row against a provider that was already rate-limiting me. 'Read-only' isn't 'free' — on a free tier the binding constraint is requests, not consequences. I removed the exemption."

### 3. What was the inspiration?

Don't name a tool. Name observations. Three of them, and they build on each other.

**Say this:**

> "Three things, and they stacked. First — I noticed that every time I tried to automate my own inbox, the API was the wall. Not the model. Second — I realized the thing blocking browser agents isn't perception, it's that a page is 100k tokens and a model has no safe way to point at something on it. Third — the actual research insight: you don't need the DOM, you need the accessibility tree plus geometry, and if you number the interactable elements, the model can reference them by integer instead of by coordinate or selector."

**Unpack each one:**

**1. The API wall (the problem observation)**

- Every "AI email assistant" I looked at hit the same ceiling: the API exposes maybe 60% of what the UI does, and the remaining 40% is exactly where the value is — the multi-step flows, the compose UI behaviors, the things Google never bothered to expose.
- And the consent problem is worse than the capability problem. Asking a user for full Gmail OAuth scope is asking for the thing they're most trained to refuse. Driving the browser they're already logged into asks for nothing new.

**2. How humans actually operate a UI (the perception observation)**

- A person doesn't read the DOM. They see maybe 30 things on screen, ignore 95% of them, and act on one. That's a filter, and it's mechanical — visibility, occlusion, wrapper collapsing. None of it needs a model.
- So the design became: spend zero intelligence on perception, spend all of it on judgment. The funnel is seven dumb, single-responsibility stages — extract, visibility, occlusion, wrapper-collapse, PII-tokenize, index, format. Every one of them is independently unit-testable. That's the opposite of "throw the page at GPT and hope."

**3. Set-of-Marks — the technique that unlocked it (the research observation)**

- The idea that you overlay integer marks on interactable elements and let the model reference `[12]` instead of a CSS selector or an (x, y) — that's what makes the whole thing safe and cheap. The model can't hallucinate a coordinate. It can only pick a number that exists or a number that doesn't, and a number that doesn't is a typed `STALE_INDEX` error I can catch at the dispatcher.
- And once elements are indexed, PII tokenization falls out naturally — if the model is already referring to things by symbol, referring to people by symbol costs nothing. `alice@corp.com → P17`. That's ADR-005 in my repo: tokenize before indexing, so nothing downstream ever holds raw PII.

**The honest closer (say this — it lands well at a startup):**

> "The inspiration for the architecture, though, was watching agents fail. Every agent demo that impressed me failed the same way off-camera — it would loop, or silently stop, or do something irreversible. So I built the failure layer first-class instead of bolting it on: two independent loop detectors, per-action timeouts, a budget warning, and a rule that human-in-the-loop is never a fallback for confusion. HITL is allowed exactly three places — missing context, irreversible action, and a self-heal with ranked options. 'Ask the human when confused' is how an agent stops having a measurable reliability number."

### 4. What's this project's attraction? / What makes it interesting?

Frame it as: the hard parts are the reusable parts. Five things, in order of how much they'd care.

**1. It's a real reliability engineering problem, not a prompting problem**

- Every terminal state carries a typed error code — `STUCK`, `ACTION_TIMEOUT`, `REASONING_MISSING`, `MAX_STEPS`, `NO_ACTION`, `APPROVAL_TIMEOUT`, `APPROVAL_REJECTED_NO_ALT`. "It just stopped" is a bug I can't ship.
- Two independent loop detectors, because they catch genuinely different loops:
  - Page signature catches "my actions have no effect" — clicking a dead button. The page doesn't change.
  - Action repetition catches "I keep doing the same thing while the page churns" — the clear-and-retype loop, where every turn does change the page, so the signature check sees progress that isn't there.
- Either one alone leaves a whole class of hang running. That's a lesson from watching it fail, not from a blog post.

**2. Safety is structural, not textual**

- The approval gate is a node in the graph. You cannot reach dispatch for Send without passing through it, and there's no config flag that disables it in v1.
- The benchmark reports one metric with an absolute bar rather than a trend: approval gates: N irreversible actions, 0 bypassed. That must read zero forever. Everything else is allowed to be a trend line.
- Contrast this with "I told the model in the prompt not to send without asking" — that's unenforceable and untestable. As topology, a test proves it.

**3. Privacy that's demonstrable, not claimed**

- Raw DOM and raw PII never cross the wire. The funnel runs in the executor, next to the browser. Only tokenized observations travel.
- Which means I can say to a user: "the model literally cannot leak your contacts, because it was never given them" — and then show them the wire format. That's a much stronger claim than a privacy policy.

**4. It's measured, and the measurements found real bugs**

- The benchmark harness runs the whole graph with a scripted `LLMClient` — no browser, no network, deterministic. 15 golden tasks.
- It found things I'd have shipped otherwise. Two examples worth telling:
  - `error_code` was being silently dropped on failed actions at two call sites, because `StepRecord.error_code` was typed as the run-termination enum and a dispatch-level code like `STALE_INDEX` isn't a member of it. Assigning it would have raised — so the code discarded it. Widened the field, fixed both sites.
  - The highest-stakes pause in the system — right before mail goes out — was setting status `running` instead of `awaiting_human`, so anything reading status couldn't tell "waiting on a human" from "still working." I pinned it as a recorded defect in the benchmark file rather than quietly fixing it, so the regression can't come back.
- The point: I have a baseline, and I have a rule that no change ships unless it beats baseline on success rate without regressing steps or tokens.

**5. Recent work shows it's still being engineered, not just built**

- Capped background noise inside an open compose dialog — 93% reduction in observation size for that state, because everything behind the modal is occluded and irrelevant.
- Shrank the worker prompt 64% with zero loss of enforced behavior — because the behavior was enforced by the graph, not the prompt. That's the payoff of ADR-002 in a single number.
- Closed the feedback loop: approve / edit / reject now record endorsements and corrections, repeated preferences surface as rule candidates at run end, and whole-run ratings become eval ground truth. So the deterministic Rules worker gets better over time from actual human corrections — the cheap path grows at the expense of the expensive one.

**The one-line closer if they ask "why should I care?":**

> "Because the reusable asset here isn't the email agent. It's the funnel, the typed-failure engine, and the approval-as-topology pattern. Point them at any web UI and you have an agent that's cheap, auditable, and can't do something irreversible behind your back. Gmail was just the hardest surface I could find to prove it on."

### 5. Delivery tips & suggested next rounds

**Quick tips for delivery:**

- Lead with the constraint, not the feature. "The model never sees the DOM" is more interesting than "it has a funnel." State the rule, then show the architecture falls out of it.
- Always have one failure story ready per topic. The Extract repetition loop, the dropped `error_code`, the `awaiting_human` status bug. Startups hire people who debug, not people who describe.
- Use your ADRs. When they ask "why X and not Y?", you literally have a document with the rejected alternative and the cost you accepted. Say "I accepted that cost, here's how I mitigated it" — that's senior-sounding and it's true.
- Don't oversell. If they ask what's not done: self-healing reads a curated skill registry and does not edit its own source — deliberately out of v1, unbounded blast radius. Live `StepRecord`s don't yet carry LLM usage; only the benchmark harness's numbers are trustworthy. Saying that unprompted buys you enormous credibility.

**Suggested next rounds, in order:**

1. Architecture deep-dive — "walk me through what happens when you type 'send an email to Priya about the demo'" (end-to-end trace)
2. The hard technical questions — why LangGraph over a while loop, why tokenize before indexing, how the occlusion culler works, how you handle stale indices
3. Tradeoffs & pushback — "isn't browser automation fragile?", "why not just use the API?", "how do you know it works?"
4. Failure/debugging stories — the ones interviewers actually score you on
5. Scaling & production — multi-user, cost, what breaks at 100 users

---

### 6. Typed failure layer — in plain words

**The problem it solves:** most agents, when they go wrong, just... stop. Or spin forever. You look at the screen and think "is it thinking, or is it dead?" You can't tell, you can't count it, and you can't fix it.

**The rule I enforced:** every run that ends badly must end with a name for what went wrong, picked from a fixed list. No free-text "something failed." No silent stop.

That list is the `ErrorCode` enum — 13 codes, and nothing exits outside it:

| Group             | Codes                                                                              | Means                                                                                                     |
| ----------------- | ---------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Loop / perception | `STUCK`, `ACTION_TIMEOUT`, `REASONING_MISSING`, `MAX_STEPS`, `NO_ACTION` | The agent looped, hung, acted without thinking, ran out of budget, or didn't pick a tool                  |
| Human-in-the-loop | `APPROVAL_TIMEOUT`, `APPROVAL_REJECTED_NO_ALT`, `CONTEXT_INCOMPLETE`         | Waiting on a human that never came, rejected with no plan B, or never had enough info to start            |
| Honesty           | `SEND_UNVERIFIED`                                                                | Mail was dispatched but I couldn't confirm it. Not "sent", not "failed" — "I don't know, go check Sent." |
| Infrastructure    | `PROVIDER_EXHAUSTED`, `SURFACE_UNAVAILABLE`, `NOT_SIGNED_IN`                 | All three LLM providers rate-limited, browser gone, not logged in                                         |

**Why it actually matters (the three payoffs):**

1. It gives you a number. Because every ending is named, I can report "typed termination: 100%" on my benchmark. Without it, "success rate" is meaningless — half your failures aren't even countable.
2. It makes recovery possible. A named cause can be mapped to a fix. `STUCK` → scroll and retry. `PROVIDER_EXHAUSTED` → switch provider. `OFF_SCREEN` → widen the observation. That's what feeds the self-heal layer that offers you 4 ranked options. You can't rank remedies for "an error occurred."
3. It forces honesty. `SEND_UNVERIFIED` is my favourite one. The lazy design is to report success or failure. But sometimes you genuinely don't know — the click landed, the confirmation never came. Both answers would be a lie. So it gets its own code.

**The interview line:**

> "Typed failure means every bad ending has a name from a fixed list of 13. It sounds like bookkeeping, but it's the thing that turns an agent from a demo into something measurable. You can't have a reliability number if your failures are anonymous, and you can't auto-recover from a cause you never classified. Also — one small detail I'm proud of: it's a StrEnum, so the code serialises identically in the checkpoint, the event stream, and the log line. A failure code that renders differently on each side of the wire is a code you can't grep."

**If they push "isn't that just exception handling?"**

> Not quite. Exceptions tell you what threw. These tell you how the run ended — and most of these aren't exceptions at all. `STUCK` and `NO_ACTION` are perfectly healthy code paths where the agent is behaving correctly and still getting nowhere. Nothing throws. You have to go looking for them with guards.

### 7. What do you mean by surface-agnostic? (short, understandable answers)

- **Surface-agnostic = "doesn't know or care what website it's driving."**
- "Surface" = the thing the agent operates. Gmail's web UI is my surface. Outlook would be another. Jira, Salesforce, an internal admin panel — all surfaces.
- Surface-agnostic means that code works on any of them without changes, because it never mentions Gmail.

**Quick example**

Surface-agnostic (my funnel):

> "Drop anything invisible. Drop anything covered by something else. Number what's left."

That's true of every website ever built. Zero Gmail in it.

Surface-specific (my adapter):

> "The Subject box is `[name='subjectbox']`. Ctrl+Enter sends. When the compose dialog vanishes, the mail went out."

That's only true of Gmail.

**Why it's the whole point**

I drew a line — the `EmailSurface` port — and pushed all the Gmail knowledge below it:

```
graph · guards · funnel · PII vault · approval    ← agnostic, ~90%
────────────── EmailSurface port ──────────────
playwright_surface.py + extract.py               ← Gmail-only, ~10%
```

Everything above the line has zero Gmail references. I checked — the entire funnel is literally 0 mentions.

So adding Outlook = write one new class below the line. Nothing above it changes.

**The interview line:**

> "The valuable part of this project isn't the email agent — it's the machinery that's surface-agnostic. Turning a huge DOM into a short numbered list, catching loops, gating irreversible actions, tokenizing PII — none of that knows what Gmail is. Gmail is a slot I filled in. That was the deliberate bet in ADR-001: if I'd used the Gmail API, I'd have solved a narrower problem and thrown away everything reusable."

**One-liner if they want it blunt:**

> "The clever part is generic. The boring part is Gmail-specific. I made sure it split that way on purpose."

### 8. How the DOM → numbered list works — 7 steps

The raw idea: a real Gmail page has **thousands of DOM nodes**. A human only ever perceives ~30 things on it. The funnel mechanically reproduces that filtering — no LLM involved, all rule-based.

| # | Stage                      | What it throws away / does                                                                                                                                     |
| - | -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 | **Extract**          | Pulls DOM + accessibility roles/names + geometry + a screenshot                                                                                                |
| 2 | **Visibility**       | Drops anything hidden or scrolled off-screen — the single biggest cut, cheapest to do                                                                         |
| 3 | **Occlusion**        | Drops anything*covered* by something else — e.g. the inbox behind an open compose modal. Still technically "visible" by CSS, but a click there does nothing |
| 4 | **Wrapper collapse** | Real markup nests a clickable thing inside 5 useless`<div>`s. Collapses down to the one meaningful element                                                   |
| 5 | **PII tokenize**     | Every email/phone/name → a stable token (`alice@x.com → P17`). **Must run here, before numbering** — see below                                      |
| 6 | **SoM index**        | Assigns each remaining interactable a number`[12]`, `[27]`... Keeps the number→coordinate map on the server, never sent                                   |
| 7 | **Reading order**    | Serializes what's left in reading order, enforces a token budget, and**logs what got dropped** rather than silently cutting it                           |

**Two design details worth knowing cold:**

- **Occlusion is what makes modals "just work."** When a compose dialog opens, the inbox behind it is still valid DOM with real geometry — an agent that sees both layers will confidently click a row it can't actually reach. Culling the covered layer means the open dialog becomes *the only thing in the observation*, which is exactly what a human perceives. No special "a modal appeared" branch needed anywhere in the graph — you just re-observe.
- **PII tokenization is placed at stage 5, not earlier or later, on purpose.** It has to run *before* indexing/formatting so nothing downstream — logs, checkpoints, the wire — ever holds a raw address. There's a test that pins this exact stage order and fails loudly if anyone reorders it.

**Why it matters — 3 reasons:**

1. **Cost.** Feeding raw DOM to an LLM every turn would burn the free-tier budget in a handful of turns. A 30–50x token reduction is what makes a $0-cost agent possible at all.
2. **Accuracy.** A model drowning in irrelevant DOM makes worse decisions — same as a human staring at raw HTML instead of a rendered page. Filtering to only what's *actionable* is what makes the model reliable.
3. **Safety.** Because tokenization happens inside this same pipeline, before the model ever sees anything, PII protection isn't a separate bolt-on — it's structurally impossible to skip.

**Interview one-liner:**

> "It's seven single-purpose, unit-tested filters — visibility, occlusion, wrapper collapse, then tokenize, then number, then format. No LLM touches raw DOM at any point. It's the same filtering a human does unconsciously when they look at a webpage — I just made it explicit and mechanical, and it's why the model can afford to run on a free API tier."

### 9. Masking vs. hiding — is this the right architecture?

> Original question: "is it a good architecture to make everything invisible and do work by indexing and numbering or would it be better if we just mask the user details and imp info and other things visible? does that makes the agent work better and flexible and smart?"

**Two different axes, not one tradeoff**

- **Axis 1 — What gets hidden:** irrelevant DOM (invisible elements, occluded elements, off-screen junk). This is pure noise reduction for token budget. Nothing "important" is hidden here — a hidden element with `display:none` isn't information, it's clutter.
- **Axis 2 — What gets masked:** PII only. Email addresses, phone numbers, names → tokens.

Everything else stays fully visible as real text. Subject lines, dates, labels, button names, snippet previews — none of that is masked. The model reads:

```
[12] button "Archive"
[15] link "Q3 budget review — action needed by Friday" from P3
```

`P3` is masked (that's a person). Everything else is plain English.

So your instinct is actually already the architecture — mask only sensitive info, keep the rest visible. I'm not hiding "important info," I'm hiding noise (invisible/occluded DOM) and PII (privacy), and those are two different filters for two different reasons.

**The real question you might be asking: why numbers, not full descriptions?**

If the question is "why not let the model click by describing the element in full, instead of a bare `[12]`?" — that's the actual design decision, and here's why numbering wins:

| Approach                                             | Problem                                                                                                                                                                                                           |
| ---------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Model outputs a CSS selector                         | Hallucinates selectors that don't exist; brittle to markup changes                                                                                                                                                |
| Model outputs (x, y) coordinates                     | Coordinates drift on scroll/resize; no way to validate before clicking                                                                                                                                            |
| Model outputs free text ("click the archive button") | Ambiguous when there are 3 archive buttons; requires a fuzzy-match step that can pick the wrong one                                                                                                               |
| Model outputs an index`[12]`                       | Either it exists in this turn's list or it doesn't — instantly checkable, zero fuzzy matching, and the dispatcher rejects a stale/invalid number as a typed`STALE_INDEX` error instead of silently misclicking |

Numbering isn't about hiding things from the model — it's a safe addressing scheme. The label text next to `[12]` is still right there for the model to read and reason about. It's not blind.

**Would "keep everything visible" make it smarter? No** — and this is worth saying plainly in the interview:

- More raw text ≠ more intelligence. It's more tokens to pay for, more surface area to hallucinate over, and slower per turn. The model doesn't reason better with 5,000 tokens of DOM soup; it reasons worse, because signal-to-noise drops.
- This isn't a hunch — it's the standard finding behind Set-of-Marks-style prompting: giving a vision/UI model a small enumerated set of valid targets measurably beats asking it to freehand a coordinate or selector, because grounding failures (clicking the wrong/nonexistent thing) are the dominant failure mode in UI agents.
- Flexibility comes from what's on the list, not from hiding the list. The funnel already keeps everything actionable or informative on-screen — dates, subjects, senders, buttons. It only drops what's literally invisible/covered.

**The line to say:**

> "I don't hide important info — I mask PII and drop only what's invisible or covered. Everything else — subject lines, dates, senders, button labels — stays plain text. The numbering isn't for hiding things, it's a safe way for the model to point at something without hallucinating a coordinate or a selector. If anything, giving the model more raw, unfiltered DOM would make it less reliable, not smarter — more noise, more chances to misclick, higher cost. The intelligence lives in what the funnel decides is actionable, not in how much text you throw at the model."

**One caveat I'd volunteer if they probe further:** the reading-order formatter does drop lowest-priority items under budget pressure (off-screen, non-interactive) — but it logs a count every time (`droppedCount: 18`) rather than silently truncating, specifically so the model (and the human watching) knows something was cut and can ask to scroll/widen.

### 10. LLM fallback — how providers switch

> Original question: "how did you manage llm fallbacks and when do providers switch? also same provider fr a req or something else? breif ans in points"

**The chain**

- `FallbackLLMClient` wraps an ordered list: Groq → OpenRouter → Gemini
- All three are OpenAI-compatible, so it's the same client code, just different `base_url` + key
- Every call goes to Groq first, always — no load balancing, no round-robin

**Same provider or different, per request?**

- One request always resolves to one provider. No mid-request switching.
- Within that provider, it retries the same one first (up to `max_retries=2`) if the error looks recoverable — a 429 or a 5xx.
- Only if that provider is fully out does it move to the next one in the chain, for that same request.
- Next turn/request always restarts at Groq (unless Groq is "resting" — see below).

**When does it actually switch providers?**

It classifies the error type first — not every failure gets the same response:

| Error                                      | Same-provider retry?                 | Falls through to next provider?     |
| ------------------------------------------ | ------------------------------------ | ----------------------------------- |
| 429 rate limit                             | Yes — waits`Retry-After`, retries | Yes, if still failing after retries |
| 5xx / connection error                     | Yes — exponential backoff           | Yes                                 |
| Quota exhausted (daily cap)                | No — pointless to retry             | Yes, immediately                    |
| Auth error (bad key)                       | No                                   | Yes                                 |
| Bad request (malformed payload — our bug) | No                                   | No — raises immediately            |

That last one matters: if the request itself is broken, all 3 providers will reject it identically. Falling through anyway would burn 3 calls to hide our own bug behind a misleading "all providers exhausted." So that error type stops the chain instead of cascading.

**Backoff logic**

- If the provider says `Retry-After: 12s` → wait exactly that, don't guess
- If it doesn't say → exponential backoff, capped
- Guessing short = get rate-limited again; guessing long = stall a turn for nothing. Honor the real number when given.

**Cooldown (this is the clever bit)**

- A provider that just got rate-limited is benched for the stated duration — skipped entirely on the next request too, not just retried
- Otherwise every subsequent turn in the run would hit Groq, get a guaranteed 429, eat the full round-trip, and only then fall through — pure wasted latency, every single turn
- Only benched if it told us how long. An error with no stated duration isn't benched — guessing a ban length could drop a perfectly healthy provider for no reason
- A success clears the cooldown immediately — a daily cap can roll over mid-run, or a key can get topped up; don't stay benched on a stale timer

**Why this design (say if asked):**

> "Fallback only happens between attempts, never mid-attempt — no re-routing a half-finished request to a different model. And not every failure is treated the same: a rate limit is worth waiting for, a malformed request is our bug and should fail loud, not cascade through all three providers and come out the other side as a misleading 'everything is down.'"

### 11. What are the read-only verbs / the full verb list?

> Original question: "what is read-only verbs extract etc? what are the verbs? give short bullet point ans"

**The verb list — 22 actions the model can call**

Read-only verbs (no mailbox mutation):

- `Scroll` — move viewport up/down to reveal off-screen content
- `ReadThread` — open and read a full email thread
- `OpenFolder` — navigate to Inbox/Sent/Archive/etc.
- `Extract` — ask a targeted question about what's currently on screen (e.g. "what's in the To field?")
- `WaitFor` — pause N seconds for something to load/settle

Low-level input verbs (mechanical, not email-specific):

- `Click`, `Type`, `Clear`, `PressKey` — the raw building blocks; target by index, never by coordinate

Mutating email verbs (reversible):

- `Archive`, `MarkRead`, `Label`, `Snooze` — safe, no approval needed
- `DraftReply` — writes a draft, doesn't send

Mutating + irreversible (gated — approval required):

- `Send` — dispatches mail
- `DeleteForever` — permanent delete
- `ProposeEvent` — calendar invite path, gates on the actual invite send

Meta / control verbs (no browser action at all):

- `Remember` / `Recall` — scratchpad, so the agent doesn't need everything on screen at once
- `SetPlan` — post/revise the visible plan
- `AskUser` — trigger a context-gate question
- `Complete` — end the run with success/reason

**Why "read-only" got called out specifically**

- `Extract` and `Recall` are the two pure-read verbs — they observe/recall, they never touch the mailbox
- They used to be exempt from the repetition guard — the logic being "reads are harmless, don't count them as looping"
- That was wrong in practice: watched 4 identical `Extract("what's in the To field?")` calls in a row, invisible to the guard because of the exemption, each one a full LLM call against a provider that was already rate-limiting
- The lesson: "read-only" means "doesn't mutate the mailbox," not "free." On a free tier, the binding constraint is requests, not consequences — so now `Extract`/`Recall` count against the repetition guard like anything else
- What's still legitimately exempt: `Scroll`, `WaitFor`, `ReadThread`, `Observe` — because repeating those with the same args is still real progress (scroll twice = moved twice), unlike repeating an identical `Extract` question, which just returns the same answer again

**One-liner for the interview:**

> "22 verbs total — reads, low-level input, reversible mutations, gated irreversible ones, and meta verbs like Remember/AskUser/Complete. The interesting bug I found: I'd assumed read-only meant harmless, so I exempted Extract from the loop guard. Then I watched it call Extract four times in a row with the same question, each one a real LLM call, while a provider was already rate-limiting me. Read-only isn't free — it still costs a request. Fixed by removing the exemption."

### 12. How does the feedback loop work?

**Three loops, at three timescales**

**1. Per-turn: self-assessment**

- Before picking the next action, the model states whether its last action actually worked
- Outcomes: `PROGRESSED`, `NO_EFFECT`, `FAILED`, `UNKNOWN`
- `NO_EFFECT` is the important one — the click landed, no error, but the page didn't change. A plain success/fail flag can't see that; it's the #1 way agents waste a run silently
- Turns a silent failure into something the model reasons over on the very next turn, instead of discovering it several turns later when the stuck-guard finally fires

**2. Per-run: human correction**

- User says "no, not that one" mid-run → recorded as `Feedback(kind=CORRECTION)`, tagged to the run and the step
- Also captures `ENDORSEMENT` (human confirms an approach) and `REJECTION` (human declines a proposed send/irreversible action)
- Gets fed back into the loop as guidance on the next turn
- `applied` is tracked explicitly — feedback the loop never actually showed the model is worse than no feedback at all, because the user watches, sees nothing change, and concludes the agent ignores them. So it's a real flag, not an assumption.

**3. Across runs: promotion to a rule candidate**

- Same correction repeated 3+ times → proposed as a `RuleCandidate`, not auto-applied
- Matching is deliberately loose: lowercased, stopwords stripped, crude stemming (so "archiving" matches "archive"), word-order-independent — because nobody phrases the same complaint the same way twice
- Over-matching is the safe direction — worst case it proposes a rule the user declines
- Never auto-becomes a rule. The system proposes, a human confirms. A rule that changes behavior without explicit sign-off is unacceptable on a surface where behavior change means sending email.
- This is literally how the deterministic `RulesWorker` grows over time — rules get earned from repeated correction, not hand-configured

**Bonus: whole-run rating**

- Separate from the per-step signals — a human can rate an entire run once, after it ends
- Why it's separate: the model's own `Complete(success=True)` is the agent grading its own homework. A run rating is the only signal that judges the outcome, not a step — and it becomes eval ground truth
- Last rating wins if given twice (reconsidering counts)
- `None` (nobody rated) is kept distinguishable from a bad rating — conflating "no one said" with "someone said it was bad" would score every unattended run as a failure

**The interview line:**

> "Three loops, three timescales. Per-turn, the model grades its own last action before picking the next one — that's what catches 'my click landed but nothing happened,' which a plain success flag misses entirely. Per-run, a human correction gets replayed as guidance on the next turn, and I track whether it was actually shown to the model — because feedback that silently never reaches the model is worse than no feedback, the user just concludes it's being ignored. And across runs, if the same correction comes up three times, it's proposed as a candidate rule — never auto-applied, always a human confirms — which is how the deterministic, zero-LLM-cost path grows over time instead of staying hand-configured forever."

### 13. Indexing vs. coordinates — who sees what?

> Original question: "what is indexing and coordinate whee they use?"

**Indexing (Set-of-Marks) — what the model sees**

- Every actionable/readable element on screen gets a small integer: `[1]`, `[2]`, `[12]`...
- Assigned in reading order (top-to-bottom, left-to-right) over whatever survived the funnel
- The model's entire vocabulary for "where" is these integers. It says `Click(12)` — never a pixel, never a selector
- Indices are per-turn, never reused. Page changes → re-observe → renumber from 1. A number from last turn means nothing this turn.

**Coordinates — where they actually live**

- The model never sees a coordinate. Ever.
- When SoM assigns `[12]`, it also silently records `12 → (x, y)` — the element's centre point — in a hidden map
- That map stays server-side, in the executor. It's built during indexing, but it never gets serialized into the `Observation` sent to the model
- Only at dispatch time — after the model has already committed to "click 12" — does the executor look up 12 in that map, get the real (x, y), and fire `page.mouse.click(x, y)` via CDP `Input.*` (trusted, `isTrusted: true`, not a JS `.click()`)

**Why the split matters (the actual safety property)**

- The model can't hallucinate a coordinate — it was never given one to imitate
- The model can't target something it wasn't shown — the only valid numbers are the ones minted this turn
- A stale index (from a page that has since changed) fails loud at dispatch — `STALE_INDEX` typed error — instead of silently clicking whatever now happens to sit at that old coordinate
- This is also why indexing and coordinates are cleanly separated in code: `SoMIndexer` (funnel, stage 6) builds `index → geometry`; the dispatcher (`dispatch.py`) is the only place that ever reads it back

**Interview one-liner:**

> "Indexing is what the model sees — a small integer per element, reassigned fresh every turn. Coordinates are what the executor uses to actually click, and they never leave the server. SoM builds a hidden index → (x,y) map when it numbers the page; the model only ever picks the number. At dispatch, the executor resolves that number back to real pixels and fires a trusted CDP click — not a JS .click(). So there's no path where the model can name, guess, or be tricked into a coordinate — it literally never held one."

### 14. Does re-indexing every turn slow the system down?

> Original question: "since the coordinates and indexing are fresh every turn and updating, does it make the sys slow? is it good?"

**Short answer: yes, a little — but it's not the bottleneck, and it's non-negotiable anyway.**

**Where the actual time goes, per turn:**

- Settle wait: 0.25s–3s (waits for the page to go quiet before reading it — bounded floor/ceiling, not adaptive-per-host yet, that's a known fast-follow)
- Extract + funnel (DOM read → 7 filter stages → renumber): fast, this is in-process JS + Python, not network — a few hundred ms at most
- The LLM call: this is the actual bottleneck, by far — hundreds of ms to a few seconds depending on provider

So re-indexing itself is cheap. The LLM round-trip dwarfs it. Renumbering 50–200 elements is not where your latency budget goes.

**Is it good design? Yes** — because the alternative is worse in two ways:

**1. Reusing old indices would be actively unsafe, not just stale.**

- The page changes between turns — an email gets archived, a modal opens, the list scrolls
- A cached "index 12 = that button" from last turn can silently now point at something else entirely
- That's not a performance optimization, that's a misclick waiting to happen — the exact failure mode Set-of-Marks exists to prevent

**2. "Settle before reading" is what re-indexing actually buys you, and skipping it costs more than it saves.**

- The comment in the code is blunt about this: "Reading a half-rendered page produces an element list that is wrong in the most expensive way: plausible."
- A stale read doesn't crash — it just quietly hands the model a slightly-wrong list, the model acts on it, the action lands somewhere unintended, and the failure only surfaces turns later with no obvious cause
- That's far more expensive than the 0.25–3s wait: a debugging session vs. a bounded delay

**The honest tradeoff, framed properly:**

- Correctness costs a bounded settle wait per turn (capped at 3s)
- I chose a floor/ceiling instead of a fixed sleep, so fast pages aren't punished — most Gmail actions settle near the floor
- I have not yet built the adaptive per-host version noted in my own design doc — right now it's a fixed bound, not learned. I'd say that plainly if asked.

**Interview line:**

> "It costs something — a bounded 0.25 to 3 second settle before every read — but that's dwarfed by the LLM call itself, which is genuinely the slow part. And it's not really optional: reusing a stale index isn't a shortcut, it's a misclick, because the page has moved on. I'd rather pay a few hundred milliseconds of settle time than have the agent confidently click the wrong thing and have that failure surface three turns later with no obvious cause. The one thing I haven't built yet is making that settle window adaptive per-site instead of a fixed floor/ceiling — that's a known next step, not done."

### 15. Is any of this just DSA under the hood?

> Original question: "does thid indexing and reading the components relatable to any data struc dsa part?"

Yes — a lot of it is classic DSA, just wearing an "AI agent" costume. Good angle to bring up in an interview, it shows you understand what you built underneath the LLM parts.

**Direct mappings:**

| System piece                                                                           | DSA concept                                                                                                                                                                                |
| -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| index → geometry map (SoM)                                                            | Hash map / lookup table — O(1) resolve at dispatch                                                                                                                                        |
| Reading-order sort`(round(y/ROW_BAND), x)`                                           | Sorting with a custom key + bucketing/quantization — y is binned into row-bands first so text baselines a few px apart don't get treated as different rows, then sorted within the bucket |
| Occlusion culling (is element A covered by element B painted later?)                   | Computational geometry — axis-aligned bounding box (AABB) overlap test, plus a painter's-algorithm idea (later-painted = on top, so z-order matters, not just position)                   |
| Wrapper collapse (5 nested useless`<div>`s → 1 meaningful element)                  | Tree compression — same idea as collapsing unary chains in a parse tree, or path compression in union-find: skip nodes that add no information                                            |
| Token budget / drop lowest-priority first                                              | Greedy eviction — like an LRU cache dropping least-valuable entries under a size cap, prioritized rather than FIFO                                                                        |
| Repetition guard (action, target, args → hash, rolling window)                        | Sliding window + hashing — same pattern as cycle detection in a state graph (transposition tables in game engines, or detecting a repeated state in BFS/DFS search)                       |
| Page-signature stuck detector (hash the page, compare to last N)                       | Content fingerprinting — like a checksum/Merkle-hash diff: "did the state actually change," without comparing full content                                                                |
| PII tokenizer (`alice@x.com → P17`, stable per session)                             | String interning / symbol table — same concept a compiler uses to map identifiers to symbol IDs once, then reuse the ID                                                                   |
| Fallback LLM chain + per-provider cooldown map                                         | Linked retry chain + hash map of provider → available_at_timestamp — conceptually a circuit breaker pattern                                                                              |
| Feedback promotion (`Counter(normalise(text))`, stemmed, sorted set of words as key) | Frequency counting via hash map — literally the "group anagrams" pattern: canonicalize each string to a sorted/stemmed key, count collisions                                              |

**The one-liner for the interview:**

> "A lot of the 'AI' parts of this project are actually plain DSA with an LLM on top. Set-of-Marks is a hash map. The repetition guard is a sliding window with hashed state signatures — the same trick as cycle detection in graph search. Wrapper collapsing is tree compression. Feedback promotion is frequency-counting canonicalized strings, same shape as grouping anagrams. The model only ever touches the last 5%, judgment calls — everything before that is deterministic, testable data-structure work, which is exactly why I could unit-test each funnel stage without a browser or an LLM in the loop."

**Why this is a good thing to say, not just trivia:**

- It proves you know why it's fast and testable — none of that pipeline needs an LLM call to verify
- It reframes "I built an AI agent" as "I built a deterministic pipeline with a narrow, well-scoped LLM decision point at the end" — which is exactly the kind of engineering judgment a startup wants to hear

### 16. The router — linear vs. decision

> Original question: "what is the route part like linear, decision? how it happens? what is this? why imp?"

**What it is**

- A node that runs right after intake (task parsed into an `Intent`), before any worker is dispatched
- Its only job: pick which topology the task should run under — a straight-line script, or the full observe→reason→act loop
- Output is a `Route(topology, why)` — not a worker choice, a shape choice

**The two topologies**

|               | Linear                                                      | Decision                                                    |
| ------------- | ----------------------------------------------------------- | ----------------------------------------------------------- |
| What it means | Fixed, deterministic sequence of steps, no perception loop  | Full observe → reason → act loop, re-evaluates every turn |
| Example task  | "archive all newsletters", "mark everything from X as read" | "reply to the ones that actually need me"                   |
| LLM calls     | Can be zero                                                 | One reasoning call per turn                                 |
| Used by       | `RulesWorker`                                             | `TriageWorker`, `ComposeWorker`, `CalendarWorker`     |

**How the decision actually happens — 3 layers, cheapest first**

**1. Rule match (free, checked first)**

- If a deterministic user rule matches the task text, route to linear immediately
- Zero LLM calls. This is the "cheapest correct path" principle in code — don't spend a request on something you already know the answer to

**2. Classifier call (fallback)**

- If no rule matches, one small/cheap LLM call classifies the task text as linear or decision
- This is a classifier-role model — deliberately the cheap tier, not the big reasoning model

**3. Clamp against reality (the important safety net)**

- Whatever the rule or the classifier said gets checked against a hard fact: can this action's worker actually run linearly?
- `topology_for(action, requested)` — if the action is something like Compose and it was routed "linear," it gets overridden back to decision, because a linear path has no perception loop and literally can't find the Compose button
- This existed because it actually happened: the classifier called "write a good evening mail to P1" linear, it ran blind, and failed as `NO_ACTION` — a failure that looked like the model's fault but was actually the router's

**Why this clamp step matters (the real lesson):**

> "Whether a worker can run blind is a fact about that worker, not a judgment call for a classifier to argue about." — so it's enforced in code, not re-prompted

**Why the whole router matters**

- **Cost.** A linear route can run at literally zero LLM cost — critical on a free-tier budget. Routing everything through the reasoning loop would burn quota on tasks that don't need judgment at all.
- **Correctness.** Deterministic tasks get deterministic execution — no risk of the model improvising on "archive everything from X," which should never require judgment.
- **Safety net for a fallible classifier.** The router can be wrong (it's an LLM call), so the system doesn't trust it blindly — it clamps against what the worker can actually do, so a misclassification degrades to "ran the more careful path" instead of "crashed and blamed the model."

**Interview line:**

> "The router picks the shape of execution, not the worker. Deterministic tasks — archive, label, mark-read — run linear, which can cost zero LLM calls if a user rule already matches. Anything needing judgment per email runs the full observe-reason-act loop. The interesting part isn't the classification, it's the clamp after it: I don't trust the classifier's answer blindly, I check it against a fact — can this action's worker actually operate without a perception loop? I added that after watching a compose task get routed linear, run blind with no way to find the Compose button, and fail with a NO_ACTION code that looked like a model failure but was actually a routing bug."

### 17. What exactly does the "clamp" do?

> Original question: "so what does clamp do? what it checks and do properly? and how? after the router's decision of topology, it checks the rules given whether matched or not, if not then decision? or is it that it does the task and then checks?"

To answer the exact question directly: **no execution happens before the check.** It's a pure lookup, zero browser/LLM cost, run entirely inside the routing step before any worker starts.

**The exact sequence, in order:**

```
1. Rule store checked           (task text vs deterministic rules — zero LLM)
   │
   ├─ matched → propose "linear"
   └─ no match → ask classifier LLM → propose "linear" or "decision"
                 (one small LLM call)
   │
   ▼
2. CLAMP  =  topology_for(action, proposed)
   │
   ▼
3. Route(topology) is set → THEN, and only then, the worker starts
```

The clamp sits between "something proposed a topology" and "a worker actually starts." No task work has happened yet at that point — no browser opened, no observation taken.

**What the clamp checks — literally:**

```python
def topology_for(action, requested):
    if requested != "linear":
        return requested                          # "decision" always passes through untouched
    return "linear" if worker_for(action).supports_linear else "decision"
```

- If the proposal was "decision" → clamp does nothing, just passes it through. Decision mode has the full perception loop, so it can handle any action safely — nothing to check.
- If the proposal was "linear" → clamp looks up: which worker handles this action, and does that worker class have `supports_linear=True`?
- That's a static flag on a `WorkerSpec`, hardcoded per worker, not computed from the task at runtime:
  - `TRIAGE` → `supports_linear=True` (archive/label/snooze genuinely need no screen — the rule text is the whole instruction)
  - `COMPOSE`, `CALENDAR`, `QUERY` → `supports_linear=False` (default) — they need to actually look at the screen to work
- If the flag is `True` → topology stays "linear"
- If `False` → topology gets overridden to "decision"

So it's a single dictionary lookup + one boolean check. No task execution, no re-doing anything.

**Answering your exact question — "does it check the rules first, then decide? or does it do the task and then check?"**

- Rules are checked first, yes — but that's step 1, separate from the clamp.
- The clamp is not "do the task, then verify." It never runs the task at all. It's a pre-flight sanity check on a proposal, using a fact that's already known in advance (which worker owns this action, and whether that worker class can operate blind) — before the worker is even dispatched.
- Think of it less as "verify after" and more as "the router proposes, the clamp vetoes if the proposal is physically impossible."

**Why it has to be this order (not "try it and see"):**

If you let it "just try" linear on a Compose task, here's what actually happens: the linear worker has no observe step, so it never sees a screen, never finds a Compose button, calls no tool, and the loop terminates as `NO_ACTION` — a real wasted run, with a failure code that looks like "the model didn't know what to do" when the actual bug was routing. The clamp exists specifically to catch that before any of it runs, for free.

**One-liner:**

> "The clamp is a static lookup, not a runtime check — it never lets the task run to find out. Rules and the classifier both just propose a topology; the clamp asks one factual question — can this action's worker physically operate with no perception loop? — using a hardcoded flag on the worker, and overrides to decision if the answer is no. It costs nothing, because the fact was already known before the task started."

### 18. What rules does the system ship with?

> Original question: "what are the rules stated to follow? give ans in bullet points"

**What a "Rule" is (structure):**

- `name` — human-readable id
- `patterns` — regex patterns matched case-insensitively against the task text
- `actions` — the verbs a linear worker runs when matched (e.g. `Archive`, `MarkRead`)
- `intents` — which task intents this rule applies to (empty = any)
- `auto_send` — whether this rule is allowed to send mail without human approval (off by default)
- `enabled` — on/off switch

**The 3 default rules shipped:**

- `newsletters` — matches "newsletter", "promotions", "marketing" → `Archive`
- `notifications` — matches "notifications@", "no-reply", "automated" → `Archive` + `MarkRead`
- `mark-read-from` — matches "mark all/everything ... read" → `MarkRead`

**How matching works:**

- Task text checked against each active rule's regex patterns
- First match wins, not best match — precedence is something a user can see and reorder, not a hidden scoring function
- A match short-circuits routing entirely → runs linear, zero LLM calls

**The auto-send safety rule (the important one):**

- Auto-send is off by default and cannot be turned on by one flag alone — needs two separate opt-ins:
  1. The individual rule sets `auto_send=True`
  2. The store itself is constructed with `allow_auto_send=True`
- If the store-level flag isn't set, `auto_send` gets stripped off every rule automatically, even if the rule itself asked for it
- Why two locks: "one accidental default must not be enough" — a rule that sends mail without a human is the one path that would bypass the approval gate, so it can't be a single config toggle

**Bigger governance rule (from ADR-006 / product principles):**

- Rules are never silently learned or auto-created — see the feedback promotion loop: repeated corrections become a candidate, a human must confirm it before it becomes a real rule
- Nothing gains capability without a person explicitly saying yes

**One-liner:**

> "Three default rules ship — newsletter/promo archiving, no-reply notification cleanup, and mark-all-read. They're plain regex-and-action pairs, deliberately dumb and inspectable rather than learned, so a user can read exactly why a rule fired. The one rule I was strict about is auto-send: it needs two independent opt-ins, on the rule and on the store, because that's the single path that could bypass the human-approval gate — and I didn't want that reachable by one flag flip."

### 19. What operating rules does the whole system follow?

> Original question: "and what rules does the sys follows? like dont start anything before having 100% context, router to decide, etc etc, make a bullet point ans for that"

**The system's own operating rules — the architecture guardrails**

**1. Won't start without full context**

- Context gate (100% rule) — before any mailbox mutation, required slots for the action must be filled (e.g. `send_email` needs recipient + topic)
- Missing/ambiguous slot → asks the human first, loops until confident
- Read-only observation is allowed here (to resolve "which contact named X?") — but no mutation until gate clears

**2. Route by shape before doing anything**

- Router classifies the task as linear (deterministic, scripted) or decision (needs per-step judgment)
- Rule match → linear, zero LLM calls, checked first (cheapest path)
- Classifier fallback if no rule matches
- Clamped against a hard fact afterward: can this action's worker even run without perception? Overrides if not — the router's guess never overrides reality

**3. Nothing irreversible without a human**

- Send, delete-forever, invite-dispatch, bulk destructive ops → always pause for approval
- Structural, not a prompt instruction — it's a node in the graph, provably unskippable
- No config flag turns this off in v1

**4. The model never sees raw data**

- Real DOM never reaches the LLM — only the funneled, numbered observation
- Real PII never reaches the LLM — tokenized before indexing, resolved only at dispatch
- Keys stay server-side, never touch the model or the wire

**5. Every failure ends with a name, never silence**

- All bad endings land on one of 13 typed `ErrorCode`s
- HITL is not a fallback for "I'm confused" — it's used for exactly 3 things: asking for missing context, approval on irreversible actions, and ranked recovery options
- "Ask the human when stuck" is banned as a design — it destroys the ability to measure reliability

**6. Re-observe every turn, trust nothing stale**

- Indices/numbers are rebuilt from scratch every turn, never reused
- A stale reference is rejected at dispatch (`STALE_INDEX`), never silently acted on

**7. Cheapest correct path first**

- If a task is deterministic, don't spend an LLM call on it
- Rule check → then classifier → then full reasoning loop, in that cost order

**8. Fail loud on the system's own bugs, don't paper over them**

- A malformed request (our bug) is not retried across all 3 providers — that would hide the real bug behind a misleading "everything is down"
- Dropped/hidden observation items are always logged as a count, never silently cut

**9. No self-modification**

- Self-heal reads a curated, human-approved skill registry
- It does not edit its own running source — unbounded blast radius, explicitly out of scope

**10. Learning requires explicit human sign-off**

- Repeated corrections become a candidate rule, never an automatic one
- A rule that can auto-send needs two separate opt-ins (rule-level + store-level) — one flag is never enough for the one path that could bypass approval

**11. Show the work**

- Every reasoning step, action, and observation streams live to the user
- If the human can't see why it did something, that's treated as a bug in itself

**One-liner for the interview:**

> "The whole system is basically five non-negotiables enforced as graph structure, not prompt text: don't start without full context, never mutate irreversibly without a human, never let the model see raw data, never end a run without a typed reason, and never trust a stale reference. Each one exists because I watched the version without it fail in a specific, reproducible way."
