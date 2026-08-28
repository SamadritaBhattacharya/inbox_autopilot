"""Typed intent, plan, and route — the PRE phase's vocabulary.

Parsing a natural-language task into a **typed** intent, before anything runs, is what makes
the 100%-context rule testable. "Does this task have what it needs?" is answerable against a
data structure; it is not answerable against a sentence.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Action(StrEnum):
    """What the user is asking for. Each declares its own required slots.

    Split into read-only and mutating deliberately — see `READ_ONLY_ACTIONS`. A mailbox
    request is far more often a question than a command ("what did Priya say?", "anything
    from the bank this week?"), and those deserve a fast path: little to ask about, and a
    capability set that literally cannot change the mailbox.
    """

    # ── read-only: answer a question about the mailbox ──
    READ = "read"
    SUMMARIZE = "summarize"
    SEARCH = "search"
    COUNT = "count"
    #: The open-ended catch-all for anything else about the mailbox. Exists so a request we
    #: did not enumerate becomes a *read-only investigation* rather than an UNKNOWN that
    #: interrogates the user. Flexibility with the safe default.
    ANSWER = "answer"
    #: "open my sent folder", "show me spam". Read-only: changing which folder is on screen
    #: changes what can be SEEN, never what the mailbox contains — so it needs no gate, and
    #: a query run can use it without acquiring any power to alter anything.
    OPEN_FOLDER = "open_folder"

    # ── mutating ──
    SEND_EMAIL = "send_email"
    REPLY = "reply"
    FORWARD = "forward"
    TRIAGE = "triage"
    ARCHIVE = "archive"
    LABEL = "label"
    SNOOZE = "snooze"
    EXTRACT_EVENT = "extract_event"
    APPLY_RULES = "apply_rules"

    #: Not about email at all. The gate asks rather than guessing.
    UNKNOWN = "unknown"


#: Actions that only ever read. They bind a tool set with **no mutating verb in it**, so a
#: "summarize my inbox" run cannot archive anything even if a message body asks it to. The
#: capability is absent, not merely discouraged.
READ_ONLY_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.READ,
        Action.SUMMARIZE,
        Action.SEARCH,
        Action.COUNT,
        Action.ANSWER,
        Action.OPEN_FOLDER,
    }
)


class TaskIntent(BaseModel):
    """The task, in a shape the gate and router can reason about.

    `slots` is an open dict rather than a field per action: a required-slot schema that
    lives in data (see `slots.py`) can be extended by adding a row, whereas one encoded as
    model fields forces a schema change and a migration for every new action.
    """

    model_config = ConfigDict(frozen=True)

    action: Action = Action.UNKNOWN
    slots: dict[str, str] = Field(default_factory=dict)
    #: The classifier's own confidence in the ACTION. Slot completeness is scored
    #: separately — a perfectly-classified action can still be missing everything it needs.
    action_confidence: float = 0.0
    #: Free-text constraints worth preserving verbatim ("keep it short", "don't cc anyone").
    constraints: list[str] = Field(default_factory=list)
    #: The human has answered a question framed around this action, so the action itself is
    #: no longer in doubt — whatever the classifier scored it at.
    #:
    #: **Without this the gate could livelock, and did.** `confidence` is
    #: `action_confidence × (slots filled)`, so its ceiling is whatever the classifier
    #: happened to emit. A clear "write a good evening motivation email" scored 0.80 against
    #: a 0.85 bar; every slot was then filled, the score sat at its 0.80 ceiling forever, and
    #: three answers moved nothing. The run died `CONTEXT_INCOMPLETE` reporting `missing: .`
    #: — with nothing missing.
    #:
    #: This is grounding, the standard fix in task-oriented dialogue: evidence from the human
    #: must update the belief, or the belief tracker is not tracking. Confirming the ACTION
    #: never bypasses missing SLOTS — `is_ready` still requires those separately, so the
    #: 100%-context rule is untouched.
    action_confirmed: bool = False

    def confirmed(self) -> TaskIntent:
        """A copy whose action the human has now vouched for.

        Never applied to `UNKNOWN`: an answer to "what would you like me to do?" is the
        human supplying the action, not confirming one, and treating it as confirmation
        would let an unclassified request through the gate.
        """
        if self.action is Action.UNKNOWN:
            return self
        return self.model_copy(update={"action_confirmed": True})

    def with_slots(self, **updates: str) -> TaskIntent:
        """A copy with slots merged in. Answers accumulate; they never overwrite the intent."""
        merged = {**self.slots, **{k: v for k, v in updates.items() if v}}
        return self.model_copy(update={"slots": merged})


def merge_intent(existing: TaskIntent, fresh: TaskIntent, *, declined: bool) -> TaskIntent:
    """Fold a re-classification into what is already known.

    Re-reading the whole conversation each turn is what lets a compound answer land in the
    right places: told "to Biyash about the demo" with both the recipient and the topic
    missing, the old gate put that entire sentence into BOTH slots, because it had no way to
    split it. A classifier reading task-plus-answers does.

    Three rules, and each exists to stop a second opinion doing damage:

    **What the human gave, the human keeps.** An existing non-empty slot always wins. The
    model re-reading its own earlier output must never be able to quietly discard an address
    somebody typed — that is a wrong recipient, and nobody would see it happen.

    **The action only changes when the human rejected it.** A re-read that decides "reply"
    where the last pass said "forward" would silently redirect the task. The one time that
    IS wanted is after a refusal — "no, not an email" is precisely a licence to re-derive —
    so `declined` gates it. An `UNKNOWN` existing action is also always replaceable: there
    is nothing there to protect.

    **Confidence only ever rises.** More context is more evidence; a second reading that
    happens to sample lower must not undo ground already gained, or answering a question
    could leave the run further from starting than before.
    """
    keep_action = existing.action is not Action.UNKNOWN and not declined
    action = existing.action if keep_action else fresh.action

    # Existing values overwrite fresh ones — dict order puts the winner last.
    slots = {**fresh.slots, **{k: v for k, v in existing.slots.items() if str(v).strip()}}

    return TaskIntent(
        action=action,
        slots=slots,
        action_confidence=max(existing.action_confidence, fresh.action_confidence),
        constraints=list(dict.fromkeys([*existing.constraints, *fresh.constraints])),
        # A refusal withdraws consent: whatever was confirmed before is no longer confirmed.
        action_confirmed=existing.action_confirmed and not declined,
    )


class Plan(BaseModel):
    """What the agent intends to do, posted to the cockpit before it acts.

    Not a rigid script — the loop may revise it. Its job is to let a human see intent
    *before* the first action rather than reconstruct it afterwards.
    """

    model_config = ConfigDict(frozen=True)

    steps: list[str] = Field(default_factory=list)
    rationale: str = ""


class Route(BaseModel):
    """Execution topology, and why.

    `why` is carried because a router decision that cannot be explained cannot be debugged:
    when a task is misrouted, the interesting question is always what the classifier thought
    it saw.
    """

    model_config = ConfigDict(frozen=True)

    topology: str  # "linear" | "decision"
    why: str = ""
    #: True when a deterministic rule matched and no classifier call was needed. The
    #: cheapest correct path, and worth measuring.
    rule_matched: bool = False
