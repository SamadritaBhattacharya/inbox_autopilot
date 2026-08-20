"""Root-cause classification — pure, and the input to everything downstream.

A typed `ErrorCode` says *what* went wrong. A `Cause` says *why*, and only the why can be
remedied: `STUCK` is not actionable, "a dialog is covering the button" is. Every remedy the
system can offer, and every sentence it says to a human about a failure, starts here.

Deliberately a **pure function over evidence already in state** — the error code, the last
action, and how the observation changed. No LLM call: a classifier that costs a model call
cannot run on every failure, and a failure is exactly when the provider is most likely to be
the thing that broke.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from inbox_contracts import ActionCall, ActionResult, Observation

from app.telemetry.records import ErrorCode


class Cause(StrEnum):
    """Why a run failed, in terms something can be done about."""

    OVERLAY_BLOCKING = "overlay_blocking"
    TARGET_MOVED = "target_moved"
    OFF_SCREEN = "off_screen"
    SLOW_RENDER = "slow_render"
    NAVIGATED_AWAY = "navigated_away"
    OSCILLATION = "oscillation"
    STALE_VIEW = "stale_view"
    PROVIDER_EXHAUSTED = "provider_exhausted"
    MODEL_DEGRADED = "model_degraded"
    HUMAN_BLOCKED = "human_blocked"
    SURFACE_GONE = "surface_gone"
    BUDGET_SPENT = "budget_spent"
    UNKNOWN = "unknown"


#: What to tell the human. Never an enum name — "STUCK" explains nothing to the person
#: deciding what to do next, and this text is the whole of what they have to go on.
PLAIN: dict[Cause, str] = {
    Cause.OVERLAY_BLOCKING: "A dialog is covering the thing I was trying to click.",
    Cause.TARGET_MOVED: "What I was aiming at isn't where I expected it to be.",
    Cause.OFF_SCREEN: "What I need is off-screen — I couldn't reach it from here.",
    Cause.SLOW_RENDER: "The page was still loading when I tried to act.",
    Cause.NAVIGATED_AWAY: "The page changed underneath me mid-action.",
    Cause.OSCILLATION: "I kept going back and forth between the same two views.",
    Cause.STALE_VIEW: "I acted on a view that had already moved on.",
    Cause.PROVIDER_EXHAUSTED: "The model provider is rate-limiting or out of quota.",
    Cause.MODEL_DEGRADED: "The model stopped explaining what it was doing.",
    Cause.HUMAN_BLOCKED: "I'm waiting on you, or you declined.",
    Cause.SURFACE_GONE: "I lost the connection to the mailbox.",
    Cause.BUDGET_SPENT: "I ran out of steps before finishing.",
    Cause.UNKNOWN: "Something went wrong and I couldn't work out why.",
}


@dataclass(frozen=True)
class Diagnosis:
    cause: Cause
    #: The sentence shown to the human.
    plain: str
    #: What the classification was based on. Shown underneath, so a user who disagrees can
    #: see the reasoning rather than being told a conclusion.
    evidence: str

    @classmethod
    def of(cls, cause: Cause, evidence: str = "") -> Diagnosis:
        return cls(cause=cause, plain=PLAIN[cause], evidence=evidence)


def _appeared(observation: Observation | None, previous: Observation | None) -> list[str]:
    """Elements present now that were not there before."""
    if observation is None:
        return []
    if previous is None:
        return [element.name for element in observation.elements if element.is_new]
    before = {f"{e.role}:{e.name}" for e in previous.elements}
    return [e.name for e in observation.elements if f"{e.role}:{e.name}" not in before]


def _looks_like_a_dialog(observation: Observation | None, appeared: list[str]) -> bool:
    if observation is not None and observation.mail is not None and observation.mail.compose_open:
        return True
    dialog_words = ("dialog", "confirm", "are you sure", "cancel", "discard", "ok")
    return any(word in name.lower() for name in appeared for word in dialog_words)


def classify(
    *,
    error_code: ErrorCode | None,
    last_action: ActionCall | None = None,
    last_result: ActionResult | None = None,
    observation: Observation | None = None,
    previous: Observation | None = None,
    stuck_count: int = 0,
    oscillating: bool = False,
) -> Diagnosis:
    """Map the evidence to a cause.

    Ordered most-specific first. The infrastructure causes come before the perceptual ones
    because a rate-limited provider *looks* like a stuck agent from the page's point of
    view, and offering "scroll and retry" to someone who is out of quota wastes their turn.
    """
    action = last_action.name if last_action else "the last action"

    # ── infrastructure: unambiguous, and it masquerades as everything else ──
    if error_code is ErrorCode.PROVIDER_EXHAUSTED:
        return Diagnosis.of(Cause.PROVIDER_EXHAUSTED, "every configured provider refused the call")
    if error_code is ErrorCode.SURFACE_UNAVAILABLE:
        return Diagnosis.of(Cause.SURFACE_GONE, "the browser stopped responding")
    if error_code in (ErrorCode.APPROVAL_TIMEOUT, ErrorCode.APPROVAL_REJECTED_NO_ALT):
        return Diagnosis.of(Cause.HUMAN_BLOCKED, f"the run ended on {error_code.value}")
    if error_code is ErrorCode.REASONING_MISSING:
        return Diagnosis.of(Cause.MODEL_DEGRADED, "the model called a tool without explaining it")
    if error_code is ErrorCode.MAX_STEPS:
        return Diagnosis.of(Cause.BUDGET_SPENT, "the step budget ran out mid-task")

    # ── dispatch-level refusals: the result already names the problem ──
    code = (last_result.error_code if last_result else None) or ""
    if code == "STALE_INDEX":
        return Diagnosis.of(Cause.STALE_VIEW, f"{action} used an index from an earlier view")
    if code == "ACTION_TIMEOUT":
        return Diagnosis.of(Cause.SLOW_RENDER, f"{action} exceeded its time limit")

    # ── perceptual ──
    if oscillating:
        return Diagnosis.of(Cause.OSCILLATION, "the same two views alternated without progress")

    appeared = _appeared(observation, previous)
    if stuck_count >= 2 and _looks_like_a_dialog(observation, appeared):
        detail = ", ".join(appeared[:3]) or "a dialog is open"
        return Diagnosis.of(
            Cause.OVERLAY_BLOCKING, f"the page stopped changing and this appeared: {detail}"
        )

    if observation is not None and observation.dropped_count > 0 and stuck_count >= 1:
        return Diagnosis.of(
            Cause.OFF_SCREEN,
            f"{observation.dropped_count} items are off screen. {observation.hint or ''}".strip(),
        )

    if stuck_count >= 2:
        return Diagnosis.of(
            Cause.TARGET_MOVED, f"the page did not change after {stuck_count} actions"
        )

    if (
        observation is not None
        and previous is not None
        and observation.context_id != previous.context_id
    ):
        return Diagnosis.of(Cause.NAVIGATED_AWAY, "the page changed identity mid-action")

    if error_code is ErrorCode.STUCK:
        return Diagnosis.of(Cause.TARGET_MOVED, "actions stopped having any effect")
    if error_code is ErrorCode.NO_ACTION:
        return Diagnosis.of(Cause.MODEL_DEGRADED, "the model stopped choosing actions")
    if error_code is ErrorCode.CONTEXT_INCOMPLETE:
        return Diagnosis.of(Cause.HUMAN_BLOCKED, "not enough information to start safely")

    return Diagnosis.of(Cause.UNKNOWN, f"{action} failed with {error_code or 'no code'}")
