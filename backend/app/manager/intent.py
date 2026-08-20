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
    {Action.READ, Action.SUMMARIZE, Action.SEARCH, Action.COUNT, Action.ANSWER}
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

    def with_slots(self, **updates: str) -> TaskIntent:
        """A copy with slots merged in. Answers accumulate; they never overwrite the intent."""
        merged = {**self.slots, **{k: v for k, v in updates.items() if v}}
        return self.model_copy(update={"slots": merged})


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
