"""The required-slot registry — the 100%-context rule, as data.

**This is a table, not prompt text.** That is the entire point. "The agent won't start
without full context" expressed as an instruction is a hope; expressed as a schema the gate
evaluates, it is a property a test can prove and a reviewer can read.

Some actions accept alternatives — `send_email` needs a topic *or* a body intent, either
will do. A group of alternatives is satisfied by any one member, which is why requirements
are lists of lists rather than a flat list of names.
"""
from __future__ import annotations

from app.manager.intent import READ_ONLY_ACTIONS, Action, TaskIntent

#: Each entry is a list of ALTERNATIVE GROUPS. A group is satisfied when any slot in it is
#: filled, and the action is ready when every group is satisfied.
REQUIRED_SLOTS: dict[Action, list[list[str]]] = {
    # ── read-only: deliberately cheap to satisfy ──
    #
    # A question about the mailbox should not be met with an interrogation. These read
    # nothing they were not asked to read and change nothing at all, so the cost of
    # guessing slightly wrong is one wasted look — whereas the cost of nagging is a user
    # who stops asking. The gate's caution is priced against the blast radius, and here
    # there isn't one.
    Action.READ: [["thread_ref", "selector", "query"]],
    Action.SUMMARIZE: [],  # "summarize my inbox" needs nothing; scope defaults to inbox
    Action.SEARCH: [["query"]],
    Action.COUNT: [],
    Action.ANSWER: [["query"]],
    # ── mutating: the full bar ──
    Action.SEND_EMAIL: [["recipient_identity"], ["topic", "body_intent"]],
    Action.REPLY: [["thread_ref"], ["stance", "body_intent"]],
    Action.FORWARD: [["thread_ref"], ["recipient_identity"]],
    Action.TRIAGE: [["scope"]],
    Action.ARCHIVE: [["selector"]],
    Action.LABEL: [["selector"], ["target_label"]],
    Action.SNOOZE: [["selector"], ["until"]],
    Action.EXTRACT_EVENT: [["thread_ref"]],
    # The rules themselves ARE the input; nothing else is needed to start.
    Action.APPLY_RULES: [],
    # Never dispatched: an unknown action always fails the gate and triggers a question.
    Action.UNKNOWN: [["action_clarification"]],
}

#: Slots worth asking about when present but ambiguous, in priority order. Used to phrase a
#: single focused question rather than a checklist.
OPTIONAL_SLOTS: dict[Action, list[str]] = {
    Action.SEND_EMAIL: ["tone", "cc", "subject", "deadline"],
    Action.REPLY: ["tone", "include_quote"],
    Action.TRIAGE: ["aggressiveness", "dry_run"],
}

#: Human phrasing for a slot, used to build the question. A gate that asks for
#: "recipient_identity" has leaked its schema into the conversation.
SLOT_PROMPTS: dict[str, str] = {
    "recipient_identity": "who should this go to",
    "topic": "what the email should be about",
    "body_intent": "what you want the email to say",
    "thread_ref": "which thread you mean",
    "stance": "how you want to reply",
    "scope": "which mail I should look at (the whole inbox, a label, or a search)",
    "selector": "which messages you mean",
    "target_label": "which label to apply",
    "until": "when it should come back",
    "query": "what to search for",
    "action_clarification": "what you'd like me to do",
}

#: Sensible defaults for read-only work, applied by the gate before it decides to ask.
#: "Summarize my inbox" means the inbox; making the user say so is friction with no payoff,
#: because a wrong guess here reads the wrong list rather than mailing the wrong person.
READ_ONLY_DEFAULTS: dict[Action, dict[str, str]] = {
    Action.SUMMARIZE: {"scope": "inbox"},
    Action.COUNT: {"scope": "inbox"},
    Action.READ: {"selector": "the most recent thread"},
}

#: Below this, the gate asks rather than proceeds. Mirrors
#: `Settings.context_confidence_threshold`, which is the value actually used at runtime.
DEFAULT_THRESHOLD = 0.85


def apply_defaults(intent: TaskIntent) -> TaskIntent:
    """Fill read-only defaults the user did not bother to state."""
    defaults = READ_ONLY_DEFAULTS.get(intent.action)
    if not defaults:
        return intent
    missing = {k: v for k, v in defaults.items() if not intent.slots.get(k, "").strip()}
    return intent.with_slots(**missing) if missing else intent


def missing_slots(intent: TaskIntent) -> list[str]:
    """Which requirements this intent cannot satisfy.

    Returns the FIRST name of each unsatisfied group — the one the question will use.
    """
    intent = apply_defaults(intent)
    groups = REQUIRED_SLOTS.get(intent.action, [])
    return [
        group[0]
        for group in groups
        if not any(intent.slots.get(name, "").strip() for name in group)
    ]


def confidence(intent: TaskIntent) -> float:
    """How ready this intent is to run, in [0, 1].

    Two independent things have to be true, so the score is their product rather than their
    average: a confidently-classified action with no slots filled is not half-ready, it is
    not ready. Averaging would let a high classifier score paper over missing information —
    which is exactly the failure the gate exists to prevent.
    """
    groups = REQUIRED_SLOTS.get(intent.action, [])
    if intent.action is Action.UNKNOWN:
        return 0.0
    if not groups:
        return intent.action_confidence

    satisfied = len(groups) - len(missing_slots(intent))
    return intent.action_confidence * (satisfied / len(groups))


#: Read-only work clears at a lower bar. The gate's caution should be proportional to what
#: a mistake costs, and here a misread means one wasted look — not a mail sent to the wrong
#: person. Holding both to the same bar makes the agent feel obstructive for no safety gain.
READ_ONLY_THRESHOLD = 0.5


def threshold_for(action: Action, *, default: float = DEFAULT_THRESHOLD) -> float:
    return READ_ONLY_THRESHOLD if action in READ_ONLY_ACTIONS else default


def is_ready(intent: TaskIntent, *, threshold: float = DEFAULT_THRESHOLD) -> bool:
    bar = min(threshold, threshold_for(intent.action, default=threshold))
    return not missing_slots(intent) and confidence(intent) >= bar


def question_for(intent: TaskIntent) -> str:
    """One human question covering what is missing.

    Deliberately asks for everything missing at once. A gate that asks three questions in
    sequence is three round trips of a human's attention, and each pause is a place the run
    gets abandoned.
    """
    missing = missing_slots(intent)
    if not missing:
        return "Could you confirm what you'd like me to do?"

    phrases = [SLOT_PROMPTS.get(name, name.replace("_", " ")) for name in missing]
    if len(phrases) == 1:
        return f"Before I start — {phrases[0]}?"
    head = ", ".join(phrases[:-1])
    return f"Before I start — {head}, and {phrases[-1]}?"
