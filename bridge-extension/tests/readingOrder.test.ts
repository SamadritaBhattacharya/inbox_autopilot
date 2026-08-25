/**
 * `ReadingOrderFormatter`, on its own — mirroring
 * `backend/tests/observation/test_stages.py` and the focus-box tests in
 * `backend/tests/observation/test_conformance.py`.
 *
 * No dedicated file for this stage existed on this side before; it was exercised only
 * through `pipeline.test.ts` and the shared conformance fixtures, neither of which used
 * enough elements to trigger a real budget cut. These are the TS side of the B3 fix — see
 * `docs/IMPROVEMENT-PLAN.md`.
 */
import { describe, expect, it } from "vitest";

import { rawElement } from "@/funnel/raw";
import { ReadingOrderFormatter } from "@/funnel/readingOrder";
import { SoMIndexer } from "@/funnel/som";

const VIEWPORT_H = 800;

const COMPOSE_FOCUS = [810, 480, 340, 300] as const;

function inboxAndCompose(count = 140) {
  const rows = Array.from({ length: count }, (_, i) =>
    rawElement({
      nodeId: i + 1,
      role: "listitem",
      name: `Person ${i} — Re: a fairly typical subject line about something ${i}`,
      y: 100 + i * 12,
      interactive: true,
    }),
  );
  const fields = (
    [
      ["textbox", "To", 500],
      ["textbox", "Subject", 540],
      ["textbox", "Message Body", 580],
      ["button", "Send", 720],
    ] as const
  ).map(([role, name, y], n) =>
    rawElement({ nodeId: 900 + n, role, name, x: 820, y, interactive: true }),
  );
  return new SoMIndexer().apply([...rows, ...fields]).indexed;
}

describe("region-of-interest scoping (B3)", () => {
  it("hard-caps what's outside the focus box, even at the default budget", () => {
    // The actual regression: priority alone left ~99 of 140 background rows visible at the
    // default budget, because the dialog's own fields cost so little that most of the
    // budget was never spent.
    const { elements, budgetDropped } = new ReadingOrderFormatter().apply(inboxAndCompose(), {
      viewportHeight: VIEWPORT_H,
      focusBox: COMPOSE_FOCUS,
    });

    const composeNames = new Set(["To", "Subject", "Message Body", "Send"]);
    const survived = elements.filter((e) => !composeNames.has(e.name));

    for (const name of composeNames) {
      expect(elements.some((e) => e.name === name)).toBe(true);
    }
    expect(survived.length).toBeLessThan(20);
    expect(budgetDropped).toBeGreaterThan(100);
  });

  it("leaves the default budget unaffected with no focus box open", () => {
    const withoutDialog = inboxAndCompose().filter(
      (e) => !["To", "Subject", "Message Body", "Send"].includes(e.name),
    );

    const { elements } = new ReadingOrderFormatter().apply(withoutDialog, {
      viewportHeight: VIEWPORT_H,
      focusBox: null,
    });

    expect(elements.length).toBeGreaterThan(100);
  });

  it("never caps elements INSIDE the box, however many there are", () => {
    const manyFields = Array.from({ length: 30 }, (_, n) =>
      rawElement({
        nodeId: 900 + n,
        role: "textbox",
        name: `field ${n}`,
        x: 820,
        y: 480 + n * 20,
        interactive: true,
      }),
    );
    const indexed = new SoMIndexer().apply(manyFields).indexed;

    const { elements, budgetDropped } = new ReadingOrderFormatter().apply(indexed, {
      viewportHeight: VIEWPORT_H,
      focusBox: COMPOSE_FOCUS,
    });

    expect(budgetDropped).toBe(0);
    expect(elements).toHaveLength(30);
  });

  it("still keeps every compose field at a tight budget — the pre-existing guarantee", () => {
    const rows = Array.from({ length: 140 }, (_, i) =>
      rawElement({
        nodeId: i + 1,
        role: "listitem",
        name: `Inbox row ${i} with a fairly long subject line to eat the budget`,
        y: 100 + i * 12,
        interactive: true,
      }),
    );
    const fields = (
      [
        ["textbox", "To", 500],
        ["textbox", "Subject", 540],
        ["textbox", "Message Body", 580],
        ["button", "Send", 720],
      ] as const
    ).map(([role, name, y], n) =>
      rawElement({ nodeId: 900 + n, role, name, x: 820, y, interactive: true }),
    );
    const indexed = new SoMIndexer().apply([...rows, ...fields]).indexed;

    const { elements } = new ReadingOrderFormatter({ tokenBudget: 300 }).apply(indexed, {
      viewportHeight: VIEWPORT_H,
      focusBox: COMPOSE_FOCUS,
    });
    const names = new Set(elements.map((e) => e.name));

    expect(names.has("To")).toBe(true);
    expect(names.has("Subject")).toBe(true);
    expect(names.has("Message Body")).toBe(true);
    expect(names.has("Send")).toBe(true);
  });

  it("the counterfactual: without a focus box, the rows still win on volume", () => {
    const rows = Array.from({ length: 140 }, (_, i) =>
      rawElement({
        nodeId: i + 1,
        role: "listitem",
        name: `Inbox row ${i} with a long subject`,
        y: 100 + i * 12,
        interactive: true,
      }),
    );
    const subject = rawElement({
      nodeId: 901,
      role: "textbox",
      name: "Subject",
      x: 820,
      y: 540,
      interactive: true,
    });
    const indexed = new SoMIndexer().apply([...rows, subject]).indexed;

    const { elements } = new ReadingOrderFormatter({ tokenBudget: 300 }).apply(indexed, {
      viewportHeight: VIEWPORT_H,
      focusBox: null,
    });

    expect(elements.some((e) => e.name === "Subject")).toBe(false);
  });
});
