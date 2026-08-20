"""Self-assessment — did the last action actually do anything?

Pure functions. Two jobs: pull the model's own verdict out of its reasoning, and derive the
ground truth from what actually happened.

**Why the derived outcome matters more than the stated one.** The model's assessment is a
claim; `NO_EFFECT` is a measurement. An action that *succeeded* — the click landed, no error
returned — while leaving the page byte-identical is the single most common way an agent
wastes a run, and it is invisible to a success flag. Feeding that measurement back on the
next turn is what lets the agent change approach at step 3 instead of step 8.

**The assessment is never enforced with a retry.** A second full LLM call to nudge a
formatting label would double the cost of every turn to obtain a string we already
approximate. The prompt asks; the derived outcome covers the case where the model does not
answer, and the structural guards cover the case where both are wrong.
"""
from __future__ import annotations

import re

from inbox_contracts import ActionResult

from app.feedback.models import Outcome

#: A leading "Assessment: ..." line, tolerating bullets, bold, and heading marks.
_ASSESSMENT = re.compile(r"^[\s*_#>\-]*assessment\b\s*[:\-–]\s*(.+)$", re.IGNORECASE)

MAX_ASSESSMENT_CHARS = 300


def split_assessment(text: str) -> tuple[str | None, str]:
    """Separate a leading assessment line from the rest of the reasoning.

    Returns `(assessment, remaining_text)`. The assessment is surfaced to the cockpit as its
    own signal — "the agent noticed the click did nothing" is a different kind of
    information from "the agent is about to click", and collapsing them into one blob makes
    both harder to read.
    """
    lines = text.splitlines()
    for position, line in enumerate(lines[:3]):
        match = _ASSESSMENT.match(line.strip())
        if match and match.group(1).strip():
            assessment = match.group(1).strip()[:MAX_ASSESSMENT_CHARS]
            rest = "\n".join(lines[:position] + lines[position + 1 :]).strip()
            return assessment, rest
    return None, text


def derive_outcome(
    *,
    result: ActionResult | None,
    page_changed: bool,
    is_first_action: bool = False,
) -> Outcome:
    """What the last action achieved, measured rather than claimed."""
    if result is None or is_first_action:
        return Outcome.UNKNOWN
    if not result.success:
        return Outcome.FAILED
    return Outcome.PROGRESSED if page_changed else Outcome.NO_EFFECT


def outcome_note(outcome: Outcome, action: str | None) -> str | None:
    """The line fed back to the model on the next turn.

    Phrased as an observation rather than an instruction. Telling the model what to do next
    pre-empts the reasoning we want it to do; telling it what happened gives it the one fact
    it could not otherwise have.
    """
    verb = action or "your last action"
    if outcome is Outcome.NO_EFFECT:
        return (
            f"Note: {verb} reported success but the page did not change. It may not have "
            "reached its target. Check before repeating it."
        )
    if outcome is Outcome.FAILED:
        return f"Note: {verb} failed. Do not simply retry it — work out why first."
    return None
