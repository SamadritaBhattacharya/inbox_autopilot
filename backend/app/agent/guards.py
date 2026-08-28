"""Loop guards — pure functions, no I/O.

An agent loop without these does not fail; it *hangs*, burning free-tier quota on the same
wrong action until a human notices. Every guard here ends in a typed `ErrorCode`, because a
run that stops without one cannot be counted, diagnosed, or turned into a ranked remedy.

Two independent detectors, because they catch different loops:

- **Page signature** catches "my actions have no effect" — clicking a dead button, waiting
  for something that never arrives. The page does not change.
- **Action repetition** catches "I keep doing the same thing while the page churns" — the
  clear/retype loop, where every turn *does* change the page and the signature check sees
  progress that is not there.

Either alone leaves a real class of loop running.
"""
from __future__ import annotations

import hashlib
import json

from inbox_contracts import ActionCall, Observation

# ── thresholds ──────────────────────────────────────────────────────────────

#: Repeating a committing action three times is worth a hard nudge…
REPEAT_NUDGE_AT = 3
#: …and five times is not a strategy, it is a loop.
REPEAT_KILL_AT = 5
#: Window size. Long enough to see a cycle, short enough that old history stops mattering.
RECENT_WINDOW = 8

#: An unchanged page for two turns is worth mentioning…
STUCK_NUDGE_AT = 2
#: …and eight means nothing this agent does reaches the page.
STUCK_KILL_AT = 8

#: Sane reasoning is a few hundred characters. Past this it is a degenerate repetition
#: loop, and letting it into history burns tokens on every later turn AND feeds the model
#: its own repetition to continue.
MAX_REASONING_CHARS = 3000

#: With this many steps left, tell the model to report what it has. A well-explained partial
#: answer beats a MAX_STEPS timeout with the findings unreported.
BUDGET_WARNING_STEPS = 5

#: Verbs that are *meant* to repeat. Scrolling five times is reading, not looping.
#: Verbs where repeating the IDENTICAL call is still progress. `Scroll(down)` twice moves
#: twice; `WaitFor` twice waits longer. Both genuinely advance the run.
#:
#: `Extract` and `Recall` were once on this list and should not have been. They are pure
#: reads with no side effects, which is why they looked harmless — but that also means an
#: identical repeat returns an identical answer, so it cannot advance anything. Observed
#: live: four identical `Extract("what is in the To field?")` calls in a row, invisible to
#: this guard because of that exemption, each one a full LLM call against a provider that
#: was already rate-limiting. "Read-only" is not the same as "free"; on a free tier the
#: binding constraint is requests, not consequences.
#:
#: Different arguments stay safe either way — `action_signature` hashes the args, so two
#: different Extract questions have different signatures and never count against each other.
REPEATABLE_VERBS = frozenset({"Scroll", "WaitFor", "ReadThread", "Observe"})

#: Verbs where the same verb on a DIFFERENT target is progress, not repetition. Archiving
#: forty newsletters is the job; treating it as a loop would kill every triage run.
PER_TARGET_VERBS = frozenset({"Archive", "Snooze", "MarkRead", "Label", "DraftReply"})


def action_signature(call: ActionCall) -> str:
    """A stable identity for "the same action again".

    Includes arguments, so archiving thread 3 and thread 9 are different actions while
    clicking the same dead button twice is the same one.
    """
    args = json.dumps(call.args, sort_keys=True, default=str)
    return f"{call.name}:{hashlib.sha1(args.encode()).hexdigest()[:12]}"


def is_repetition_candidate(call: ActionCall) -> bool:
    """Should this action count toward the repetition guard at all?"""
    return call.name not in REPEATABLE_VERBS


def repetition_count(recent: list[str], signature: str) -> int:
    """How many times this exact action appears in the recent window."""
    return recent[-RECENT_WINDOW:].count(signature)


def push_action(recent: list[str], signature: str) -> list[str]:
    """Append to the rolling window, bounded."""
    return [*recent, signature][-RECENT_WINDOW:]


#: A cycle this long over this few distinct actions is oscillation, not exploration.
OSCILLATION_WINDOW = 6
OSCILLATION_NUDGE_AT = 4
OSCILLATION_KILL_AT = 6


def is_oscillating(recent: list[str], *, at_least: int = OSCILLATION_NUDGE_AT) -> bool:
    """Is the agent bouncing between the same two actions?

    The repetition guard exempts verbs that are *meant* to repeat — scrolling five times is
    reading, not looping. But that exemption has a hole: scroll down, scroll up, scroll
    down, scroll up is a loop made entirely of exempt actions, and both the repetition
    guard and the page-signature guard miss it because every single step genuinely changes
    the page.

    Observed on a real run: the agent had everything it needed after one screen, and spent
    its remaining budget alternating between two views trying to see them at once.

    Detected as a short cycle — a window covering at most two distinct actions that are not
    all the same one (plain repetition is the other guard's job).
    """
    tail = recent[-OSCILLATION_WINDOW:]
    if len(tail) < at_least:
        return False
    distinct = set(tail)
    return len(distinct) == 2 and tail[-1] != tail[-2]


def oscillation_nudge() -> str:
    return (
        "You are alternating between the same two views without making progress. You do not "
        "need to see everything at once — use Remember to note what you have already seen, "
        "then move on. If you have enough to answer, call Complete(success=true) with what "
        "you found."
    )


def page_signature(observation: Observation | None) -> str:
    """A fingerprint of what the agent can see.

    Built from the element list rather than the screenshot: a blinking cursor or a relative
    timestamp changes pixels every second, and a signature that never repeats would disable
    stuck detection entirely.
    """
    if observation is None:
        return ""
    parts = [observation.context_id, str(len(observation.elements))]
    parts += [f"{e.index}:{e.role}:{e.name[:40]}" for e in observation.elements]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def clip_reasoning(text: str) -> str:
    """Bound runaway output before it enters history."""
    if len(text) <= MAX_REASONING_CHARS:
        return text
    return text[:MAX_REASONING_CHARS].rstrip() + " …[truncated: output ran away]"


def budget_reminder(step: int, max_steps: int) -> str | None:
    """A nudge to wrap up, when the step budget is nearly spent."""
    remaining = max_steps - step
    if remaining <= 0 or remaining > BUDGET_WARNING_STEPS:
        return None
    urgency = (
        "This is your LAST step — "
        if remaining <= 1
        else f"Only {remaining} steps left — "
    )
    return (
        f"{urgency}if you already have the answer or useful partial findings, call "
        "Complete(success, reason) NOW and put the findings in the reason. Running out of "
        "steps with findings unreported counts as a failure."
    )


def repetition_nudge(count: int) -> str:
    return (
        f"You have used the SAME action {count} times and it is NOT making progress. Do NOT "
        "repeat it. If you already entered the text, submit it instead of retyping. "
        "Otherwise pick a DIFFERENT element, scroll to find what you need, or call "
        "Complete(success=false) if you are genuinely blocked."
    )


def stuck_nudge(count: int) -> str:
    return (
        f"The page has NOT changed after your last {count} action(s) — they had no effect. "
        "Do NOT repeat the same action. Scroll to reveal off-screen content, WaitFor(2) if "
        "it may still be loading, or pick a DIFFERENT element. If you genuinely cannot "
        "proceed, call Complete(success=false)."
    )
