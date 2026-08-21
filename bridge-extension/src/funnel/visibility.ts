/**
 * Stage 2 — drop what the user cannot see.
 *
 * The single biggest reduction in the funnel, and the cheapest. A real mail page carries
 * thousands of nodes that are styled away, collapsed, or scrolled far out of view; none can
 * be acted on, and every one costs tokens the budget needs elsewhere.
 *
 * Two categories, deliberately counted **separately**:
 *
 * - **hidden** — not rendered at all. Gone, and nothing the agent could do about it.
 * - **off-screen** — rendered, but outside the viewport. The agent CAN reach these by
 *   scrolling, so the count is surfaced. Conflating the two would tell the agent to scroll
 *   after content that does not exist.
 *
 * Ported from `backend/app/observation/funnel/visibility.py`.
 */
import { bottom, right, type PageMeta, type RawElement } from "./raw";

/** Below this, an element is a tracking pixel or a collapsed container, not a target. */
export const MIN_DIMENSION = 2.0;

/**
 * How far outside the viewport still counts as "nearly visible". A row peeking in by a few
 * pixels is genuinely reachable, and dropping it makes the list flicker between turns as the
 * page settles by a pixel or two.
 */
export const VIEWPORT_MARGIN = 4.0;

export interface VisibilityResult {
  kept: RawElement[];
  hidden: number;
  offscreenAbove: number;
  offscreenBelow: number;
}

export interface VisibilityOptions {
  margin?: number;
  minDimension?: number;
}

/** Keeps elements that are rendered AND within (or touching) the viewport. */
export class VisibilityFilter {
  private readonly margin: number;
  private readonly min: number;

  constructor(options: VisibilityOptions = {}) {
    this.margin = options.margin ?? VIEWPORT_MARGIN;
    this.min = options.minDimension ?? MIN_DIMENSION;
  }

  /**
   * Above and below are counted separately because "there is more" without "which way" is
   * not actionable: the agent scrolls one direction, sees the count unchanged, and scrolls
   * the same way again.
   */
  apply(elements: RawElement[], meta: PageMeta): VisibilityResult {
    const kept: RawElement[] = [];
    let hidden = 0;
    let offscreenAbove = 0;
    let offscreenBelow = 0;

    for (const element of elements) {
      if (!this.isRendered(element)) {
        hidden += 1;
        continue;
      }
      if (!this.inViewport(element, meta)) {
        if (bottom(element) <= 0) offscreenAbove += 1;
        else offscreenBelow += 1;
        continue;
      }
      kept.push(element);
    }

    return { kept, hidden, offscreenAbove, offscreenBelow };
  }

  private isRendered(element: RawElement): boolean {
    if (!element.displayed) return false;
    // Zero-area nodes cannot be clicked or read. This also catches the many wrappers that
    // exist purely to hold a CSS rule.
    return element.width >= this.min && element.height >= this.min;
  }

  /**
   * Geometry is viewport-relative, so this is a pure box test — no scroll maths. Doing it in
   * page coordinates would make the answer depend on when the scroll offset was read
   * relative to the element boxes, a race that surfaces as elements randomly appearing and
   * vanishing between turns.
   */
  private inViewport(element: RawElement, meta: PageMeta): boolean {
    return (
      right(element) > -this.margin &&
      bottom(element) > -this.margin &&
      element.x < meta.viewportWidth + this.margin &&
      element.y < meta.viewportHeight + this.margin
    );
  }
}
