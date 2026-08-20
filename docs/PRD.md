# PRD — Inbox Autopilot

- **Status:** Draft v1 · **Owner:** principal engineering · **Build window:** 1 week to demo, then harden
- **Cost target:** $0 (free tiers only) · **Companion docs:** [`SYSTEM-DESIGN.md`](SYSTEM-DESIGN.md), [`ENGINEERING-SPEC.md`](ENGINEERING-SPEC.md)

---

## 1. Summary

Inbox Autopilot is an **email agent that operates Gmail through a real browser**. You type a task in
plain English — *"send an email to Priya about the Friday demo"*, *"clear my inbox of newsletters"*,
*"reply to the ones that actually need me"* — and you watch it happen: the left pane is the
conversation, the right pane is the live browser screen where it clicks Compose, fills the recipient,
writes the subject and body, and stops at Send to ask you.

Three things make it different from a scripted Gmail macro or a naive "LLM + Gmail API" wrapper:

1. **It refuses to start half-informed.** If the task is missing a slot the action requires, it asks
   before it touches the mailbox. Nothing mutates until context is complete.
2. **The model never sees your data.** Email addresses, phone numbers, and personal names are
   replaced with stable tokens (`alice@corp.com → P17`) *before* anything leaves the machine holding
   the DOM. The model reasons over tokens; the executor resolves them only at dispatch.
3. **It recovers instead of dying.** Every failure lands on a typed error code. The system classifies
   the root cause, consults a curated skill registry, and hands you four ranked options — one marked
   Recommended, and a fourth where you just type what you want instead.

## 2. Problem

Email is the highest-volume, lowest-leverage surface most knowledge workers touch. The backlog is
mostly noise that follows obvious rules, punctuated by a handful of messages that genuinely need a
human. Existing tools fail in one of three ways:

| Approach | Why it fails |
| --- | --- |
| Gmail filters / rules | Deterministic only. Cannot read intent, cannot draft, cannot judge "does this need me?" |
| API-based LLM assistants | Require OAuth scopes users won't grant, break on anything the API doesn't expose, and ship your entire mailbox to a model provider. |
| Generic browser agents | Have no email semantics, no approval model, and will happily send a half-written email to the wrong person. |

The gap is an agent with **real UI reach** (it can do anything you can do in Gmail), **email
semantics** (archive/label/snooze/draft/extract-event are first-class verbs), and a **trust model**
strong enough that you'd actually let it near your inbox.

## 3. Users and jobs to be done

| Persona | Job | Success looks like |
| --- | --- | --- |
| **Backlog drowner** — 200+ unread, mostly noise | "Make my inbox small enough to think about" | One triage run archives/labels the obvious, surfaces 5 things that need a decision |
| **Reluctant correspondent** — hates writing routine mail | "Write this for me, but let me check it" | Types one sentence, watches a draft assemble live, edits a word, approves |
| **Meeting-scheduler** — threads that end in "let's find time" | "Turn this thread into a calendar event" | Agent extracts date/time/attendees from the thread, drafts the event, gates the invite |
| **Rule-haver** — knows exactly what should happen | "Just do the thing, don't think about it" | Deterministic route runs with **zero** LLM calls; fast and free |

**Non-user (explicit):** this is not a bulk-mail or outreach tool. Rate-limited, human-gated,
single-mailbox by design.

## 4. Product principles

1. **The human holds the trigger.** Anything irreversible — Send, delete-forever, invite dispatch,
   bulk destructive ops — pauses for approval. There is no configuration that turns this off in v1.
2. **Show the work.** Every reasoning step, every action, every observation is streamed. If the user
   cannot see why it did something, it did the wrong thing.
3. **Ask early, not late.** A question before the run costs seconds. A wrong email costs a
   relationship.
4. **Tokens, not names.** The model's ignorance of real identifiers is a feature we can demonstrate,
   not a claim we make.
5. **Fail typed, never silent.** Every terminal state carries an `ErrorCode`. "It just stopped" is a
   bug.
6. **Cheapest correct path.** If a task is deterministic, don't spend an LLM call on it.

## 5. Scope

### In scope for v1

- Gmail web UI via a **server-side Playwright/CDP browser** (default; dev, CI, demo) **and** the
  **user's own Chrome via a bridge extension** (the real product shape). Both behind one port.
- Task intents: `send_email`, `reply`, `triage`, `label`, `archive`, `snooze`, `search`,
  `extract_event`, `apply_rules`.
- Two-pane cockpit (Next.js) with live browser frames, streamed reasoning, questions, ranked options,
  and approval cards.
- Three human-in-the-loop interrupts: **AskUser** (context), **Approval** (irreversible), **Options**
  (self-heal).
- PII tokenization of addresses + phones (deterministic) and personal names (best-effort).
- Free multi-provider LLM gateway with automatic fallback.
- Typed failure layer, repetition/stuck guards, per-action timeout walls.
- Benchmark harness over fixture mailboxes.

### Out of scope for v1 (stated, not forgotten)

| Deferred | Why | Where it goes |
| --- | --- | --- |
| Multi-account / multi-provider mail (Outlook, IMAP) | The `EmailSurface` port makes it additive later | v2 |
| Agent editing its own running source | Unbounded blast radius — see [ADR-009](ADR.md#adr-009) | v1.1 as a sandboxed, human-reviewed dev-assist mode |
| Autonomous send (no human) | Violates principle 1 | Not planned |
| Attachment authoring / file uploads | Adds a filesystem trust boundary | v2 |
| Mobile cockpit | Desktop-first; layout is responsive but untested on mobile | v2 |
| Team / shared inboxes, RBAC | Single-user product first | v2 |

## 6. Requirements → acceptance criteria

Each requirement gets an ID used throughout the other docs.

### R1 — Two-pane cockpit

**Requirement.** LHS: chat, run history, questions, ranked options. RHS: the live email screen —
typing visible as it happens, current action labelled.

**Acceptance.**
- [ ] RHS renders streamed browser frames at ≥2 fps during an active run.
- [ ] LHS shows, in order: task → plan → per-turn reasoning → tool call line → result.
- [ ] A cockpit refresh mid-run **re-attaches** to the live run and replays its history; the run and
      its browser survive the disconnect.
- [ ] The action currently executing is labelled on the RHS (e.g. `type → subject field`).

### R2 — "send email on \<topic\> to \<recipient\>" as a ReAct loop with confirmation

**Requirement.** One sentence produces a complete drafted email, assembled step by step in the live
view, then asks for confirmation and acts on the answer.

**Acceptance.**
- [ ] The agent clicks Compose, fills To / Subject / Body as **separate observable steps** — not one
      atomic jump.
- [ ] Before Send it raises an **approval interrupt** and the cockpit renders the fully composed
      draft (resolved, human-readable).
- [ ] Three decisions are supported and behave correctly: **Approve** (sends), **Edit** (user's text
      replaces the field, loop continues), **Reject** (no send; agent offers an alternative or
      terminates with a typed reason).
- [ ] With the approval pending, **no** send-shaped action can execute. Verified by test, not by
      inspection.

### R3 — Will not start without 100% context

**Requirement.** If the agent lacks any information the task requires, it asks instead of guessing.

**Acceptance.**
- [ ] Each intent declares a **required-slots schema**; `context_gate` computes `missing_slots` and a
      confidence score.
- [ ] Any missing or ambiguous slot raises an `AskUser` interrupt; the run is durably paused (survives
      a process restart via the checkpointer).
- [ ] Ambiguity resolution may perform **read-only** observation (e.g. to list the three contacts
      matching "Priya") but MUST NOT mutate the mailbox.
- [ ] The gate loops until confidence ≥ threshold; a test proves no worker dispatch occurs before it
      clears.

### R4 — Self-healing with root cause and four ranked options

**Requirement.** On failure the agent determines the root cause, loads skills, and proposes fixes as
4 options: `[1]` Recommended, `[2]`, `[3]`, `[4]` Other (free-form), with a human choosing.

**Acceptance.**
- [ ] Every typed failure maps to a `Cause` via a pure, unit-tested classifier.
- [ ] `SkillRegistry.strategies_for(cause)` returns scored `RemediationStrategy` objects; the top
      three become options 1–3 with option 1 explicitly marked Recommended.
- [ ] Option 4 always exists and accepts free text, which is folded back into the loop as guidance.
- [ ] Choosing a remedy re-enters the loop; a second failure of the **same** cause escalates rather
      than looping (no infinite remediation).
- [ ] The diagnosis shown to the user names the cause in plain language, not an enum.

> **Scope note on "see codebase and try to fix".** v1 self-heals at the *task* level from a curated
> registry. Reading and patching its own source is real, wanted, and **deliberately separated** into a
> sandboxed dev-assist mode that proposes a diff + tests for human review and never runs in the live
> loop. See [ADR-009](ADR.md#adr-009) for the full reasoning and the v1.1 design.

### R5 — Manager workflow / proper AI workflow

**Requirement.** A real supervisor architecture, not a `while` loop with prompts.

**Acceptance.**
- [ ] Orchestration is an explicit LangGraph `StateGraph`; there is no agent `while` loop in the
      codebase.
- [ ] A supervisor dispatches to worker **subgraphs**; adding a worker is adding a class + one
      registration line, with zero edits to the supervisor.
- [ ] All routing decisions are **pure functions** with unit tests covering every branch.
- [ ] Graph state is the single source of truth; no mutable agent state lives outside `AgentState`.

### R6 — Linear vs decision routing

**Requirement.** Classify the task's execution topology and route accordingly.

**Acceptance.**
- [ ] A `router` node emits a typed `Route ∈ {linear, decision}`.
- [ ] `linear` tasks execute with **zero** per-step LLM calls (measured in the trajectory).
- [ ] A deterministic pre-check (rule match) short-circuits the classifier LLM call entirely when the
      task matches a known rule.
- [ ] Misrouting is recoverable: a linear worker that hits ambiguity escalates to the decision path
      rather than failing.

### R7 — No raw data to the AI

**Requirement.** Email IDs, addresses, and personal data never reach the model.

**Acceptance.**
- [ ] `PiiTokenizer` runs **inside the funnel, before serialization**, so no downstream stage ever
      holds raw PII.
- [ ] A test asserts that for a fixture inbox containing known addresses, **zero** raw addresses
      appear in: the `Observation` payload, the LLM request body, the trajectory store, the event
      stream, or the logs.
- [ ] Token→real resolution happens only in the executor, only at action dispatch.
- [ ] Tokens are stable within a session and are **not** reused across sessions.

### R8 — Real-time visibility

**Requirement.** The user sees what is happening as it happens.

**Acceptance.**
- [ ] The graph is driven with `astream(stream_mode="updates")`; every node update is forwarded to the
      `EventSink`.
- [ ] Reasoning streams token-by-token, not as a post-hoc block.
- [ ] Median cockpit latency from action execution to visible frame < 1s on a local run.
- [ ] Nothing is rendered from client-side inference — the cockpit displays only what the backend
      emitted.

### R9 — Guardrails

**Requirement.** Proper guardrails throughout.

**Acceptance.** The full list lives in [`ENGINEERING-SPEC.md §Guardrails`](ENGINEERING-SPEC.md) and
[`SECURITY-MODEL.md`](SECURITY-MODEL.md). The PRD-level bar:
- [ ] 100% of terminal states carry an `ErrorCode` (measured by the benchmark harness).
- [ ] No irreversible action path exists that bypasses an approval interrupt (proven by test).
- [ ] Provider keys are unreachable from the frontend and the extension (proven by build inspection).
- [ ] Every mutating action logs enough to undo it.

## 7. User journeys

### J1 — Compose with a missing slot (R2 + R3)

```
User:  "send an email to priya about the friday demo"
Agent: [intake]  action=send_email  targets=[Priya]  topic="friday demo"
       [context_gate] slot `recipient_identity` ambiguous → read-only observe → 2 matches
       ❓ "I found two contacts named Priya — P17 (last mailed Tuesday) or P43? Also, roughly
          what should the email say beyond 'the Friday demo'?"
User:  "the first one. tell him the demo moved to 4pm and ask if that works."
Agent: [router] decision → [planner] 5 steps posted to LHS
       RHS: Compose opens · To fills · Subject fills · Body types out live
       ⏸ APPROVAL — cockpit renders the resolved draft (real name and address shown to the human)
User:  [Edit] changes "moved to 4pm" → "moved to 4pm IST"  → [Approve]
Agent: clicks Send · [verify] confirms the message is in Sent · ✅ done
```

### J2 — Triage with a rules short-circuit (R6)

```
User:  "clear out the newsletters and anything from notifications@"
Agent: [intake] action=triage · [router] rule match → LINEAR (no classifier call)
       RulesWorker: 34 matches · archive each · 0 LLM calls · 11s
       ✅ "Archived 34. Left 6 that matched no rule — want me to look at those?"
```

### J3 — Failure into ranked options (R4)

```
Agent: click Compose[12] → ACTION_TIMEOUT ×2 → verify fail
       [diagnose] cause = OVERLAY_BLOCKING (an unchanged page signature + a new modal in the diff)
       💡 "A Gmail dialog is covering the Compose button — that's why my clicks aren't landing."
          [1] Dismiss the dialog and retry Compose   ← Recommended
          [2] Reload Gmail and start the compose over
          [3] Use the keyboard shortcut `c` instead of clicking
          [4] Other — tell me what to do
User:  [1]
Agent: dismiss-overlay strategy → retry → compose opens → loop resumes
```

## 8. Success metrics

| Metric | Target (v1) | Measured by |
| --- | --- | --- |
| Task success rate on the fixture suite | ≥ 80% | benchmark harness |
| Terminal states carrying a typed `ErrorCode` | **100%** | benchmark harness |
| Unauthorized sends | **0** | approval-bypass test suite |
| Raw PII leaks across the wire | **0** | PII leak test suite |
| Median steps per compose task | ≤ 8 | trajectory store |
| LLM calls on a linear task | **0** | trajectory store |
| Cockpit action→frame latency (p50, local) | < 1s | instrumented run |
| Dollar cost | **$0** | provider dashboards |

## 9. Risks

| # | Risk | Severity | Mitigation |
| --- | --- | --- | --- |
| 1 | **Prompt injection via email bodies.** Message content is attacker-controlled text entering the model's context. | **Critical** | Approval gates on every irreversible verb; instruction/content separation in the prompt; content is framed as untrusted data; no verb can be invoked by text the agent merely *read*. See [`SECURITY-MODEL.md`](SECURITY-MODEL.md). |
| 2 | **Free-tier rate limits** starve a long triage run. | High | Groq primary (highest free throughput); batched classification (one call scores N subjects); linear route uses zero calls; automatic fallback chain; prompt caching on a stable prefix. |
| 3 | **Gmail UI churn** breaks the funnel's assumptions. | High | The funnel is generic (roles + geometry), not selector-based. Failures land as typed codes and route into self-heal rather than crashing. |
| 4 | **Google automation defenses** flag the session. | High | Headful + stealth by default; the extension surface drives the user's *own* logged-in Chrome with a real profile; dev/CI uses a dedicated fixture account. See [ADR-010](ADR.md#adr-010). |
| 5 | **Wrong recipient on a send.** | **Critical** | Recipient is a token end-to-end; the approval card renders the *resolved* address for the human to check; verify step confirms the Sent entry matches the intended token. |
| 6 | **Name tokenization is fuzzy** — a missed name leaks. | Medium | Addresses and phones are deterministic and MUST be complete; names are best-effort and scoped as such in the PRD, not overclaimed. Leak tests assert the deterministic classes at 100%. |
| 7 | **Runaway loops** burn quota. | Medium | Repetition guard (nudge at 3, kill at 5), stuck-signature detection, per-action timeout wall, max-steps budget with an end-of-budget "report findings now" injection. |

## 10. Open questions

| # | Question | Needed by | Default if unanswered |
| --- | --- | --- | --- |
| Q1 | Fixture Gmail account for dev/CI — dedicated test account, or recorded-DOM fixtures only? | M1 | Recorded-DOM fixtures for unit/CI; one dedicated account for integration, run manually. |
| Q2 | Name tokenization depth in v1 — headers only, or bodies too? | M1 | Headers + sender/recipient chips deterministically; body names best-effort. |
| Q3 | Calendar target — drive Google Calendar's UI, or emit an `.ics` draft? | M6 | `.ics` draft first (no second UI surface to learn), UI path as a fast-follow. |
| Q4 | Does the cockpit persist run history across browser sessions? | M3 | Yes, keyed by `thread_id` from the checkpointer; list view is read-only in v1. |
| Q5 | Router granularity — is a third `compound` class (multi-worker plans) needed in v1? | M2 | No. Two classes; the supervisor can already dispatch sequentially. |
