/**
 * The bridge wire protocol — how the backend drives this extension.
 *
 * Deliberately a plain request/response RPC over one socket, not a second event stream. The
 * backend already has an event channel to the cockpit; adding a second one here would mean
 * two orderings to reason about and two places for a frame to go missing.
 *
 * **The backend is the caller, always.** It asks for an observation or an action; the
 * extension answers. The extension never pushes work of its own, which means there is no
 * path by which a compromised backend can make this extension do something the running graph
 * did not ask for — the surface area is exactly these four methods.
 *
 * Payloads carry only what the backend is allowed to know: tokenized observations, typed
 * results, and an approval fingerprint. No coordinates, no addresses, no page text.
 */
import type { ActionCall, ActionResult, Observation } from "@inbox/contracts";

/** Bumped when the shape below changes incompatibly. Checked at `hello`. */
export const BRIDGE_PROTOCOL_VERSION = "1";

// ── extension -> backend ────────────────────────────────────────────────────

/**
 * First frame on every connection. Until the backend accepts it, nothing else is served.
 *
 * The pairing code is what binds this browser to one user's runs. Without it any process
 * that can reach the socket could ask this extension to read the mailbox.
 */
export interface HelloFrame {
  type: "hello";
  protocolVersion: string;
  /**
   * The durable credential, once this browser has paired. Preferred over the code: it is
   * what makes a reconnect silent, and MV3 reconnects constantly.
   */
  bridgeToken?: string;
  /** The single-use hand-off from a signed-in cockpit. Only on the very first connection. */
  pairingCode?: string;
  extensionVersion: string;
}

export interface ResultFrame {
  type: "result";
  id: string;
  ok: true;
  result: unknown;
}

export interface ErrorFrame {
  type: "error";
  id: string;
  ok: false;
  error: { message: string; code: string | null };
}

/** Unsolicited, and the only such frame: the tab went away mid-run. */
export interface DetachedFrame {
  type: "detached";
  reason: string;
}

export type ExtensionFrame = HelloFrame | ResultFrame | ErrorFrame | DetachedFrame;

// ── backend -> extension ────────────────────────────────────────────────────

export interface ObserveRequest {
  type: "call";
  id: string;
  method: "observe";
  params?: Record<string, never>;
}

export interface ActRequest {
  type: "call";
  id: string;
  method: "act";
  params: { call: ActionCall };
}

export interface PreviewRequest {
  type: "call";
  id: string;
  method: "preview";
  params: { call: ActionCall };
}

export interface FingerprintRequest {
  type: "call";
  id: string;
  method: "fingerprint";
  params: { call: ActionCall };
}

export interface ApproveRequest {
  type: "call";
  id: string;
  method: "approve";
  params: { fingerprint: string };
}

export interface StartRequest {
  type: "call";
  id: string;
  method: "start";
  params: { boundVerbs: string[]; tokenBudget?: number };
}

export interface StopRequest {
  type: "call";
  id: string;
  method: "stop";
  params?: Record<string, never>;
}

export interface WelcomeFrame {
  type: "welcome";
  sessionId: string;
  /**
   * Re-issued on EVERY connect, not only on first pairing, so a daily user's pairing rolls
   * forward instead of expiring underneath them. Store whatever arrives.
   */
  bridgeToken?: string;
  /** Which account the backend thinks this browser belongs to. For the popup to show. */
  account?: string;
}

export type BackendFrame =
  | ObserveRequest
  | ActRequest
  | PreviewRequest
  | FingerprintRequest
  | ApproveRequest
  | StartRequest
  | StopRequest
  | WelcomeFrame;

export type BridgeMethod = Extract<BackendFrame, { type: "call" }>["method"];

/**
 * Narrow an incoming frame.
 *
 * The backend is authenticated but still parsed defensively: a frame that does not match is
 * ignored rather than trusted, because "we authenticated the peer" and "the peer sent
 * something sensible" are different claims.
 */
export function parseBackendFrame(data: unknown): BackendFrame | null {
  if (!data || typeof data !== "object") return null;
  const frame = data as Record<string, unknown>;

  if (frame.type === "welcome" && typeof frame.sessionId === "string") {
    return {
      type: "welcome",
      sessionId: frame.sessionId,
      ...(typeof frame.bridgeToken === "string" ? { bridgeToken: frame.bridgeToken } : {}),
      ...(typeof frame.account === "string" ? { account: frame.account } : {}),
    };
  }
  if (frame.type !== "call") return null;
  if (typeof frame.id !== "string" || typeof frame.method !== "string") return null;

  const methods: BridgeMethod[] = [
    "observe",
    "act",
    "preview",
    "fingerprint",
    "approve",
    "start",
    "stop",
  ];
  if (!methods.includes(frame.method as BridgeMethod)) return null;

  return frame as unknown as BackendFrame;
}

export type { ActionCall, ActionResult, Observation };
