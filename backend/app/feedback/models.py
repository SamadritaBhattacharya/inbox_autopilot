"""Feedback — the signals that let the agent correct itself and be corrected.

Three loops, at three timescales, and they are genuinely different mechanisms:

**Per-turn (self-assessment).** Before choosing the next action the model states whether the
*last* one did what it intended. Without this the agent has no notion of whether it is
making progress — it discovers "my clicks are doing nothing" only when the stuck guard fires
several wasted turns later. The assessment turns a silent failure into an observation the
model reasons over on the very next turn.

**Per-run (human correction).** The user says "no, not that one" mid-run. It enters the loop
as guidance on the next turn and is recorded against the run.

**Across runs (promotion).** The same correction given repeatedly is not a correction, it is
a preference. Recurring feedback becomes a candidate rule — which is how a deterministic
rule gets *earned* rather than configured, and how the linear route grows over time.

`applied` is tracked because feedback that silently fails to reach the model is worse than
no feedback: the user watches, sees nothing change, and concludes the agent ignores them.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class FeedbackKind(StrEnum):
    #: The model's own verdict on its previous action.
    ASSESSMENT = "assessment"
    #: A human telling the agent it got something wrong.
    CORRECTION = "correction"
    #: A human confirming an approach — worth keeping, since it is what promotion needs.
    ENDORSEMENT = "endorsement"
    #: A human declining a proposed irreversible action.
    REJECTION = "rejection"
    #: A human's verdict on a WHOLE RUN, given once after it ends.
    #:
    #: Deliberately not reusing ENDORSEMENT/REJECTION, which now also come from the approval
    #: gate and mean something narrower: "this specific send was right". A run rating is the
    #: only signal in the system that judges the outcome rather than a step, which makes it
    #: the one thing that can tell you whether `Complete(success=True)` was *true* — the
    #: agent's own verdict is otherwise the agent grading its own homework.
    RUN_RATING = "run_rating"


class Outcome(StrEnum):
    """What the last action actually achieved.

    `NO_EFFECT` is the interesting one: the action *succeeded* — the click landed, no error —
    and the page did not change. That combination is invisible to a success flag and is the
    single most common way an agent wastes a run.
    """

    PROGRESSED = "progressed"
    NO_EFFECT = "no_effect"
    FAILED = "failed"
    UNKNOWN = "unknown"


class Feedback(BaseModel):
    model_config = ConfigDict(frozen=True)

    thread_id: str
    kind: FeedbackKind
    text: str
    step: int = 0
    #: The action this refers to, when there is one.
    action: str | None = None
    outcome: Outcome = Outcome.UNKNOWN
    #: False until the loop has actually shown it to the model. Unapplied human feedback is
    #: a broken promise, so it is tracked rather than assumed.
    applied: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def mark_applied(self) -> Feedback:
        return self.model_copy(update={"applied": True})

    @property
    def is_human(self) -> bool:
        return self.kind is not FeedbackKind.ASSESSMENT
