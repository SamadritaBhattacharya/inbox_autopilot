"""`SkillRegistry` — ranks remedies for a cause and builds the four options.

Always exactly four: three real moves scored against the cause, plus a free-form escape
hatch. The shape is fixed because a human answering under mild pressure should not have to
read a variable-length menu — and because a curated registry cannot anticipate everything,
so admitting that with option 4 is more honest than pretending otherwise.

**Self-heal must terminate.** Strategies already tried in this run are excluded, so a second
failure of the same cause offers genuinely different moves rather than the one that just
failed. After enough attempts the registry returns nothing and the run ends typed — an agent
that can always offer another remedy is an agent that can loop on remediation forever.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.recovery.causes import Cause
from app.recovery.strategies import (
    ALL_STRATEGIES,
    BY_NAME,
    FREEFORM_OPTION,
    Option,
    RemediationStrategy,
)

#: How many times one cause may be remediated before the run gives up. The third occurrence
#: means the remedies are not working, and asking a fourth time is nagging rather than helping.
MAX_ATTEMPTS_PER_CAUSE = 2

#: Real strategies offered alongside the free-form option.
RANKED_SLOTS = 3

#: Below this, a strategy is not a genuine fit; padding the list with weak suggestions
#: teaches people that the options are noise.
MIN_FIT = 0.15


@runtime_checkable
class SkillRegistry(Protocol):
    def strategies_for(
        self, cause: Cause, *, exclude: set[str] | None = None
    ) -> list[RemediationStrategy]: ...


class CuratedSkillRegistry:
    """The v1 registry: a fixed, versioned, tested set."""

    def __init__(self, strategies: tuple = ALL_STRATEGIES) -> None:
        self._strategies = strategies

    def strategies_for(
        self, cause: Cause, *, exclude: set[str] | None = None
    ) -> list[RemediationStrategy]:
        excluded = exclude or set()
        scored = [
            (strategy.applies_to(cause), index, strategy)
            for index, strategy in enumerate(self._strategies)
            if strategy.name not in excluded and strategy.applies_to(cause) >= MIN_FIT
        ]
        # Sort by fit, then by registry position — equal scores must break deterministically
        # or identical evidence produces different options on different runs.
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [strategy for _, _, strategy in scored[:RANKED_SLOTS]]

    def options_for(self, cause: Cause, *, exclude: set[str] | None = None) -> list[Option]:
        """The four ranked options, `[1]` marked Recommended."""
        ranked = self.strategies_for(cause, exclude=exclude)
        options = [
            Option(
                n=index + 1,
                label=strategy.label,
                detail=strategy.detail,
                recommended=index == 0,
            )
            for index, strategy in enumerate(ranked)
        ]
        # Free-form always sits last and always exists, whatever the numbering above did.
        return [*options, Option(**{**FREEFORM_OPTION.__dict__, "n": len(options) + 1})]

    @staticmethod
    def guidance_for(name: str) -> str:
        strategy = BY_NAME.get(name)
        return strategy.guidance() if strategy else ""

    def exhausted(self, attempts: list[str], cause: Cause) -> bool:
        """Has this cause been remediated too many times already?"""
        return attempts.count(cause.value) >= MAX_ATTEMPTS_PER_CAUSE
