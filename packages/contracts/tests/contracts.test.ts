import { describe, expect, it } from "vitest";
import {
  actionCallSchema,
  actionResultSchema,
  envelopeSchema,
  observationSchema,
  PROTOCOL_VERSION,
} from "../src/index.js";

const validObservation = {
  contextId: "ctx-abc123",
  title: "Inbox (12)",
  viewport: { width: 1440, height: 900, scrollX: 0, scrollY: 240 },
  elements: [
    { index: 1, role: "button", name: "Compose" },
    { index: 2, role: "listitem", name: "P17 - Friday demo", isNew: true },
  ],
  mail: { view: "inbox", unreadCount: 12, composeOpen: false },
  droppedCount: 18,
};

describe("observation", () => {
  it("parses a valid payload", () => {
    const parsed = observationSchema.parse(validObservation);
    expect(parsed.contextId).toBe("ctx-abc123");
    expect(parsed.elements).toHaveLength(2);
    expect(parsed.droppedCount).toBe(18);
  });

  it("applies the same defaults the Python model has", () => {
    const parsed = observationSchema.parse({
      contextId: "c",
      viewport: { width: 1, height: 1 },
    });
    expect(parsed.protocolVersion).toBe(PROTOCOL_VERSION);
    expect(parsed.droppedCount).toBe(0);
    // default_factory=list on the Python side must not surface as `undefined` here.
    expect(parsed.elements).toEqual([]);
  });

  it("requires contextId", () => {
    expect(() => observationSchema.parse({ viewport: { width: 1, height: 1 } })).toThrow();
  });

  // ── the security invariants must survive codegen ──────────────────────────
  // These are the whole reason `extra="forbid"` exists on the Pydantic side. If the
  // generated Zod stopped being strict, the executor and cockpit would silently accept
  // coordinates or raw DOM, and the guarantee would hold on only one side of the wire.

  it.each(["x", "y", "coordinates"])("rejects geometry at the top level: %s", (field) => {
    expect(() => observationSchema.parse({ ...validObservation, [field]: 10 })).toThrow();
  });

  it.each(["x", "y", "centerX", "backendNodeId"])("rejects geometry on an element: %s", (field) => {
    expect(() =>
      observationSchema.parse({
        ...validObservation,
        elements: [{ index: 1, role: "button", name: "Compose", [field]: 42 }],
      }),
    ).toThrow();
  });

  it.each(["html", "outerHTML", "dom", "selector"])("rejects raw DOM: %s", (field) => {
    expect(() => observationSchema.parse({ ...validObservation, [field]: "<div/>" })).toThrow();
  });

  it("rejects a url (it leaks thread and message ids on an email surface)", () => {
    expect(() =>
      observationSchema.parse({ ...validObservation, url: "https://mail.google.com/#inbox/18f3a" }),
    ).toThrow();
  });

  it("rejects an unknown mail view", () => {
    expect(() =>
      observationSchema.parse({ ...validObservation, mail: { view: "spam-folder" } }),
    ).toThrow();
  });
});

describe("actionCall", () => {
  it("parses a verb with free-form args", () => {
    const parsed = actionCallSchema.parse({ name: "Type", args: { index: 14, text: "P17" } });
    expect(parsed.name).toBe("Type");
    expect(parsed.args).toEqual({ index: 14, text: "P17" });
  });

  it("defaults args to an empty object", () => {
    expect(actionCallSchema.parse({ name: "Recall" }).args).toEqual({});
  });

  it("rejects unknown top-level keys", () => {
    expect(() => actionCallSchema.parse({ name: "Click", index: 5 })).toThrow();
  });
});

describe("actionResult", () => {
  it("round-trips an error code and an undo payload", () => {
    const parsed = actionResultSchema.parse({
      success: false,
      reason: "target vanished",
      errorCode: "STALE_INDEX",
      undo: null,
    });
    expect(parsed.errorCode).toBe("STALE_INDEX");
  });

  it("requires success", () => {
    expect(() => actionResultSchema.parse({ reason: "nope" })).toThrow();
  });
});

describe("envelope", () => {
  it("carries an optional correlation id", () => {
    expect(envelopeSchema.parse({ type: "frame" }).id).toBeNull();
    expect(envelopeSchema.parse({ type: "act", id: "req-7" }).id).toBe("req-7");
  });

  it("stamps the protocol version on every frame", () => {
    expect(envelopeSchema.parse({ type: "register" }).protocolVersion).toBe(PROTOCOL_VERSION);
  });
});
