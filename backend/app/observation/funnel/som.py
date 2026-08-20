"""Stage 6 — Set-of-Marks indexing.

Every survivor gets a small integer. The model then references elements by **number**, and
the hidden `index -> geometry` map stays here, in the executor.

That indirection is the whole safety property: the model cannot name a coordinate it was
never given, cannot click something it did not see listed, and cannot be talked into
targeting an element by an injected string, because the only vocabulary it has is the set
of integers this stage minted this turn.

**Indices are per-turn and are never reused across turns.** They are assigned in reading
order over the elements that survived the funnel, so the same button legitimately gets a
different number after the page changes. Re-observing rebuilds them from scratch; a stale
index from a previous turn is rejected at dispatch rather than silently acted on.
"""
from __future__ import annotations

from dataclasses import replace

from app.observation.raw import RawElement

#: Vertical tolerance for "same row". Text baselines within a row differ by a few pixels,
#: and sorting strictly by y would interleave columns of a table into nonsense.
ROW_BAND = 12.0


def reading_order_key(element: RawElement) -> tuple[float, float]:
    """Top-to-bottom, then left-to-right — with y quantised into row bands."""
    return (round(element.y / ROW_BAND), element.x)


class SoMIndexer:
    """Assigns `[N]` and builds the executor-side geometry map."""

    def apply(
        self, elements: list[RawElement]
    ) -> tuple[list[RawElement], dict[int, tuple[float, float]]]:
        """Returns `(indexed_elements, index -> centre point)`.

        Only interactive elements and elements carrying text are indexed — an index is a
        promise that there is something to do or read there, and handing the model numbers
        that do nothing teaches it that numbers sometimes do nothing.
        """
        ordered = sorted(elements, key=reading_order_key)

        indexed: list[RawElement] = []
        geometry: dict[int, tuple[float, float]] = {}
        next_index = 1

        for element in ordered:
            if not (element.interactive or element.has_text):
                continue
            indexed.append(replace(element, index=next_index))
            geometry[next_index] = (
                element.x + element.width / 2,
                element.y + element.height / 2,
            )
            next_index += 1

        return indexed, geometry
