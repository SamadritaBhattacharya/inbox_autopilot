"""`RulesStore` — deterministic user rules, and the linear route's reason to exist.

A rule that matches means the task needs **no LLM call at all**: not to route it, not to
execute it. On free tiers where the binding constraint is requests-per-day, that is the
difference between one triage run and unlimited ones.

**Auto-send is off by default and cannot be switched on by configuration alone.** A rule
that sends mail without a human is the one path that would bypass the approval gate, so
enabling it requires setting the flag on the rule *and* passing an explicit opt-in when the
store is constructed. One accidental default must not be enough.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Rule:
    """One deterministic instruction.

    `patterns` are matched case-insensitively against the task text; `actions` are the
    verbs a linear worker will run. Both are plain data so a rule can be shown to a human
    and understood without reading code.
    """

    name: str
    patterns: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    #: Which intents this rule may serve. Empty means any.
    intents: tuple[str, ...] = ()
    #: Requires an explicit store-level opt-in as well; see `RulesStore`.
    auto_send: bool = False
    enabled: bool = True

    def matches(self, task: str, action: str | None = None) -> bool:
        if not self.enabled or not self.patterns:
            return False
        if self.intents and action and action not in self.intents:
            return False
        lowered = task.lower()
        return any(re.search(pattern, lowered) for pattern in self.patterns)


@runtime_checkable
class RulesStore(Protocol):
    def active(self) -> list[Rule]: ...
    def match(self, task: str, action: str | None = None) -> Rule | None: ...


DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(
        name="newsletters",
        patterns=(r"\bnewsletter", r"\bpromotions?\b", r"\bmarketing\b"),
        actions=("Archive",),
        intents=("triage", "archive"),
    ),
    Rule(
        name="notifications",
        patterns=(r"notifications?@", r"\bno-?reply\b", r"\bautomated\b"),
        actions=("Archive", "MarkRead"),
        intents=("triage", "archive"),
    ),
    Rule(
        name="mark-read-from",
        patterns=(r"\bmark (all|everything) .*read\b",),
        actions=("MarkRead",),
        intents=("triage", "archive"),
    ),
)


class InMemoryRulesStore:
    """Dev and test implementation; a durable store swaps in behind the port."""

    def __init__(
        self,
        rules: list[Rule] | None = None,
        *,
        allow_auto_send: bool = False,
    ) -> None:
        self._rules: list[Rule] = list(rules if rules is not None else DEFAULT_RULES)
        # The second lock on auto-send. A rule may ASK for it; only this grants it.
        self._allow_auto_send = allow_auto_send

    def active(self) -> list[Rule]:
        """Enabled rules, with auto-send stripped unless the store explicitly permits it."""
        return [
            rule if self._allow_auto_send else field_replace(rule, auto_send=False)
            for rule in self._rules
            if rule.enabled
        ]

    def match(self, task: str, action: str | None = None) -> Rule | None:
        """The first active rule matching this task, or None.

        First match rather than best match: rule precedence must be something a user can
        see and reorder, not the output of a scoring function they cannot inspect.
        """
        for rule in self.active():
            if rule.matches(task, action):
                return rule
        return None

    def add(self, rule: Rule) -> None:
        self._rules.append(rule)


def field_replace(rule: Rule, **updates: object) -> Rule:
    """`dataclasses.replace` under a name that says why it is here."""
    from dataclasses import replace

    return replace(rule, **updates)  # type: ignore[arg-type]


@dataclass
class NoRules:
    """An empty store. Every task routes by classifier."""

    rules: list[Rule] = field(default_factory=list)

    def active(self) -> list[Rule]:
        return []

    def match(self, task: str, action: str | None = None) -> Rule | None:
        return None
