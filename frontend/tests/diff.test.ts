/**
 * The draft diff shown on the approval card.
 *
 * Pure, so it is tested as arithmetic rather than through a rendered component. What
 * matters is that it never LIES: a line reported unchanged has to actually be unchanged,
 * because the whole point is letting somebody skip re-reading the rest.
 */
import { describe, expect, it } from "vitest";

import { changeSummary, diffLines, toHunks } from "../lib/diff";

const BEFORE = ["To:      Priya <p@x.com>", "Subject: Friday demo", "", "It moved to 4pm."].join(
  "\n",
);

describe("diffLines", () => {
  it("shows only the line that changed", () => {
    const rows = diffLines(BEFORE, BEFORE.replace("4pm", "5pm"));

    expect(rows).not.toBeNull();
    expect(rows!.filter((row) => row.kind !== "same")).toEqual([
      { kind: "remove", text: "It moved to 4pm." },
      { kind: "add", text: "It moved to 5pm." },
    ]);
  });

  it("keeps every unchanged line intact", () => {
    const rows = diffLines(BEFORE, BEFORE.replace("4pm", "5pm"))!;
    const same = rows.filter((row) => row.kind === "same").map((row) => row.text);

    expect(same).toEqual(["To:      Priya <p@x.com>", "Subject: Friday demo", ""]);
  });

  it("reports a changed recipient — the line that matters most", () => {
    const rows = diffLines(BEFORE, BEFORE.replace("Priya <p@x.com>", "alex@corp.com"))!;

    expect(rows.some((row) => row.kind === "add" && row.text.includes("alex@corp.com"))).toBe(true);
    expect(rows.some((row) => row.kind === "remove" && row.text.includes("p@x.com"))).toBe(true);
  });

  it("returns null when nothing changed, so no strip is rendered", () => {
    expect(diffLines(BEFORE, BEFORE)).toBeNull();
  });

  it("returns null on the FIRST ask, where there is no previous version", () => {
    expect(diffLines("", BEFORE)).toBeNull();
  });

  it("treats CRLF as the same text — a line ending is not an edit", () => {
    expect(diffLines(BEFORE, BEFORE.replace(/\n/g, "\r\n"))).toBeNull();
  });

  it("handles a pure addition", () => {
    const rows = diffLines(BEFORE, `${BEFORE}\nBest,\nSam`)!;

    expect(changeSummary(rows)).toEqual({ added: 2, removed: 0 });
  });

  it("handles a pure deletion", () => {
    const rows = diffLines(`${BEFORE}\nBest,\nSam`, BEFORE)!;

    expect(changeSummary(rows)).toEqual({ added: 0, removed: 2 });
  });

  it("handles a body replaced outright", () => {
    const rows = diffLines(BEFORE, "To:      Priya <p@x.com>\nSubject: New\n\nEntirely different.")!;

    expect(rows.filter((row) => row.kind === "same").length).toBeGreaterThan(0);
    expect(changeSummary(rows).added).toBeGreaterThan(0);
  });

  it("refuses a pair too large to diff rather than blocking the card", () => {
    const huge = Array.from({ length: 500 }, (_, n) => `line ${n}`).join("\n");

    expect(diffLines(huge, `${huge}\nextra`)).toBeNull();
  });

  it("survives blank input on either side", () => {
    expect(diffLines("", "")).toBeNull();
    expect(diffLines(BEFORE, "")).toBeNull();
  });

  it("never drops or invents content", () => {
    const after = "To:      Priya <p@x.com>\nSubject: Friday demo\n\nMoved to 5pm.\nBest, Sam";
    const rows = diffLines(BEFORE, after)!;

    const reconstructedBefore = rows
      .filter((row) => row.kind !== "add")
      .map((row) => row.text)
      .join("\n");
    const reconstructedAfter = rows
      .filter((row) => row.kind !== "remove")
      .map((row) => row.text)
      .join("\n");

    expect(reconstructedBefore).toBe(BEFORE);
    expect(reconstructedAfter).toBe(after);
  });
});

describe("toHunks", () => {
  it("elides long unchanged stretches down to a gap marker", () => {
    const before = Array.from({ length: 20 }, (_, n) => `line ${n}`).join("\n");
    const after = before.replace("line 10", "line TEN");

    const hunks = toHunks(diffLines(before, after)!);

    expect(hunks).toContain(null);
    expect(hunks.length).toBeLessThan(12);
    expect(hunks.some((row) => row?.kind === "add" && row.text === "line TEN")).toBe(true);
  });

  it("keeps a line of context either side of every change", () => {
    const before = "a\nb\nc\nd\ne";
    const hunks = toHunks(diffLines(before, "a\nb\nC\nd\ne")!);
    const texts = hunks.map((row) => row?.text ?? "…");

    expect(texts).toContain("b");
    expect(texts).toContain("d");
  });

  it("collapses consecutive elisions into ONE marker", () => {
    const before = Array.from({ length: 30 }, (_, n) => `line ${n}`).join("\n");
    const after = before.replace("line 0", "CHANGED");

    const hunks = toHunks(diffLines(before, after)!);

    expect(hunks.filter((row) => row === null).length).toBe(1);
  });

  it("leaves a small diff untouched", () => {
    const hunks = toHunks(diffLines("a\nb", "a\nB")!);

    expect(hunks.every((row) => row !== null)).toBe(true);
  });
});
