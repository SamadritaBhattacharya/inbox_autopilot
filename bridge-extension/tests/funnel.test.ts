/**
 * The funnel stages, ported from Python.
 *
 * These mirror `backend/tests/observation/` deliberately: the extension and the server-side
 * surface must prune identically, or the same mailbox produces two different observations
 * and a bug reproduces on one surface but not the other. Where a case here has a Python
 * twin, the twin's intent is restated rather than paraphrased.
 */
import { describe, expect, it } from "vitest";

import { OcclusionCuller } from "@/funnel/occlusion";
import { hasText, overlaps, rawElement, type PageMeta } from "@/funnel/raw";
import { SoMIndexer } from "@/funnel/som";
import { VisibilityFilter } from "@/funnel/visibility";
import { WrapperCollapser } from "@/funnel/wrapperCollapse";

const meta = (overrides: Partial<PageMeta> = {}): PageMeta => ({
  contextRef: "https://mail.google.com/mail/u/0/#inbox",
  title: "Inbox",
  viewportWidth: 1280,
  viewportHeight: 800,
  scrollX: 0,
  scrollY: 0,
  view: "inbox",
  threadRef: null,
  unreadCount: null,
  composeOpen: false,
  ...overrides,
});

const box = (nodeId: number, x: number, y: number, w = 100, h = 20, extra = {}) =>
  rawElement({ nodeId, x, y, width: w, height: h, ...extra });

describe("VisibilityFilter", () => {
  it("keeps what is on screen", () => {
    const result = new VisibilityFilter().apply([box(1, 10, 10)], meta());
    expect(result.kept).toHaveLength(1);
  });

  it("drops what computed style never rendered", () => {
    const result = new VisibilityFilter().apply(
      [box(1, 10, 10, 100, 20, { displayed: false })],
      meta(),
    );
    expect(result.kept).toHaveLength(0);
    expect(result.hidden).toBe(1);
  });

  it("drops tracking pixels and collapsed containers", () => {
    const result = new VisibilityFilter().apply([box(1, 10, 10, 1, 1)], meta());
    expect(result.hidden).toBe(1);
  });

  it("counts above and below separately", () => {
    // "There is more" without "which way" is not actionable: the agent scrolls one
    // direction, sees the count unchanged, and scrolls the same way again.
    const result = new VisibilityFilter().apply(
      [box(1, 10, -500), box(2, 10, 5000), box(3, 10, 5001)],
      meta(),
    );
    expect(result.offscreenAbove).toBe(1);
    expect(result.offscreenBelow).toBe(2);
  });

  it("keeps a row peeking in by a pixel", () => {
    // Dropping these makes the list flicker between turns as the page settles.
    const result = new VisibilityFilter().apply([box(1, 10, -2)], meta());
    expect(result.kept).toHaveLength(1);
  });
});

describe("OcclusionCuller", () => {
  const viewportArea = 1280 * 800;

  it("believes the browser's hit-test over geometry", () => {
    // The whole point of the stage: a transparent scrim and a real cover are geometrically
    // identical, and only the browser can tell them apart.
    const elements = [
      box(1, 0, 0, 100, 100, { receivesPointer: false }),
      box(2, 0, 0, 100, 100, { receivesPointer: true }),
    ];

    const result = new OcclusionCuller().apply(elements, viewportArea);

    expect(result.occluded).toBe(1);
    expect(result.kept.map((e) => e.nodeId)).toEqual([2]);
  });

  it("falls back to geometry only when the hit-test is unavailable", () => {
    const covered = box(1, 0, 0, 100, 100, { paintOrder: 1 });
    const cover = box(2, 0, 0, 100, 100, { paintOrder: 9 });

    const result = new OcclusionCuller().apply([covered, cover], viewportArea);

    expect(result.occluded).toBe(1);
    expect(result.kept.map((e) => e.nodeId)).toEqual([2]);
  });

  it("never treats a full-page scrim as an occluder", () => {
    // Treating one as a cover would blank the entire observation — the worst possible
    // failure for this stage.
    const scrim = box(1, 0, 0, 1280, 800, { paintOrder: 9 });
    const content = box(2, 10, 10, 100, 100, { paintOrder: 1 });

    const result = new OcclusionCuller().apply([scrim, content], viewportArea);

    expect(result.kept.map((e) => e.nodeId).sort()).toEqual([1, 2]);
  });

  it("does not treat containment as occlusion", () => {
    const parent = box(1, 0, 0, 100, 100, { paintOrder: 1 });
    const child = box(2, 0, 0, 100, 100, { paintOrder: 9, parentId: 1 });

    const result = new OcclusionCuller().apply([parent, child], viewportArea);

    expect(result.occluded).toBe(0);
  });

  it("preserves the incoming order", () => {
    // Reading order is decided later, by a stage that knows about reading order.
    const elements = [box(3, 0, 300), box(1, 0, 100), box(2, 0, 200)];

    const result = new OcclusionCuller().apply(elements, viewportArea);

    expect(result.kept.map((e) => e.nodeId)).toEqual([3, 1, 2]);
  });
});

describe("WrapperCollapser", () => {
  it("folds a layout div into its only child", () => {
    const wrapper = box(1, 0, 0, 100, 100);
    const button = box(2, 0, 0, 100, 100, { interactive: true, parentId: 1, name: "Send" });

    const result = new WrapperCollapser().apply([wrapper, button]);

    expect(result.kept.map((e) => e.nodeId)).toEqual([2]);
    expect(result.collapsed).toBe(1);
  });

  it("keeps an interactive parent however plain it looks", () => {
    // The handler may be on this node and the child a span with no behaviour at all.
    const outer = box(1, 0, 0, 100, 100, { interactive: true });
    const inner = box(2, 0, 0, 100, 100, { parentId: 1 });

    const result = new WrapperCollapser().apply([outer, inner]);

    expect(result.kept.map((e) => e.nodeId)).toContain(1);
  });

  it("keeps a container that groups two children", () => {
    const group = box(1, 0, 0, 200, 100);
    const a = box(2, 0, 0, 100, 100, { parentId: 1 });
    const b = box(3, 100, 0, 100, 100, { parentId: 1 });

    const result = new WrapperCollapser().apply([group, a, b]);

    expect(result.kept.map((e) => e.nodeId)).toContain(1);
  });

  it("keeps a parent noticeably bigger than its child", () => {
    const row = box(1, 0, 0, 800, 100);
    const button = box(2, 0, 0, 80, 20, { interactive: true, parentId: 1 });

    const result = new WrapperCollapser().apply([row, button]);

    expect(result.kept.map((e) => e.nodeId)).toContain(1);
  });

  it("folds inert text already readable from its clickable parent", () => {
    // A mail row is ONE actionable unit; listing sender and subject separately asks the
    // model to choose between numbers that do the same thing.
    const row = box(1, 0, 0, 800, 60, { interactive: true, name: "Priya — Friday demo" });
    const sender = box(2, 10, 10, 100, 20, { parentId: 1, name: "Priya" });

    const result = new WrapperCollapser().apply([row, sender]);

    expect(result.kept.map((e) => e.nodeId)).toEqual([1]);
  });

  it("keeps inert text the parent does NOT already say", () => {
    const row = box(1, 0, 0, 800, 60, { interactive: true, name: "Priya" });
    const subject = box(2, 10, 10, 100, 20, { parentId: 1, name: "Friday demo" });

    const result = new WrapperCollapser().apply([row, subject]);

    expect(result.kept.map((e) => e.nodeId)).toEqual([1, 2]);
  });
});

describe("SoMIndexer", () => {
  it("numbers in reading order, top to bottom then left to right", () => {
    const elements = [
      box(1, 500, 100, 50, 20, { interactive: true }),
      box(2, 10, 100, 50, 20, { interactive: true }),
      box(3, 10, 300, 50, 20, { interactive: true }),
    ];

    const { indexed } = new SoMIndexer().apply(elements);

    expect(indexed.map((e) => e.nodeId)).toEqual([2, 1, 3]);
    expect(indexed.map((e) => e.index)).toEqual([1, 2, 3]);
  });

  it("treats a few pixels of baseline drift as the same row", () => {
    // Sorting strictly by y interleaves the columns of a table into nonsense.
    const elements = [
      box(1, 500, 102, 50, 20, { interactive: true }),
      box(2, 10, 100, 50, 20, { interactive: true }),
    ];

    const { indexed } = new SoMIndexer().apply(elements);

    expect(indexed.map((e) => e.nodeId)).toEqual([2, 1]);
  });

  it("indexes nothing that cannot be acted on or read", () => {
    // An index is a promise that there is something to do or read there. Handing the model
    // numbers that do nothing teaches it that numbers sometimes do nothing.
    const elements = [box(1, 0, 0, 50, 20), box(2, 0, 50, 50, 20, { interactive: true })];

    const { indexed } = new SoMIndexer().apply(elements);

    expect(indexed.map((e) => e.nodeId)).toEqual([2]);
  });

  it("maps each index to the element's centre, and keeps that map here", () => {
    const { geometry } = new SoMIndexer().apply([
      box(1, 100, 200, 40, 20, { interactive: true }),
    ]);

    expect(geometry.get(1)).toEqual({ x: 120, y: 210 });
  });

  it("does not disturb the caller's array", () => {
    const elements = [
      box(2, 10, 300, 50, 20, { interactive: true }),
      box(1, 10, 100, 50, 20, { interactive: true }),
    ];

    new SoMIndexer().apply(elements);

    expect(elements.map((e) => e.nodeId)).toEqual([2, 1]);
  });
});

describe("geometry helpers", () => {
  it("reports the fraction of an element that is covered", () => {
    const a = box(1, 0, 0, 100, 100);
    const b = box(2, 50, 0, 100, 100);

    expect(overlaps(a, b)).toBeCloseTo(0.5);
  });

  it("reports no overlap for disjoint boxes", () => {
    expect(overlaps(box(1, 0, 0, 10, 10), box(2, 500, 500, 10, 10))).toBe(0);
  });

  it("counts a value as text, so a filled field is never dropped as empty", () => {
    expect(hasText(rawElement({ nodeId: 1, value: "a@b.com" }))).toBe(true);
    expect(hasText(rawElement({ nodeId: 1 }))).toBe(false);
  });
});
