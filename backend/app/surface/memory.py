"""Procedural memory — where things are, remembered across runs.

Three kinds of memory exist in this project, with different lifetimes and different trust
levels, and conflating them is the whole way a "let's add memory" feature goes wrong:

    working    — this run's scratchpad         — one run       — AgentState.agent_memory
    episodic   — what happened                 — forever, offline — TrajectoryStore
    procedural — where Send is, how to compose — across runs, online — THIS MODULE

This is the third kind, and it lives in the **executor**, not the brain — only the executor
ever sees the DOM, and keeping it here is what lets `PlaywrightEmailSurface` and
`ExtensionEmailSurface` stay swappable behind one port. The brain never learns that this
store exists; it keeps emitting the same indexed `ActionCall`s it always has, and a surface
that wants to answer one from a warm cache is free to do so entirely on its own side.

**A cached locator is a hypothesis, never a fact.** Nothing in this module ever hands back a
descriptor without the caller re-verifying it still matches the live page — `recall()` takes
the verifier as an argument for exactly that reason. One DOM query is free; a wrong trusted
click on a button that used to be Send is unbounded. This module never dispatches, never
resolves a token, and never touches approval — it answers exactly one question, "have I seen
something like this before, and does it still check out", and nothing else.

**Never keyed on anything that dies with the next paint.** Indices are rebuilt every turn by
construction; coordinates die on window resize. What survives is a semantic descriptor —
role, an accessible-name pattern, roughly where in the page tree — matched fresh against
whatever is on screen right now, keyed by a `PageSignature` that names the KIND of screen
("compose", on "mail.google.com"), never a URL or a page title (a Gmail title routinely
embeds the signed-in user's own address).

See `docs/IMPROVEMENT-PLAN.md` §B5 for the five rules this module exists to hold to, and for
what is deliberately NOT built here: nothing in this codebase yet asks the brain to emit a
verb with no index, and nothing wires this store into a live surface. Both need a real
browser to verify against, which this environment does not have. What is here — the store,
its decay, its provenance discipline, its refusal to hold anything that looks like PII — is
fully exercised by fakes and needs none.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.security.patterns import EMAIL_RE, find_phones

#: A locator survives this many CONSECUTIVE failed verifications before it is evicted.
#: Gmail ships UI changes, and a cache that never invalidates is worse than no cache at all,
#: because it is fast and confidently wrong. One is not enough — a single transient DOM
#: hiccup (an animation mid-render, a slow-loading dialog) must not evict a locator that is
#: actually still correct.
MAX_CONSECUTIVE_MISSES = 2


class Provenance(StrEnum):
    """Where a locator came from, and therefore how much it should be trusted.

    Two levels, never conflated. A human who curates a locator by hand has verified it
    against the real product; a locator this store wrote after one successful dispatch has
    verified nothing except that it worked ONCE. Mixing the two into one trust level is how
    a store ends up confidently wrong — see the recovery `SkillRegistry`, which this is
    deliberately kept separate from for the identical reason.
    """

    CURATED = "curated"
    LEARNED = "learned"


class UnsafeMemoryValue(ValueError):
    """Refused: the value looks like it carries real PII.

    Raised rather than silently stripped. A store that quietly redacts and continues trains
    whoever is writing to it to not notice; a store that refuses tells them immediately, on
    the write that matters, before anything is persisted.
    """


#: Matches this project's own token scheme (P17, C4, H2, T9 — see `security/patterns.py`).
#: A token is a REFERENCE to PII, not PII itself, and referring to "the same recipient
#: field across runs" by token would be a reasonable thing to want — so tokens are let
#: through deliberately, not by oversight.
_TOKEN_RE = re.compile(r"\b(?:P|H|C|T)\d+\b")


def _contains_unsafe_pii(text: str) -> bool:
    """Emails and phone numbers, specifically — the structural, high-confidence classes.

    Deliberately not names: a locator's `name_pattern` legitimately says things like "Send"
    or "Subject line" or a sender's role-label, and a name-shaped string is not evidence of
    a real person the way an email or a phone number is. Reusing `security/patterns.py`
    rather than a second implementation is the point — one definition of "this is PII",
    used everywhere it matters.
    """
    without_tokens = _TOKEN_RE.sub("", text)
    return bool(EMAIL_RE.search(without_tokens)) or bool(find_phones(without_tokens))


@dataclass(frozen=True)
class PageSignature:
    """The KIND of screen a locator applies to — never a URL, never a title.

    `host` names the product ("mail.google.com"); `view` names the screen within it
    ("compose", "inbox", "thread" — the same vocabulary `Observation.mail.view` already
    uses). Both are structural. A Gmail page TITLE routinely embeds the signed-in user's own
    address ("Inbox (12) - alice@corp.com - Gmail"), which is exactly the kind of value this
    store must never be keyed on, let alone store.
    """

    host: str
    view: str

    def __post_init__(self) -> None:
        for field_name, value in (("host", self.host), ("view", self.view)):
            if _contains_unsafe_pii(value):
                raise UnsafeMemoryValue(f"PageSignature.{field_name} looks like it carries PII")


@dataclass(frozen=True)
class LocatorDescriptor:
    """A semantic address for one element — stable across turns, unlike an index.

    `container_path` is a coarse structural hint ("dialog", "dialog>footer") for
    disambiguating between two elements that share a role and name — never a CSS selector or
    an XPath, both of which are exactly the kind of brittle, DOM-shape-specific detail a UI
    refactor breaks for no reason connected to what the element actually IS.
    """

    role: str
    name_pattern: str
    container_path: str = ""

    def __post_init__(self) -> None:
        for field_name, value in (
            ("role", self.role),
            ("name_pattern", self.name_pattern),
            ("container_path", self.container_path),
        ):
            if _contains_unsafe_pii(value):
                raise UnsafeMemoryValue(f"LocatorDescriptor.{field_name} looks like it carries PII")


@dataclass(frozen=True)
class LocatorEntry:
    """One remembered locator, plus enough about its own track record to be trusted or not."""

    descriptor: LocatorDescriptor
    provenance: Provenance
    hits: int = 0
    consecutive_misses: int = 0
    last_used: float = field(default_factory=time.time)


@runtime_checkable
class ProceduralMemoryStore(Protocol):
    def remember(
        self,
        signature: PageSignature,
        verb: str,
        descriptor: LocatorDescriptor,
        *,
        provenance: Provenance = Provenance.LEARNED,
    ) -> None: ...

    def recall(
        self, signature: PageSignature, verb: str, *, verify
    ) -> LocatorDescriptor | None: ...

    def forget(self, signature: PageSignature, verb: str) -> None: ...


class InMemoryProceduralMemory:
    """Dev and test implementation; a durable store swaps in behind the port.

    Same shape as every other store in this codebase (`InMemoryRulesStore`,
    `InMemoryTrajectoryStore`, `InMemoryFeedbackStore`) — process-lifetime only, which is
    honest about what it is: nothing here yet writes to a durable backing store, because
    nothing yet calls this from a live surface to persist. Building the durable half before
    there is a caller would be dead code with no way to prove it correct.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[PageSignature, str], LocatorEntry] = {}

    def remember(
        self,
        signature: PageSignature,
        verb: str,
        descriptor: LocatorDescriptor,
        *,
        provenance: Provenance = Provenance.LEARNED,
    ) -> None:
        """Record a locator. A LEARNED write never overwrites a CURATED one.

        A human curated that entry on purpose; one successful dispatch by the agent has not
        earned the right to replace their judgement. Writing a CURATED entry always wins,
        including over a previous CURATED entry — a human is allowed to correct themselves.
        """
        if _contains_unsafe_pii(verb):
            raise UnsafeMemoryValue(f"verb {verb!r} looks like it carries PII")

        key = (signature, verb)
        existing = self._entries.get(key)
        if (
            existing is not None
            and existing.provenance is Provenance.CURATED
            and provenance is not Provenance.CURATED
        ):
            return
        self._entries[key] = LocatorEntry(descriptor=descriptor, provenance=provenance)

    def recall(
        self, signature: PageSignature, verb: str, *, verify
    ) -> LocatorDescriptor | None:
        """A locator IF one is remembered AND `verify` confirms it still matches.

        `verify(descriptor) -> bool` is supplied by the caller because only the caller has a
        live page to check against — this store has no DOM, on purpose, so it cannot be the
        thing that decides a descriptor is still good. A miss updates the entry's own
        decay counter; `MAX_CONSECUTIVE_MISSES` consecutive misses evicts it, regardless of
        provenance — a curated locator that no longer matches is exactly as wrong as a
        learned one, and Gmail does not spare hand-written entries when it ships a redesign.
        """
        key = (signature, verb)
        entry = self._entries.get(key)
        if entry is None:
            return None

        if verify(entry.descriptor):
            self._entries[key] = replace(
                entry, hits=entry.hits + 1, consecutive_misses=0, last_used=time.time()
            )
            return entry.descriptor

        misses = entry.consecutive_misses + 1
        if misses >= MAX_CONSECUTIVE_MISSES:
            del self._entries[key]
        else:
            self._entries[key] = replace(entry, consecutive_misses=misses)
        return None

    def forget(self, signature: PageSignature, verb: str) -> None:
        self._entries.pop((signature, verb), None)

    def entry(self, signature: PageSignature, verb: str) -> LocatorEntry | None:
        """Read the raw entry, decay counters and all — for tests and diagnostics.

        Not on the `ProceduralMemoryStore` port: a caller with a live page should only ever
        need `recall`'s already-verified answer, never the bookkeeping behind it.
        """
        return self._entries.get((signature, verb))

    def __len__(self) -> int:
        return len(self._entries)
