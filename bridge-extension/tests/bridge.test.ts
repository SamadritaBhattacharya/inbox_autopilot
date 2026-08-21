/**
 * The bridge edges: what the backend may ask for, what the page may return, and how a key
 * is turned into an event.
 *
 * The protocol test is the security-relevant one. The backend is authenticated, but "we
 * authenticated the peer" and "the peer sent something sensible" are different claims, and
 * only the second one keeps a malformed or malicious frame from reaching `chrome.debugger`.
 */
import { describe, expect, it } from "vitest";

import { parseBackendFrame } from "@/bridge/protocol";
import { describeStatus } from "@/ui/status";
import { detectView, parseElements, parseMeta } from "@/page/extract";
import { parseKey, timeoutFor, TYPE_KEYSTROKE_LIMIT } from "@/driver/actions";
import type { ActionCall } from "@inbox/contracts";

const call = (name: string, args: Record<string, unknown> = {}): ActionCall =>
  ({ name, args }) as ActionCall;

describe("protocol", () => {
  it("accepts the four port methods plus lifecycle", () => {
    for (const method of ["observe", "act", "preview", "fingerprint", "approve", "start", "stop"]) {
      expect(parseBackendFrame({ type: "call", id: "1", method })).not.toBeNull();
    }
  });

  it("refuses a method that is not on the port", () => {
    // There is no "evaluate this for me" escape hatch, and this is what keeps one from
    // being added by accident: a compromised backend can call exactly these and no more.
    for (const method of ["evaluate", "eval", "screenshot", "navigate", "__proto__"]) {
      expect(parseBackendFrame({ type: "call", id: "1", method })).toBeNull();
    }
  });

  it("refuses malformed frames rather than trusting them", () => {
    for (const frame of [null, undefined, 42, "call", {}, { type: "call" }, { type: "call", id: 1 }]) {
      expect(parseBackendFrame(frame)).toBeNull();
    }
  });

  it("accepts the welcome frame", () => {
    expect(parseBackendFrame({ type: "welcome", sessionId: "s1" })).toEqual({
      type: "welcome",
      sessionId: "s1",
    });
  });
});

describe("view detection", () => {
  it("names a sign-in wall rather than calling it an inbox", () => {
    // Every other branch falls through to "inbox", so a login page was reported as a
    // mailbox — and the agent confidently summarized a mailbox it had never seen.
    expect(detectView("https://accounts.google.com/v3/signin/identifier", false)).toBe("signed_out");
    expect(detectView("https://mail.google.com/ServiceLogin", false)).toBe("signed_out");
  });

  it("prefers compose over everything", () => {
    expect(detectView("https://mail.google.com/mail/u/0/#inbox", true)).toBe("compose");
  });

  it("recognises the labelled views", () => {
    expect(detectView("https://mail.google.com/mail/u/0/#sent", false)).toBe("sent");
    expect(detectView("https://mail.google.com/mail/u/0/#drafts", false)).toBe("drafts");
    expect(detectView("https://mail.google.com/mail/u/0/#search/q", false)).toBe("search");
  });

  it("recognises a thread by its message id", () => {
    expect(detectView("https://mail.google.com/mail/u/0/#inbox/FMfcgzQbdRxKlZ", false)).toBe("thread");
  });

  it("does not mistake a path-shaped URL for a thread", () => {
    // A plain `file:///…/inbox.html` was classified as "thread" because the last path
    // segment happened to be long enough. Gmail puts the id in the FRAGMENT.
    expect(detectView("file:///C:/fixtures/a-very-long-filename.html", false)).toBe("inbox");
    expect(detectView("https://mail.google.com/mail/u/0/", false)).toBe("inbox");
  });
});

describe("parsing what the page returned", () => {
  it("survives a page that returns nonsense", () => {
    // This data comes from a page we do not control. A hostile or merely broken one must
    // produce boring elements, not an exception that kills the turn.
    const parsed = parseElements([
      { nodeId: 1, x: "banana", width: null, role: 42 },
      null,
      "not an object",
      { role: "button" }, // no nodeId
    ]);

    expect(parsed).toHaveLength(1);
    expect(parsed[0]!.x).toBe(0);
    expect(parsed[0]!.role).toBe("generic");
  });

  it("keeps a real element intact", () => {
    const parsed = parseElements([
      {
        nodeId: 7,
        role: "button",
        name: "Send",
        value: null,
        x: 10.5,
        y: 20.5,
        width: 80,
        height: 30,
        interactive: true,
        displayed: true,
        paintOrder: 3,
        receivesPointer: true,
        parentId: 2,
      },
    ]);

    expect(parsed[0]).toMatchObject({ nodeId: 7, name: "Send", interactive: true, parentId: 2 });
  });

  it("defaults a viewport rather than producing NaN geometry", () => {
    const meta = parseMeta({ contextRef: "https://mail.google.com/#inbox" });

    expect(meta.viewportWidth).toBe(1280);
    expect(meta.viewportHeight).toBe(800);
  });
});

describe("typing walls", () => {
  it("gives a short field the plain wall", () => {
    expect(timeoutFor(call("Type", { text: "hi" }))).toBe(10_000);
  });

  it("scales the wall with a long body", () => {
    // A fixed timeout on an action whose duration is LINEAR in its payload is the wrong
    // model: it fails on exactly the long bodies the writer exists to produce.
    const long = "x".repeat(TYPE_KEYSTROKE_LIMIT + 200);

    expect(timeoutFor(call("Type", { text: long }))).toBeGreaterThan(10_000);
  });

  it("caps the wall so a pathological body cannot hang the run", () => {
    expect(timeoutFor(call("Type", { text: "x".repeat(500_000) }))).toBe(60_000);
  });

  it("scales for a resolved recipient too", () => {
    const long = "P1, ".repeat(40);
    expect(timeoutFor(call("Type", { recipient: long }))).toBeGreaterThan(10_000);
  });

  it("leaves other verbs alone", () => {
    expect(timeoutFor(call("Click", { index: 3 }))).toBe(10_000);
    expect(timeoutFor(call("Navigate", { url: "x" }))).toBe(30_000);
  });
});

describe("keys", () => {
  it("splits modifiers into CDP's bitmask", () => {
    expect(parseKey("Control+Enter")).toEqual({ base: "Enter", modifiers: 2 });
    expect(parseKey("Shift+Tab")).toEqual({ base: "Tab", modifiers: 8 });
    expect(parseKey("Enter")).toEqual({ base: "Enter", modifiers: 0 });
  });

  it("accepts the aliases people actually type", () => {
    expect(parseKey("ctrl+Enter").modifiers).toBe(2);
    expect(parseKey("cmd+Enter").modifiers).toBe(4);
  });
});

describe("status text", () => {
  it("distinguishes waiting from needing the user", () => {
    // The distinction users get wrong: one sorts itself out, the other never will.
    expect(describeStatus({ state: "offline", retryInMs: 4000 }, false)).toContain("retrying");
    expect(describeStatus({ state: "rejected", reason: "not paired" }, false)).toBe("not paired");
  });

  it("says whether a run is in progress", () => {
    expect(describeStatus({ state: "connected" }, true)).toContain("run is in progress");
    expect(describeStatus({ state: "connected" }, false)).toContain("idle");
  });
});
