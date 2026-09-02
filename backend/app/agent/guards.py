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
import re

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


#: Verbs whose ANSWER does not depend on their arguments.
#:
#: `Extract` returns a fixed sentence — "the element list above is everything visible, field
#: contents are never included" — whatever it is asked. So the assumption the signature below
#: rests on ("different arguments mean a different question, and a different question is
#: progress") is simply false for it: rewording the query produces a brand-new signature and
#: the identical reply.
#:
#: Observed live. Asked to close a compose window that had ALREADY closed, the agent ran
#: `Extract("what elements correspond to the open compose box…")`, got the canned answer,
#: rephrased it to `Extract("identify any elements related to the open compose box…")`, and
#: got the same answer again — two full LLM calls, no new information, and nothing counting
#: them as a repeat. Nothing would have stopped ten.
ARGS_INDEPENDENT_VERBS = frozenset({"Extract"})


def action_signature(call: ActionCall) -> str:
    """A stable identity for "the same action again".

    Includes arguments, so archiving thread 3 and thread 9 are different actions while
    clicking the same dead button twice is the same one — EXCEPT for the verbs above, whose
    reply is the same however the question is worded.
    """
    if call.name in ARGS_INDEPENDENT_VERBS:
        return f"{call.name}:*"
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


#: Words a model uses when it has decided the task is finished.
#:
#: Narrow on purpose: these are read out of the model's OWN reasoning, and a false positive
#: tells it to Complete a task it has not done. "task is complete" and "we are already
#: there" are conclusions; "the compose window is complete" is not the shape being matched,
#: because every pattern is anchored to a judgement about the task or the work.
_CONCLUDED = re.compile(
    r"\btask (?:is |seems |appears )?(?:already )?(?:complete|completed|done|finished)\b"
    r"|\balready (?:there|open|done|complete)\b"
    r"|\bconsider (?:the )?task done\b"
    r"|\bnothing (?:more|further|else) to do\b"
    r"|\bno further action\b"
    # "complete" only where it NAMES THE VERB. "I will complete the subject field next" is a
    # plan, not a conclusion, and treating it as one would end a run that had barely
    # started — the exact false positive this guard must not make.
    r"|\bcall complete\b"
    r"|\bcomplete\s*\("
    r"|\bcomplete success\b"
    # "Need to respond with Complete. Provide success." — observed, and it matched none of
    # the above, so that turn got the generic nudge instead of the one naming the verb.
    r"|\brespond with complete\b"
    r"|\bcomplete with success\b"
    r"|\bprovide success\b",
    re.IGNORECASE,
)


def has_concluded(reasoning: str) -> bool:
    """Did the model just say, in words, that it is finished?"""
    return bool(_CONCLUDED.search(reasoning or ""))


def no_tool_call_nudge(reasoning: str) -> str:
    """What to say when a turn produced reasoning and no action.

    **Three separate bugs have now ended this way**, and in each the model reached the right
    answer and then described it instead of doing it: `AskUser` was recommended and not
    handled; "propose sending again" was read as *say* something; and a run that had opened
    the requested mail concluded *"we can consider task done... so we can Complete success"*
    and emitted nothing. The run died `NO_ACTION` with the task finished.

    A generic "you did not call a tool" answers none of those, because the model does not
    believe it failed to decide — it believes it decided and said so. Naming the verb its own
    words imply closes the gap that the prompt rule did not.
    """
    if has_concluded(reasoning):
        return (
            "You said the task is finished, but saying it does not end the run — only the "
            "tool does. Call Complete(success=true, reason=...) now, with what you found in "
            "the reason. If it is NOT actually finished, call the tool that finishes it."
        )
    return (
        "You did not call a tool, and reasoning alone changes nothing. Call exactly one "
        "tool now — or Complete(success, reason) if the task is done or you are blocked."
    )
