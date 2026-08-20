"""What the executor extracts before the funnel prunes it.

`RawElement` is the widest the data ever gets: geometry, paint order, tree position, and
raw names straight off the page. **None of this crosses the wire.** The funnel narrows it
to the `Element` contract, and the parts that stay behind — coordinates above all — are
precisely the parts that make the token scheme meaningful.

Geometry is in **viewport coordinates**, because that is what the executor needs to click
and what the visibility and occlusion stages reason about. Converting to page coordinates
would make "is this on screen?" depend on scroll state at read time, which is a race.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MailView = Literal["inbox", "thread", "compose", "search", "sent", "drafts", "calendar"]


@dataclass(frozen=True, slots=True)
class RawElement:
    """One candidate element, straight from the DOM/accessibility snapshot."""

    node_id: int
    role: str
    name: str = ""
    value: str | None = None

    # Viewport-relative geometry. Stays executor-side forever.
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0

    #: Can a user act on this? Non-interactive elements survive only if they carry text
    #: worth reading (an email subject line, a sender name).
    interactive: bool = False
    #: Does computed style render it at all? (`display:none`, `visibility:hidden`, opacity)
    displayed: bool = True
    #: Higher paints later, i.e. on top. Used only for the geometric occlusion fallback.
    paint_order: int = 0
    #: The browser's own verdict on "would a click here reach this element?" —
    #: True (yes), False (something else is on top), None (could not be tested).
    #: Authoritative when present: geometry only ever approximates this.
    receives_pointer: bool | None = None

    parent_id: int | None = None
    depth: int = 0

    #: Assigned by `SoMIndexer`; None until then.
    index: int | None = None

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def has_text(self) -> bool:
        return bool(self.name.strip() or (self.value or "").strip())

    def overlaps(self, other: RawElement) -> float:
        """Fraction of THIS element's area covered by `other`. 0.0 when disjoint."""
        if self.area <= 0:
            return 0.0
        overlap_w = min(self.right, other.right) - max(self.x, other.x)
        overlap_h = min(self.bottom, other.bottom) - max(self.y, other.y)
        if overlap_w <= 0 or overlap_h <= 0:
            return 0.0
        return (overlap_w * overlap_h) / self.area


@dataclass(frozen=True, slots=True)
class PageMeta:
    """Everything about the page that is not an element.

    `context_ref` and `thread_ref` hold RAW identifiers. They are tokenized in the funnel
    before they reach `Observation`, which is why the contract has `context_id` and no
    `url` — on an email surface a URL is an identifier.
    """

    context_ref: str
    title: str = ""
    viewport_width: int = 1280
    viewport_height: int = 800
    scroll_x: int = 0
    scroll_y: int = 0

    view: MailView = "inbox"
    thread_ref: str | None = None
    unread_count: int | None = None
    compose_open: bool = False


@dataclass
class FunnelReport:
    """What each stage removed.

    Kept because the alternative is an agent that cannot tell "there is nothing else here"
    from "I hid the rest from you". Silent truncation makes an agent confidently wrong, so
    the counts are carried out of the funnel and surfaced, not logged and forgotten.
    """

    extracted: int = 0
    hidden: int = 0  # not rendered at all
    offscreen_above: int = 0  # scrolled past; reachable by scrolling UP
    offscreen_below: int = 0  # not yet reached; reachable by scrolling DOWN
    occluded: int = 0  # covered by something on top
    collapsed: int = 0  # layout wrappers folded into their meaningful child
    budget_dropped: int = 0  # cut to fit the token budget
    shown: int = 0
    stages: list[str] = field(default_factory=list)

    @property
    def offscreen(self) -> int:
        return self.offscreen_above + self.offscreen_below

    @property
    def reachable_but_unlisted(self) -> int:
        """What the agent could still get to that is not in the list.

        Off-screen plus budget-dropped. Hidden, occluded, and collapsed elements are NOT
        counted: they are not actionable, so reporting them would just teach the agent to
        scroll after content that does not exist.
        """
        return self.offscreen_above + self.offscreen_below + self.budget_dropped
