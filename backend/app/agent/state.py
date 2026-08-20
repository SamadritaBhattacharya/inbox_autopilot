"""`AgentState` — the single source of truth flowing through the graph.

**No mutable agent state exists anywhere else.** Not on a service, not in a module global,
not on the surface object. Everything a run knows is here, which is what makes a run
checkpointable, resumable, and replayable — and what makes a durable human interrupt
possible at all. State held outside this model would silently fail to survive the pause.

Nodes return **deltas**, never mutated copies. Fields carrying history use append reducers
so two nodes writing in the same step compose instead of clobbering each other.
"""
from __future__ import annotations

import operator
from typing import Annotated, Literal

from inbox_contracts import ActionCall, ActionResult, Observation
from pydantic import BaseModel, ConfigDict, Field

from app.llm.base import Message
from app.manager.intent import Plan, Route, TaskIntent
from app.telemetry.records import ErrorCode, StepRecord

Status = Literal["gathering", "running", "awaiting_human", "done", "failed"]


class AgentState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ── identity ────────────────────────────────────────────────────────────
    task: str
    thread_id: str

    # ── PRE ─────────────────────────────────────────────────────────────────
    intent: TaskIntent | None = None
    missing_slots: list[str] = Field(default_factory=list)
    #: Every answer the human has given, in order. Kept so a resumed run can re-derive its
    #: intent from scratch rather than trusting a partially-updated copy.
    answers: Annotated[list[str], operator.add] = Field(default_factory=list)
    pending_question: str | None = None
    route: Route | None = None
    plan: Plan | None = None

    # ── IN ──────────────────────────────────────────────────────────────────
    active_worker: str | None = None
    messages: Annotated[list[Message], operator.add] = Field(default_factory=list)
    observation: Observation | None = None
    agent_memory: dict[str, str] = Field(default_factory=dict)
    history: Annotated[list[StepRecord], operator.add] = Field(default_factory=list)
    last_action: ActionCall | None = None
    last_result: ActionResult | None = None

    # ── POST ────────────────────────────────────────────────────────────────
    diagnosis: object | None = None
    #: Causes already remediated in this run, and the strategies already tried.
    #: Self-heal must terminate: without these the loop can offer the move that just
    #: failed, forever.
    remedied_causes: Annotated[list[str], operator.add] = Field(default_factory=list)
    tried_strategies: Annotated[list[str], operator.add] = Field(default_factory=list)

    # ── control ─────────────────────────────────────────────────────────────
    status: Status = "gathering"
    error_code: ErrorCode | None = None
    step: int = 0
    finished: bool = False
    success: bool | None = None
    reason: str = ""

    # ── guards ──────────────────────────────────────────────────────────────
    stuck_count: int = 0
    nudge_count: int = 0
    #: Rolling window of recent action signatures, for the repetition guard. Catches loops
    #: the page-signature check misses — an agent that keeps clearing and retyping a field
    #: is making no progress while the page changes on every turn.
    recent_actions: list[str] = Field(default_factory=list)
    #: How many times the gate has asked. Bounded: a gate that can ask forever is a gate
    #: that can hang a run forever.
    ask_count: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.status in ("done", "failed")

    @property
    def awaiting_human(self) -> bool:
        return self.status == "awaiting_human"
