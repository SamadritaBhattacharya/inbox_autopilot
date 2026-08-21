/**
 * Stage 3 — drop what is physically covered.
 *
 * This is the stage that makes modals work. When a compose panel or a confirmation dialog
 * opens, the inbox behind it is still in the DOM, still "visible" by computed style, and
 * still has valid geometry — but clicking any of it does nothing, because something else is
 * on top. An agent handed both layers will confidently click a row it cannot reach and then
 * have no idea why nothing happened.
 *
 * Culling the covered layer makes the modal *the* salient thing in the observation, which is
 * exactly the perception a human has. It is also why the loop needs no special "a dialog
 * appeared" handling: re-observe, and the dialog is simply what is there.
 *
 * **Two sources of truth, in order.** When the extractor could hit-test an element — asking
 * the browser "would a click here reach you?" — that answer is used directly. Geometry is
 * only a fallback for elements whose centre lies off-screen, where the question cannot be
 * asked. The ordering matters: geometric overlap cannot tell a transparent full-page scrim
 * from a real cover, and gets an open dialog exactly backwards, which is the one case this
 * stage exists for.
 *
 * **False positives are the risk in the fallback path.** Dropping an element that IS
 * reachable is worse than keeping one that is not — the agent loses an action it needed and
 * has no way to discover it. So the geometric bar is deliberately high.
 *
 * Ported from `backend/app/observation/funnel/occlusion.py`.
 */
import { area, overlaps, type RawElement } from "./raw";

/**
 * Fraction of an element that must be covered before it counts as unreachable. High on
 * purpose: partial overlap is normal in any layout with shadows and borders.
 */
export const COVER_THRESHOLD = 0.9;

/**
 * A cover this large relative to the viewport is a backdrop/scrim, not a real occluder.
 * Scrims are usually click-through or dismiss-on-click, and treating one as an occluder
 * would blank the entire observation — the worst possible failure for this stage.
 */
export const BACKDROP_AREA_RATIO = 0.95;

export interface OcclusionResult {
  kept: RawElement[];
  occluded: number;
}

/** Removes elements substantially covered by later-painted siblings. */
export class OcclusionCuller {
  private readonly threshold: number;

  constructor(options: { threshold?: number } = {}) {
    this.threshold = options.threshold ?? COVER_THRESHOLD;
  }

  apply(elements: RawElement[], viewportArea: number): OcclusionResult {
    if (elements.length < 2) return { kept: [...elements], occluded: 0 };

    // Original positions, captured before any sorting. Python could key this on object
    // identity; here an explicit map is both clearer and safe against duplicate nodeIds.
    const order = new Map<RawElement, number>();
    elements.forEach((element, i) => order.set(element, i));

    // Painted last is on top, so walking in descending paint order lets each element be
    // tested only against things that could actually cover it.
    const byPaint = [...elements].sort((a, b) => b.paintOrder - a.paintOrder);

    const kept: RawElement[] = [];
    const covers: RawElement[] = [];
    let occluded = 0;

    for (const element of byPaint) {
      // The browser's own hit-test, when we have it, is the answer — not evidence towards
      // it. Geometry cannot distinguish a transparent scrim from a real cover, and that
      // distinction is the whole job of this stage.
      if (element.receivesPointer === false) {
        occluded += 1;
        continue;
      }

      if (element.receivesPointer === null && this.isCovered(element, covers)) {
        occluded += 1;
        continue;
      }

      kept.push(element);
      if (!isBackdrop(element, viewportArea)) covers.push(element);
    }

    // Restore the original ordering; reading order is decided later, by a stage that knows
    // about reading order.
    kept.sort((a, b) => (order.get(a) ?? 0) - (order.get(b) ?? 0));
    return { kept, occluded };
  }

  private isCovered(element: RawElement, covers: RawElement[]): boolean {
    for (const cover of covers) {
      if (cover.nodeId === element.nodeId) continue;
      // An ancestor "covering" its own descendant is containment, not occlusion.
      if (element.parentId === cover.nodeId || cover.parentId === element.nodeId) continue;
      if (overlaps(element, cover) >= this.threshold) return true;
    }
    return false;
  }
}

function isBackdrop(element: RawElement, viewportArea: number): boolean {
  if (viewportArea <= 0) return false;
  return area(element) >= viewportArea * BACKDROP_AREA_RATIO;
}
