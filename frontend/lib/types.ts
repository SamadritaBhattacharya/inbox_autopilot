/**
 * The cockpit's view of the wire protocol.
 *
 * Hand-written rather than generated, unlike `Observation` and `ActionCall`: events are an
 * open, additive vocabulary. A cockpit that receives an unknown event must ignore it, not
 * fail to parse the frame — so the type is deliberately permissive at the boundary and
 * narrowed only where a component actually reads a field.
 */

export type AgentEvent = {
  event: string;
  data: Record<string, unknown>;
  ts: string;
};

export type RunStatus = "idle" | "starting" | "running" | "awaiting" | "done" | "failed";

/** One rendered row in the transcript. Derived from events, never stored on the wire. */
export type Entry =
  | { kind: "task"; text: string }
  | { kind: "intent"; action: string; slots: Record<string, string>; confidence: number }
  | { kind: "route"; route: string; why: string; ruleMatched: boolean }
  | { kind: "plan"; steps: string[] }
  | { kind: "status"; phase: string; message: string }
  | { kind: "assessment"; text: string; outcome: string }
  | { kind: "reasoning"; text: string }
  | { kind: "action"; name: string; args: Record<string, unknown> }
  | { kind: "result"; success: boolean; reason: string; errorCode: string | null }
  | { kind: "observation"; elements: number; dropped: number; view: string }
  | { kind: "feedback"; text: string }
  | { kind: "error"; message: string; errorCode: string | null }
  | { kind: "finalize"; success: boolean; reason: string; errorCode: string | null };

export type PendingQuestion = {
  requestId: string;
  question: string;
  missing: string[];
};

export type UsageTotals = {
  calls: number;
  inputTokens: number;
  outputTokens: number;
  cachedTokens: number;
};

export const str = (value: unknown, fallback = ""): string =>
  typeof value === "string" ? value : fallback;

export const num = (value: unknown, fallback = 0): number =>
  typeof value === "number" ? value : fallback;

export const bool = (value: unknown, fallback = false): boolean =>
  typeof value === "boolean" ? value : fallback;
