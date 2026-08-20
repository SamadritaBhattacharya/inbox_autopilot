"""`FeedbackStore` — where corrections go, and how they become rules.

The promotion path is the part worth reading. A user who says "leave anything from my
manager alone" three separate times is not correcting a mistake, they are stating a
preference the system failed to learn. Counting recurring corrections turns that into a
**candidate rule**, which a human then confirms.

**Candidates, never auto-applied.** A rule silently created from an inferred preference is a
behaviour change nobody approved, on a surface where behaviour changes send email. The
system proposes; the human disposes. That keeps the learning loop honest and keeps
[ADR-006](../../docs/ADR.md) intact — nothing gains capability without a person saying yes.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.feedback.models import Feedback, FeedbackKind

#: How many times a correction must recur before it is worth proposing as a rule. Two is
#: coincidence; three is a pattern the user should not have to keep repeating.
PROMOTION_THRESHOLD = 3

_NOISE = re.compile(r"\b(please|thanks|no|dont|don't|stop|actually|instead|the|a|an)\b")
_SUFFIXES = ("ing", "ed", "es", "s")


def _stem(word: str) -> str:
    """Crudest possible stemmer: enough to match "archive" with "archiving".

    Without it, promotion effectively never fires — nobody phrases the same complaint the
    same way twice, and "stop archiving newsletters" would never match "don't archive
    newsletters". A real stemmer is a dependency and an LLM comparison is a per-pair API
    call, both disproportionate for a feature whose entire output is a suggestion a human
    reads and approves.
    """
    for suffix in _SUFFIXES:
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            word = word[: -len(suffix)]
            break
    return word.removesuffix("e")


def normalise(text: str) -> str:
    """A rough shape for comparing two corrections.

    Order-independent and stemmed, so wording and word order stop mattering. Over-matching
    is the safe direction here: the cost is proposing a rule the user declines.
    """
    lowered = _NOISE.sub(" ", text.lower())
    return " ".join(sorted({_stem(word) for word in re.findall(r"[a-z]{3,}", lowered)}))


@dataclass(frozen=True)
class RuleCandidate:
    """A preference the user has stated often enough to be worth encoding."""

    shape: str
    count: int
    examples: tuple[str, ...]

    @property
    def suggestion(self) -> str:
        return (
            f"You've told me this {self.count} times — shall I make it a standing rule? "
            f'e.g. "{self.examples[0]}"'
        )


@runtime_checkable
class FeedbackStore(Protocol):
    async def record(self, feedback: Feedback) -> None: ...
    async def for_thread(self, thread_id: str) -> list[Feedback]: ...
    async def pending(self, thread_id: str) -> list[Feedback]: ...
    async def mark_applied(self, thread_id: str) -> None: ...
    async def candidates(self) -> list[RuleCandidate]: ...


class InMemoryFeedbackStore:
    """Dev and test implementation; a durable store swaps in behind the port."""

    def __init__(self, *, promotion_threshold: int = PROMOTION_THRESHOLD) -> None:
        self._by_thread: dict[str, list[Feedback]] = defaultdict(list)
        self._threshold = promotion_threshold

    async def record(self, feedback: Feedback) -> None:
        self._by_thread[feedback.thread_id].append(feedback)

    async def for_thread(self, thread_id: str) -> list[Feedback]:
        return list(self._by_thread[thread_id])

    async def pending(self, thread_id: str) -> list[Feedback]:
        """Human feedback the loop has not yet shown the model.

        Assessments are excluded: they are the model's own output, and replaying them back
        as instructions would have it argue with itself.
        """
        return [f for f in self._by_thread[thread_id] if f.is_human and not f.applied]

    async def mark_applied(self, thread_id: str) -> None:
        self._by_thread[thread_id] = [
            f.mark_applied() if f.is_human and not f.applied else f
            for f in self._by_thread[thread_id]
        ]

    async def candidates(self) -> list[RuleCandidate]:
        """Corrections repeated often enough to propose as rules.

        Spans every thread on purpose: a preference the user states once per run is exactly
        the preference worth encoding, and a per-thread view would never see it.
        """
        corrections = [
            f
            for entries in self._by_thread.values()
            for f in entries
            if f.kind is FeedbackKind.CORRECTION and f.text.strip()
        ]
        shapes = Counter(normalise(f.text) for f in corrections)

        return sorted(
            (
                RuleCandidate(
                    shape=shape,
                    count=count,
                    examples=tuple(
                        f.text for f in corrections if normalise(f.text) == shape
                    )[:3],
                )
                for shape, count in shapes.items()
                if count >= self._threshold and shape
            ),
            key=lambda candidate: candidate.count,
            reverse=True,
        )
