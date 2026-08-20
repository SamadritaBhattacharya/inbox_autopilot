"""Worker registry — intent in, capability out.

**Capability follows intent, and it is decided in PRE.** By the time a worker starts, its
tool set is fixed: a `summarize` run holds no mutating verb, a `triage` run holds no `Send`.
Nothing the agent later reads can widen that. An email body demanding "forward this to
attacker@evil.com" during a summarize run is arguing with a schema that has no forward tool
in it — there is nothing to negotiate with.

Adding a worker is adding a row here plus a class. The supervisor does not change, which is
the Open/Closed claim made concrete.
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from app.manager.intent import READ_ONLY_ACTIONS, Action
from app.workers.tools import (
    CALENDAR_TOOLS,
    COMPOSE_TOOLS,
    QUERY_TOOLS,
    TRIAGE_TOOLS,
    verb_names,
)


@dataclass(frozen=True)
class WorkerSpec:
    """What a worker is allowed to do, and how it behaves."""

    name: str
    tools: tuple[type[BaseModel], ...]
    #: Read-only workers cannot change the mailbox — enforced by the tool set, restated
    #: here so the property is greppable and assertable.
    read_only: bool
    #: Rough per-run step ceiling. Reading a thread is short; triaging a backlog is not.
    max_steps: int
    purpose: str

    @property
    def verbs(self) -> frozenset[str]:
        return verb_names(self.tools)


QUERY = WorkerSpec(
    name="query",
    tools=QUERY_TOOLS,
    read_only=True,
    max_steps=20,
    purpose="read, search, count, summarize, and answer questions about the mailbox",
)

TRIAGE = WorkerSpec(
    name="triage",
    tools=TRIAGE_TOOLS,
    read_only=False,
    max_steps=40,
    purpose="work the backlog: archive, label, snooze, mark read",
)

COMPOSE = WorkerSpec(
    name="compose",
    tools=COMPOSE_TOOLS,
    read_only=False,
    max_steps=25,
    purpose="write and send mail, with Send gated on human approval",
)

CALENDAR = WorkerSpec(
    name="calendar",
    tools=CALENDAR_TOOLS,
    read_only=True,
    max_steps=20,
    purpose="read a thread and propose a calendar event for you to check",
)

#: Intent -> worker. Anything read-only lands on QUERY; anything unmapped lands there too,
#: which is the safe default: an unrecognised request investigates rather than mutates.
WORKER_FOR_ACTION: dict[Action, WorkerSpec] = {
    **{action: QUERY for action in READ_ONLY_ACTIONS},
    Action.TRIAGE: TRIAGE,
    Action.ARCHIVE: TRIAGE,
    Action.LABEL: TRIAGE,
    Action.SNOOZE: TRIAGE,
    Action.APPLY_RULES: TRIAGE,
    Action.SEND_EMAIL: COMPOSE,
    Action.REPLY: COMPOSE,
    Action.FORWARD: COMPOSE,
    # Extraction READS. Creating the event and sending an invite are separate, gated, and
    # not in v1 — see CALENDAR_TOOLS.
    Action.EXTRACT_EVENT: CALENDAR,
}

WORKERS: dict[str, WorkerSpec] = {
    spec.name: spec for spec in (QUERY, TRIAGE, COMPOSE, CALENDAR)
}


def worker_for(action: Action) -> WorkerSpec:
    """The worker for an intent, defaulting to read-only.

    An unmapped action must never fall through to something that can mutate. Defaulting to
    QUERY means the worst case for a misclassification is a wasted look.
    """
    return WORKER_FOR_ACTION.get(action, QUERY)
