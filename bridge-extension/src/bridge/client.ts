/**
 * The socket to the backend, with the reconnection an MV3 service worker actually needs.
 *
 * **The MV3 trap, stated once.** Chrome terminates an idle service worker after ~30 seconds,
 * and it takes the WebSocket with it. An extension that opens a socket and assumes it stays
 * open works perfectly for half a minute and then dies silently — no error, no close event
 * the page ever sees, just a bridge that stopped answering. Two things prevent that here: an
 * activity alarm that keeps the worker alive while a session is running, and reconnection
 * that treats a drop as routine rather than exceptional.
 *
 * Reconnect backoff is capped and jittered. A backend restart otherwise means every
 * connected browser retries in lockstep on the same second.
 */
import {
  BRIDGE_PROTOCOL_VERSION,
  parseBackendFrame,
  type BackendFrame,
  type ExtensionFrame,
} from "./protocol";

const MIN_BACKOFF_MS = 500;
const MAX_BACKOFF_MS = 30_000;

export interface BridgeClientOptions {
  url: string;
  /** The durable credential, if this browser has paired before. */
  bridgeToken: string;
  /** The one-time code, used only until a bridge token comes back. */
  pairingCode: string;
  extensionVersion: string;
  onFrame: (frame: BackendFrame) => void | Promise<void>;
  onStatus?: (status: BridgeStatus) => void;
}

export type BridgeStatus =
  | { state: "connecting" }
  | { state: "connected" }
  | { state: "rejected"; reason: string }
  | { state: "offline"; retryInMs: number };

export class BridgeClient {
  private socket: WebSocket | null = null;
  private attempt = 0;
  private closedByUs = false;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly options: BridgeClientOptions) {}

  get connected(): boolean {
    return this.socket?.readyState === WebSocket.OPEN;
  }

  connect(): void {
    this.closedByUs = false;
    this.clearRetry();
    this.options.onStatus?.({ state: "connecting" });

    let socket: WebSocket;
    try {
      socket = new WebSocket(this.options.url);
    } catch (error) {
      // A malformed URL throws synchronously. Treat it as a normal failure so the popup
      // shows "offline" rather than the worker dying on startup.
      this.scheduleRetry(error instanceof Error ? error.message : String(error));
      return;
    }
    this.socket = socket;

    socket.addEventListener("open", () => {
      this.attempt = 0;
      // Identify before anything else. Until the backend accepts this, we serve nothing.
      //
      // The token wins when we have one: a pairing code is single-use, so presenting it on
      // a reconnect would burn it and leave the extension unable to come back.
      this.send({
        type: "hello",
        protocolVersion: BRIDGE_PROTOCOL_VERSION,
        ...(this.options.bridgeToken
          ? { bridgeToken: this.options.bridgeToken }
          : { pairingCode: this.options.pairingCode }),
        extensionVersion: this.options.extensionVersion,
      });
      this.options.onStatus?.({ state: "connected" });
    });

    socket.addEventListener("message", (event) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(String(event.data));
      } catch {
        return; // Unparseable frames are ignored, never fatal.
      }
      const frame = parseBackendFrame(parsed);
      if (frame) void this.options.onFrame(frame);
    });

    socket.addEventListener("close", (event) => {
      this.socket = null;
      if (this.closedByUs) return;
      // 4401/4403 are ours: the pairing code was refused. Retrying with the same bad code
      // forever would hammer the backend and never succeed, so stop and say so.
      if (event.code === 4401 || event.code === 4403) {
        this.options.onStatus?.({
          state: "rejected",
          reason: event.reason || "this browser is not paired with that account",
        });
        return;
      }
      this.scheduleRetry(event.reason || `socket closed (${event.code})`);
    });

    socket.addEventListener("error", () => {
      // `close` always follows, and carries the useful information. Handling both would
      // double every retry.
    });
  }

  send(frame: ExtensionFrame): void {
    if (this.socket?.readyState !== WebSocket.OPEN) return;
    this.socket.send(JSON.stringify(frame));
  }

  disconnect(): void {
    this.closedByUs = true;
    this.clearRetry();
    this.socket?.close(1000, "bridge stopped");
    this.socket = null;
  }

  private scheduleRetry(reason: string): void {
    // Exponential with full jitter: without it every browser reconnects on the same second
    // after a backend restart.
    const ceiling = Math.min(MAX_BACKOFF_MS, MIN_BACKOFF_MS * 2 ** this.attempt);
    const delay = Math.round(Math.random() * ceiling) + MIN_BACKOFF_MS;
    this.attempt += 1;

    this.options.onStatus?.({ state: "offline", retryInMs: delay });
    console.info(`[bridge] ${reason}; retrying in ${delay}ms`);
    this.retryTimer = setTimeout(() => this.connect(), delay);
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
  }
}
