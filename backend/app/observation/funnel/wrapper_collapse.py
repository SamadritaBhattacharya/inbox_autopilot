"""Stage 4 — fold layout wrappers into the thing that matters.

Modern web apps nest deeply: `div > div > div > button` is ordinary, and every one of those
divs has a role, a box, and a name inherited from its subtree. Left alone, the model sees
four entries that are all "the same button" and has to guess which one to click — and
picking a wrapper often does nothing, because the click handler is on the leaf.

A wrapper is an element that adds no information: not interactive, and geometrically
indistinguishable from a single child it contains. Fold it away and keep the child.

**Direction matters.** The child is kept, not the parent. The parent is where the layout
lives; the child is where the behaviour lives.
"""
from __future__ import annotations

from app.observation.raw import RawElement

#: How closely a parent's box must match its child's before the parent is considered pure
#: layout. Padding of a pixel or two is still a wrapper; a parent noticeably bigger than its
#: child is doing something (a row containing a button, say) and must survive.
GEOMETRY_TOLERANCE = 0.92


class WrapperCollapser:
    """Removes non-interactive parents that merely wrap one child."""

    def __init__(self, *, tolerance: float = GEOMETRY_TOLERANCE) -> None:
        self._tolerance = tolerance

    def apply(self, elements: list[RawElement]) -> tuple[list[RawElement], int]:
        """Returns `(kept, collapsed_count)`."""
        children_by_parent: dict[int, list[RawElement]] = {}
        for element in elements:
            if element.parent_id is not None:
                children_by_parent.setdefault(element.parent_id, []).append(element)

        kept: list[RawElement] = []
        collapsed = 0

        for element in elements:
            if self._is_wrapper(element, children_by_parent.get(element.node_id, [])):
                collapsed += 1
                continue
            kept.append(element)

        return kept, collapsed

    def _is_wrapper(self, element: RawElement, children: list[RawElement]) -> bool:
        # Interactive elements are never wrappers, however plain they look — the handler
        # may well be on this node and the child may be a span with no behaviour at all.
        if element.interactive:
            return False

        # Exactly one child: with two or more, this element is a container that gives its
        # children meaning by grouping them, and removing it loses that grouping.
        if len(children) != 1:
            return False

        child = children[0]

        # If the parent carries text the child does not, it is contributing information.
        parent_text = element.name.strip()
        if parent_text and parent_text != child.name.strip():
            return False

        if element.area <= 0 or child.area <= 0:
            return False
        return child.area / element.area >= self._tolerance
