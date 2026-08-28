"""The required-slot registry — the 100%-context rule, as data.

**This is a table, not prompt text.** That is the entire point. "The agent won't start
without full context" expressed as an instruction is a hope; expressed as a schema the gate
evaluates, it is a property a test can prove and a reviewer can read.

Some actions accept alternatives — `send_email` needs a topic *or* a body intent, either
will do. A group of alternatives is satisfied by any one member, which is why requirements
are lists of lists rather than a flat list of names.

**Conditional requirements are a second, smaller table** (`CONDITIONAL_SLOTS`), for the
cases `REQUIRED_SLOTS` cannot express: a requirement that exists only because of what the
intent *already contains*. "Send to one person" and "send to three people" need different
things, and a flat alternative-group has no way to say that — it can express "any of these
fill the requirement," not "this requirement only applies sometimes." A predicate over the
whole intent can. The extensibility property is the same one `REQUIRED_SLOTS` already has:
a new conditional ask is a new row, and the gate that evaluates it never changes.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from app.manager.intent import READ_ONLY_ACTIONS, Action, TaskIntent
from app.security.patterns import TOKEN_RE

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
    Action.OPEN_FOLDER: [["folder"]],
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

#: Splits a recipient phrase into candidate people. Deliberately loose — "P1 and P2",
#: "P1, P2", "alice@x.com and bob@y.com" all need to count as two. It is used only to
#: decide WHETHER to ask a question, never to resolve who anyone is; that stays the
#: vault's job, at dispatch, on tokens the model actually sends.
_RECIPIENT_SPLIT_RE = re.compile(r"\s*(?:,|;|&|\band\b)\s*", re.IGNORECASE)


def split_recipients(value: str) -> list[str]:
    """`value` broken into its candidate people, in order, de-duplicated."""
    seen: set[str] = set()
    parts: list[str] = []
    for part in _RECIPIENT_SPLIT_RE.split(value):
        part = part.strip()
        if part and part not in seen:
            seen.add(part)
            parts.append(part)
    return parts


def recipient_count(value: str) -> int:
    """How many people `value` seems to name.

    Tokens first — by the time `context_gate` runs, an operator-supplied address has
    already been minted into a token (`_trust_user_addresses` in `manager/nodes.py`), so
    counting `TOKEN_RE` matches is the common, exact case. Falling back to a naive split
    only covers a name the classifier copied verbatim because it had no address to trust
    yet ("email John and Priya") — good enough to trigger this question, which is all it
    is for.
    """
    if not value.strip():
        return 0
    tokens = set(TOKEN_RE.findall(value))
    if tokens:
        return len(tokens)
    return len(split_recipients(value))


@dataclass(frozen=True)
class ConditionalSlot:
    """One requirement that exists only when `predicate` holds.

    `prompt` is written whole, not looked up by name in `SLOT_PROMPTS` — a conditional ask
    needs to state its own recommended default ("I'll do X unless you say otherwise"),
    which a bare noun phrase like "who should this go to" has no room for. See §B2 in
    `docs/IMPROVEMENT-PLAN.md`: propose a default, don't just interrogate.
    """

    slot: str
    predicate: Callable[[TaskIntent], bool]
    prompt: str
    #: What the slot resolves to if the human's answer is too vague to parse. Not applied
    #: automatically — this documents the default the prompt promises; the gate still asks.
    default: str
    options: tuple[str, ...] = ()


#: Predicate over the intent, not over one slot value — that is the entire reason this
#: table exists rather than one more `REQUIRED_SLOTS` group.
CONDITIONAL_SLOTS: dict[Action, tuple[ConditionalSlot, ...]] = {
    Action.SEND_EMAIL: (
        ConditionalSlot(
            slot="delivery_mode",
            predicate=lambda intent: recipient_count(
                intent.slots.get("recipient_identity", "")
            )
            > 1,
            prompt=(
                "whether that's one email to everyone or a separate email to each "
                "(I'll send one email to everyone unless you say separately)"
            ),
            default="together",
            options=("together", "separate"),
        ),
    ),
    Action.FORWARD: (
        ConditionalSlot(
            slot="delivery_mode",
            predicate=lambda intent: recipient_count(
                intent.slots.get("recipient_identity", "")
            )
            > 1,
            prompt=(
                "whether to forward it to everyone at once or send it on separately to "
                "each (I'll forward it to everyone at once unless you say separately)"
            ),
            default="together",
            options=("together", "separate"),
        ),
    ),
}


def resolved_delivery_mode(intent: TaskIntent) -> str:
    """`"separate"` or `"together"` — never the raw free text a human typed.

    The gate stores whatever the human said verbatim (consistent with every other slot);
    this is the one place that turns "yeah do them one at a time please" into a value the
    worker's rendering can branch on. Defaults to the slot's own documented default rather
    than guessing at unrecognised text — a misread here changes how many emails go out,
    which is exactly the kind of guess this feature exists to avoid making silently.
    """
    conditions = CONDITIONAL_SLOTS.get(intent.action, ())
    spec = next((c for c in conditions if c.slot == "delivery_mode"), None)
    if spec is None:
        return "together"
    raw = intent.slots.get("delivery_mode", "").strip().lower()
    if not raw:
        return spec.default
    if re.search(r"\bsepar|\bindividual|\bone (at a time|each|by one)|\bapart\b", raw):
        return "separate"
    if re.search(r"\btogether|\bone email|\bsame email|\ball at once|\bcc\b", raw):
        return "together"
    return spec.default


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
    "folder": "which folder or label to open",
    "action_clarification": "what you'd like me to do",
}

#: What each action IS, in words a person can say yes or no to.
#:
#: Used when every slot is filled and the only thing still in doubt is the classification.
#: The old fallback there was "Could you confirm what you'd like me to do?" — which names
#: nothing, so there is no answer that can resolve it. Asking about the ACTION gives the
#: human something to confirm or correct, which is what turns a stuck turn into progress.
ACTION_PROMPTS: dict[Action, str] = {
    Action.SEND_EMAIL: "send an email",
    Action.REPLY: "reply to a thread",
    Action.FORWARD: "forward a thread",
    Action.TRIAGE: "tidy up your inbox",
    Action.ARCHIVE: "archive some mail",
    Action.LABEL: "label some mail",
    Action.SNOOZE: "snooze some mail",
    Action.EXTRACT_EVENT: "pull a calendar event out of a thread",
    Action.APPLY_RULES: "apply your standing rules",
    Action.READ: "read some mail",
    Action.SUMMARIZE: "summarize your mail",
    Action.SEARCH: "search your mail",
    Action.COUNT: "count some mail",
    Action.ANSWER: "answer a question about your mail",
    Action.OPEN_FOLDER: "open a folder",
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

    # A human who answered a question framed around this action has settled what the action
    # IS. The classifier's own number is evidence; theirs is better evidence, and it must be
    # able to override — otherwise no amount of answering can ever raise the score and the
    # gate traps a fully-specified task. See `TaskIntent.action_confirmed`.
    base = 1.0 if intent.action_confirmed else intent.action_confidence
    if not groups:
        return base

    satisfied = len(groups) - len(missing_slots(intent))
    return base * (satisfied / len(groups))


#: Read-only work clears at a lower bar. The gate's caution should be proportional to what
#: a mistake costs, and here a misread means one wasted look — not a mail sent to the wrong
#: person. Holding both to the same bar makes the agent feel obstructive for no safety gain.
READ_ONLY_THRESHOLD = 0.5


def threshold_for(action: Action, *, default: float = DEFAULT_THRESHOLD) -> float:
    return READ_ONLY_THRESHOLD if action in READ_ONLY_ACTIONS else default


def outstanding_slots(intent: TaskIntent) -> list[str]:
    """Everything still needed before this task may run.

    Required slots first; a conditional requirement is evaluated only once every required
    group is already satisfied. Asking "one email or separate?" before the gate even knows
    who it is going to is backwards — and it would mean two round trips for tasks that
    genuinely need both, which is exactly the friction the batched question exists to avoid.
    """
    required = missing_slots(intent)
    if required:
        return required
    conditions = CONDITIONAL_SLOTS.get(intent.action, ())
    return [
        c.slot
        for c in conditions
        if c.predicate(intent) and not intent.slots.get(c.slot, "").strip()
    ]


def is_ready(intent: TaskIntent, *, threshold: float = DEFAULT_THRESHOLD) -> bool:
    bar = min(threshold, threshold_for(intent.action, default=threshold))
    return not outstanding_slots(intent) and confidence(intent) >= bar


def question_for(intent: TaskIntent) -> str:
    """One human question covering what is missing.

    Deliberately asks for everything missing at once. A gate that asks three questions in
    sequence is three round trips of a human's attention, and each pause is a place the run
    gets abandoned. A conditional ask is never batched alongside a required one —
    `outstanding_slots` only surfaces it once the required bar is already clear — but it is
    still phrased through this same single-question path rather than a second one.
    """
    missing = outstanding_slots(intent)
    if not missing:
        # Nothing is missing, so the only thing still in doubt is WHAT this is. Name it, so
        # there is an answer that resolves the question — "yes" confirms the action and the
        # gate clears on the next pass (see `TaskIntent.action_confirmed`).
        #
        # The old text here was "Could you confirm what you'd like me to do?", which names
        # nothing and therefore cannot be resolved by any reply. Asked three times against a
        # fully-specified task, it read as an agent that had stopped listening — and it had,
        # in the sense that no answer could have changed the outcome.
        what = ACTION_PROMPTS.get(intent.action)
        if what:
            return f"Just to check — you want me to {what}?"
        return "Could you tell me what you'd like me to do?"

    conditions_by_slot = {c.slot: c for c in CONDITIONAL_SLOTS.get(intent.action, ())}
    phrases = [
        conditions_by_slot[name].prompt
        if name in conditions_by_slot
        else SLOT_PROMPTS.get(name, name.replace("_", " "))
        for name in missing
    ]
    if len(phrases) == 1:
        return f"Before I start — {phrases[0]}?"
    head = ", ".join(phrases[:-1])
    return f"Before I start — {head}, and {phrases[-1]}?"
