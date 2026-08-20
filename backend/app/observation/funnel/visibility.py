"""Stage 2 — drop what the user cannot see.

The single biggest reduction in the funnel, and the cheapest. A real mail page carries
thousands of nodes that are styled away, collapsed, or scrolled far out of view; none of
them can be acted on, and every one costs tokens the budget needs elsewhere.

Two categories, deliberately counted **separately**:

- **hidden** — not rendered at all. Gone, and there is nothing the agent could do about it.
- **off-screen** — rendered, but outside the viewport. The agent CAN reach these by
  scrolling, so the count is surfaced. Conflating the two would tell the agent to scroll
  after content that does not exist.
"""
from __future__ import annotations

from app.observation.raw import PageMeta, RawElement

#: Below this, an element is a tracking pixel or a collapsed container, not a target.
MIN_DIMENSION = 2.0

#: How far outside the viewport still counts as "nearly visible". A row peeking in by a few
#: pixels is genuinely reachable, and dropping it makes the list flicker between turns as
#: the page settles by a pixel or two.
VIEWPORT_MARGIN = 4.0


class VisibilityFilter:
    """Keeps elements that are rendered AND within (or touching) the viewport."""

    def __init__(
        self, *, margin: float = VIEWPORT_MARGIN, min_dimension: float = MIN_DIMENSION
    ) -> None:
        self._margin = margin
        self._min = min_dimension

    def apply(
        self, elements: list[RawElement], meta: PageMeta
    ) -> tuple[list[RawElement], int, int]:
        """Returns `(kept, hidden_count, offscreen_count)`."""
        kept: list[RawElement] = []
        hidden = 0
        offscreen = 0

        for element in elements:
            if not self._is_rendered(element):
                hidden += 1
                continue
            if not self._in_viewport(element, meta):
                offscreen += 1
                continue
            kept.append(element)

        return kept, hidden, offscreen

    def _is_rendered(self, element: RawElement) -> bool:
        if not element.displayed:
            return False
        # Zero-area nodes cannot be clicked or read. This also catches the many wrapper
        # elements that exist purely to hold a CSS rule.
        return element.width >= self._min and element.height >= self._min

    def _in_viewport(self, element: RawElement, meta: PageMeta) -> bool:
        """Geometry is viewport-relative, so this is a pure box test — no scroll maths.

        Doing it in page coordinates instead would make the answer depend on when the
        scroll offset was read relative to the element boxes, which is a race that shows up
        as elements randomly appearing and vanishing between turns.
        """
        return (
            element.right > -self._margin
            and element.bottom > -self._margin
            and element.x < meta.viewport_width + self._margin
            and element.y < meta.viewport_height + self._margin
        )
