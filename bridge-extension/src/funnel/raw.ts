/**
 * What the extension extracts before the funnel prunes it.
 *
 * `RawElement` is the widest this data ever gets: geometry, paint order, tree position, and
 * raw names straight off the page. **None of it crosses the wire.** The funnel narrows it to
 * the `Element` contract, and the parts left behind — coordinates above all — are precisely
 * what makes the index scheme meaningful: the model cannot name a coordinate it was never
 * given.
 *
 * Geometry is in **viewport coordinates**, because that is what the debugger needs in order
 * to click and what visibility and occlusion reason about. Page coordinates would make "is
 * this on screen?" depend on scroll state at read time, which is a race that shows up as
 * elements randomly appearing and vanishing between turns.
 *
 * Ported from `backend/app/observation/raw.py`; the two must stay in lockstep, which is what
 * `tests/conformance` exists to prove.
 */

export type MailView =
  | "inbox"
  | "thread"
  | "compose"
  | "search"
  | "sent"
  | "drafts"
  | "calendar"
  | "signed_out";

/** One candidate element, straight from the DOM/accessibility snapshot. */
export interface RawElement {
  nodeId: number;
  role: string;
  name: string;
  value: string | null;

  /** Viewport-relative. Stays extension-side forever. */
  x: number;
  y: number;
  width: number;
  height: number;

  /** Can a user act on this? Non-interactive elements survive only if they carry text. */
  interactive: boolean;
  /** Does computed style render it at all? (`display:none`, `visibility:hidden`, opacity) */
  displayed: boolean;
  /** Higher paints later, i.e. on top. Used only by the geometric occlusion fallback. */
  paintOrder: number;
  /**
   * The browser's own verdict on "would a click here reach this element?" —
   * `true` (yes), `false` (something else is on top), `null` (could not be tested).
   * Authoritative when present: geometry only ever approximates it.
   */
  receivesPointer: boolean | null;

  parentId: number | null;
  depth: number;

  /** Assigned by the SoM indexer; `null` until then. */
  index: number | null;
}

/** Everything about the page that is not an element. */
export interface PageMeta {
  /**
   * A RAW identifier (the URL). Tokenized before it reaches an `Observation`, which is why
   * the contract carries `contextId` and no `url` — on an email surface a URL is an
   * identifier.
   */
  contextRef: string;
  title: string;
  viewportWidth: number;
  viewportHeight: number;
  scrollX: number;
  scrollY: number;

  view: MailView;
  threadRef: string | null;
  unreadCount: number | null;
  composeOpen: boolean;
  /**
   * Which compose fields already hold content. Booleans only — the content itself is
   * precisely what must not reach the model in the clear.
   *
   * A committed recipient becomes a chip, a separate node, so the input reads empty and the
   * agent types the address a second time on top of the first.
   */
  toFilled: boolean;
  subjectFilled: boolean;
  bodyFilled: boolean;
  /**
   * The open dialog's box, when there is one. Viewport-relative.
   *
   * Not for clicking — for PRIORITY. When a compose window is open its fields are the only
   * things the agent can act on, and without this they compete for the token budget against
   * every inbox row behind them and lose. Observed live: the subject field was trimmed away
   * and the agent spent five turns scrolling a page that never moves, looking for it.
   */
  focusBox: Box | null;
}

/** `[x, y, width, height]`, viewport-relative. */
export type Box = readonly [number, number, number, number];

/**
 * What each stage removed.
 *
 * Kept because the alternative is an agent that cannot tell "there is nothing else here"
 * from "I hid the rest from you". Silent truncation makes an agent confidently wrong, so
 * these counts are carried out of the funnel and surfaced, not logged and forgotten.
 */
export interface FunnelReport {
  extracted: number;
  /** Not rendered at all. */
  hidden: number;
  /** Scrolled past; reachable by scrolling UP. */
  offscreenAbove: number;
  /** Not yet reached; reachable by scrolling DOWN. */
  offscreenBelow: number;
  /** Covered by something on top. */
  occluded: number;
  /** Layout wrappers folded into their meaningful child. */
  collapsed: number;
  /** Cut to fit the token budget. */
  budgetDropped: number;
  shown: number;
  stages: string[];
}

export function emptyReport(): FunnelReport {
  return {
    extracted: 0,
    hidden: 0,
    offscreenAbove: 0,
    offscreenBelow: 0,
    occluded: 0,
    collapsed: 0,
    budgetDropped: 0,
    shown: 0,
    stages: [],
  };
}

/**
 * What the agent could still reach that is not in the list.
 *
 * Off-screen plus budget-dropped. Hidden, occluded, and collapsed elements are deliberately
 * NOT counted: they are not actionable, so reporting them would teach the agent to scroll
 * after content that does not exist.
 */
export function reachableButUnlisted(report: FunnelReport): number {
  return report.offscreenAbove + report.offscreenBelow + report.budgetDropped;
}

export function offscreen(report: FunnelReport): number {
  return report.offscreenAbove + report.offscreenBelow;
}

// ── geometry helpers ────────────────────────────────────────────────────────

export const area = (e: RawElement): number => e.width * e.height;
export const right = (e: RawElement): number => e.x + e.width;
export const bottom = (e: RawElement): number => e.y + e.height;

export function hasText(e: RawElement): boolean {
  return Boolean(e.name.trim() || (e.value ?? "").trim());
}

/** Fraction of `self`'s area covered by `other`. 0 when disjoint. */
export function overlaps(self: RawElement, other: RawElement): number {
  const selfArea = area(self);
  if (selfArea <= 0) return 0;
  const overlapW = Math.min(right(self), right(other)) - Math.max(self.x, other.x);
  const overlapH = Math.min(bottom(self), bottom(other)) - Math.max(self.y, other.y);
  if (overlapW <= 0 || overlapH <= 0) return 0;
  return (overlapW * overlapH) / selfArea;
}

/** A `RawElement` with sensible defaults, so callers state only what matters. */
export function rawElement(partial: Partial<RawElement> & { nodeId: number }): RawElement {
  return {
    role: "generic",
    name: "",
    value: null,
    x: 0,
    y: 0,
    width: 0,
    height: 0,
    interactive: false,
    displayed: true,
    paintOrder: 0,
    receivesPointer: null,
    parentId: null,
    depth: 0,
    index: null,
    ...partial,
  };
}
