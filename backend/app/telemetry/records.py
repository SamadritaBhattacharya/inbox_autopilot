"""Typed failures and the trajectory record.

**Every terminal state carries an `ErrorCode`.** "It just stopped" is a P0 bug, not a
mystery to investigate later: an untyped exit cannot be counted, cannot be diagnosed by
the recovery layer, and cannot be turned into a ranked remedy for the user. The benchmark
measures "% terminated with a typed code" and the target is 100%.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["classifier", "executor", "validator"]


class ErrorCode(StrEnum):
    """The complete set of ways a run can end badly. Nothing exits outside this list.

    `StrEnum` so a code serialises as its bare value everywhere — checkpoints, the event
    stream, log lines — rather than as `ErrorCode.STUCK`. A failure code that renders
    differently depending on which side of the wire prints it is a code you cannot grep.
    """

    # ── loop / perception ──
    STUCK = "STUCK"
    ACTION_TIMEOUT = "ACTION_TIMEOUT"
    REASONING_MISSING = "REASONING_MISSING"
    MAX_STEPS = "MAX_STEPS"
    NO_ACTION = "NO_ACTION"

    # ── human-in-the-loop ──
    APPROVAL_TIMEOUT = "APPROVAL_TIMEOUT"
    APPROVAL_REJECTED_NO_ALT = "APPROVAL_REJECTED_NO_ALT"
    CONTEXT_INCOMPLETE = "CONTEXT_INCOMPLETE"

    # ── infrastructure ──
    PROVIDER_EXHAUSTED = "PROVIDER_EXHAUSTED"
    SURFACE_UNAVAILABLE = "SURFACE_UNAVAILABLE"
    NOT_SIGNED_IN = "NOT_SIGNED_IN"


class Usage(BaseModel):
    """Token accounting for one LLM call. Metered on EVERY call, every role."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    # Cached prompt tokens, where the provider reports them. Prompt caching is a primary
    # free-tier lever, so "did the cache hit?" must be measurable rather than assumed.
    cached_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )


class StepRecord(BaseModel):
    """One row of the trajectory.

    The ordered records ARE the trajectory: replayable, auditable, and the substrate the
    benchmark measures. Anything worth asking about a run afterwards has to be here,
    because nothing else survives.

    Nothing here may carry raw PII — this is a persisted egress point and passes through
    the redaction filter on write.
    """

    model_config = ConfigDict(frozen=True)

    step: int
    node: str
    worker: str | None = None

    action: str | None = None
    success: bool | None = None
    #: `str`, not `ErrorCode` — this row carries TWO different vocabularies depending on
    #: which node wrote it. A terminal step (`finalize`, `diagnose`) writes a member of
    #: `ErrorCode`, the run-termination codes in CLAUDE.md §11. An `act` or `linear` step
    #: writes a dispatch-rejection code instead (`STALE_INDEX`, `UNKNOWN_TOKEN`,
    #: `APPROVAL_REQUIRED`, …) — see `app.surface.dispatch` — which is a wider, per-action
    #: vocabulary that is not and should not be a member of `ErrorCode`. Typing this field
    #: as `ErrorCode` made recording an action's real code a `ValidationError`, which is why
    #: the act node silently wrote `None` here instead of `result.error_code`: every
    #: hallucinated referent the dispatcher caught was refused correctly and told to the
    #: model, then its typed reason was thrown away before it ever reached the trajectory.
    error_code: str | None = None

    # LLM metering — populated on reason/intake/router/verify steps, absent elsewhere.
    provider: str | None = None
    role: Role | None = None
    usage: Usage | None = None
    latency_ms: int | None = None

    # Enough to reverse a mutating action. Without it, "undo" is a promise we cannot keep.
    undo: dict[str, Any] | None = None

    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
