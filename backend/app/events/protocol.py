"""The event vocabulary the cockpit renders.

One flat type with a string tag rather than a class per event: these cross a WebSocket as
JSON, and a cockpit that receives an unknown event should ignore it, not fail to parse the
frame. A closed union would make adding an event a breaking protocol change.

**Every event is an egress point**, so anything constructed here has already passed through
tokenization upstream. The one deliberate exception is `approval_request.preview`, which
carries the resolved draft to the human — see `SECURITY-MODEL.md §5`.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ── lifecycle ──
STATUS = "status"
INTENT = "intent"
ROUTE = "route"
PLAN_UPDATE = "plan_update"
FINALIZE = "finalize"
RUN_COMPLETE = "run_complete"
RUN_ABSENT = "run_absent"

# ── the loop ──
REASONING = "reasoning"
#: The agent's verdict on its own previous action, with the measured outcome alongside.
ASSESSMENT = "assessment"
#: Confirmation that a human correction reached the loop.
FEEDBACK_ACK = "feedback_ack"
#: A preference stated often enough to be worth proposing as a standing rule.
RULE_CANDIDATE = "rule_candidate"
#: A calendar event read out of a thread, drafted for a human to check.
EVENT_PROPOSED = "event_proposed"
TOOL_CALL = "tool_call"
ACTION_RESULT = "action_result"
OBSERVATION = "observation"
FRAME = "frame"

# ── human in the loop ──
QUESTION = "question"
APPROVAL_REQUEST = "approval_request"
APPROVAL_RESULT = "approval_result"
DIAGNOSIS = "diagnosis"
OPTIONS = "options"

# ── telemetry ──
USAGE = "usage"
MEMORY_UPDATE = "memory_update"
ERROR = "error"


class AgentEvent(BaseModel):
    """One thing that happened, on its way to the cockpit."""

    model_config = ConfigDict(frozen=True)

    event: str
    data: dict[str, Any] = Field(default_factory=dict)
    ts: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_wire(self) -> dict[str, Any]:
        return {"event": self.event, "data": self.data, "ts": self.ts}
