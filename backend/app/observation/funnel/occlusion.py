"""Stage 3 — drop what is physically covered.

This is the stage that makes modals work. When a compose panel or a confirmation dialog
opens, the inbox behind it is still in the DOM, still "visible" by computed style, and
still has valid geometry — but clicking any of it does nothing, because something else is
on top. An agent handed both layers will confidently click a row it cannot reach and then
have no idea why nothing happened.

Culling the covered layer means the modal becomes *the* salient thing in the observation,
which is exactly the perception a human has. It is also why the loop needs no special
"a dialog appeared" handling: re-observe, and the dialog is simply what is there.

**Two sources of truth, in order.** When the extractor could hit-test an element — asking
the browser "would a click here reach you?" — that answer is used directly. Geometry is
only a fallback for elements whose centre lies off-screen, where the question cannot be
asked. This ordering matters: geometric overlap cannot tell a transparent full-page scrim
from a real cover, and gets an open dialog exactly backwards, which is the one case this
stage exists for.

**False positives are the risk in the fallback path.** Dropping an element that IS reachable
is worse than keeping one that is not — the agent loses an action it needed and has no way
to discover it. So the geometric bar is deliberately high: only a *substantial* cover by
something painted later, and never by a full-page layer.
"""
from __future__ import annotations

from app.observation.raw import RawElement

#: Fraction of an element that must be covered before it is considered unreachable.
#: High on purpose: partial overlap is normal in any layout with shadows and borders.
COVER_THRESHOLD = 0.9

#: A cover this large relative to the viewport is a backdrop/scrim, not a real occluder.
#: Scrims are usually click-through or dismiss-on-click, and treating one as an occluder
#: would blank the entire observation — the worst possible failure for this stage.
BACKDROP_AREA_RATIO = 0.95


class OcclusionCuller:
    """Removes elements substantially covered by later-painted siblings."""

    def __init__(self, *, threshold: float = COVER_THRESHOLD) -> None:
        self._threshold = threshold

    def apply(
        self, elements: list[RawElement], viewport_area: float
    ) -> tuple[list[RawElement], int]:
        """Returns `(kept, occluded_count)`."""
        if len(elements) < 2:
            return list(elements), 0

        # Painted last is on top, so walking in descending paint order lets each element be
        # tested only against things that could actually cover it.
        by_paint = sorted(elements, key=lambda e: e.paint_order, reverse=True)

        kept: list[RawElement] = []
        occluded = 0
        covers: list[RawElement] = []

        for element in by_paint:
            # The browser's own hit-test, when we have it, is the answer — not evidence
            # towards it. Geometry cannot distinguish a transparent scrim from a real
            # cover, and that distinction is the whole job of this stage.
            if element.receives_pointer is False:
                occluded += 1
                continue

            if element.receives_pointer is None and self._is_covered(element, covers):
                occluded += 1
                continue

            kept.append(element)
            if not self._is_backdrop(element, viewport_area):
                covers.append(element)

        # Restore the original ordering; reading order is decided later, by a stage that
        # knows about reading order.
        order = {id(e): i for i, e in enumerate(elements)}
        kept.sort(key=lambda e: order[id(e)])
        return kept, occluded

    def _is_covered(self, element: RawElement, covers: list[RawElement]) -> bool:
        for cover in covers:
            if cover.node_id == element.node_id:
                continue
            # An ancestor "covering" its own descendant is just containment, not occlusion.
            if element.parent_id == cover.node_id or cover.parent_id == element.node_id:
                continue
            if element.overlaps(cover) >= self._threshold:
                return True
        return False

    @staticmethod
    def _is_backdrop(element: RawElement, viewport_area: float) -> bool:
        if viewport_area <= 0:
            return False
        return element.area >= viewport_area * BACKDROP_AREA_RATIO
