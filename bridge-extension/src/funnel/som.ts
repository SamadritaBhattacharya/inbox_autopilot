/**
 * Stage 6 — Set-of-Marks indexing.
 *
 * Every survivor gets a small integer. The model then references elements by **number**, and
 * the hidden `index -> geometry` map stays here, in the extension.
 *
 * That indirection is the whole safety property: the model cannot name a coordinate it was
 * never given, cannot click something it did not see listed, and cannot be talked into
 * targeting an element by an injected string — the only vocabulary it has is the set of
 * integers this stage minted this turn.
 *
 * **Indices are per-turn and never reused across turns.** They are assigned in reading order
 * over the survivors, so the same button legitimately gets a different number after the page
 * changes. Re-observing rebuilds them from scratch; a stale index is rejected at dispatch
 * rather than silently acted on.
 *
 * Ported from `backend/app/observation/funnel/som.py`.
 */
import { hasText, type RawElement } from "./raw";

/**
 * Vertical tolerance for "same row". Text baselines within a row differ by a few pixels, and
 * sorting strictly by y interleaves the columns of a table into nonsense.
 */
export const ROW_BAND = 12.0;

export interface Point {
  x: number;
  y: number;
}

export interface IndexedElements {
  indexed: RawElement[];
  /** index -> centre point. Never leaves the extension. */
  geometry: Map<number, Point>;
}

/** Top-to-bottom, then left-to-right — with y quantised into row bands. */
export function readingOrderKey(element: RawElement): [number, number] {
  return [Math.round(element.y / ROW_BAND), element.x];
}

function compareReadingOrder(a: RawElement, b: RawElement): number {
  const [aRow, aX] = readingOrderKey(a);
  const [bRow, bX] = readingOrderKey(b);
  return aRow - bRow || aX - bX;
}

/**
 * Assigns `[N]` and builds the extension-side geometry map.
 *
 * Only interactive elements and elements carrying text are indexed — an index is a promise
 * that there is something to do or read there, and handing the model numbers that do nothing
 * teaches it that numbers sometimes do nothing.
 */
export class SoMIndexer {
  apply(elements: RawElement[]): IndexedElements {
    // Copy before sorting: mutating the caller's array in place has bitten every funnel
    // that tried it, because a later stage still holds the original order.
    const ordered = [...elements].sort(compareReadingOrder);

    const indexed: RawElement[] = [];
    const geometry = new Map<number, Point>();
    let next = 1;

    for (const element of ordered) {
      if (!(element.interactive || hasText(element))) continue;
      indexed.push({ ...element, index: next });
      geometry.set(next, {
        x: element.x + element.width / 2,
        y: element.y + element.height / 2,
      });
      next += 1;
    }

    return { indexed, geometry };
  }
}
