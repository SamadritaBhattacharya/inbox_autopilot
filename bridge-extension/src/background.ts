/**
 * The service worker — the extension's only long-lived component.
 *
 * It owns three things and delegates everything else: the socket to the backend, the
 * `BridgeSession` bound to one Gmail tab, and the keepalive that stops Chrome from
 * terminating the worker mid-run.
 *
 * **Why the dispatch table is so small.** Every method here maps to one of the four the
 * `EmailSurface` port defines, plus start/stop. A compromised backend can call exactly these
 * and nothing more — there is no "evaluate this for me" escape hatch, and adding one would
 * hand the backend the page it was carefully kept away from.
 */
import { BridgeClient, type BridgeStatus } from "./bridge/client";
import type { BackendFrame } from "./bridge/protocol";
import { BridgeSession } from "./session";
import { DebuggerUnavailable } from "./driver/cdp";

/** Keeps the worker alive while a run is in flight. See `KEEPALIVE_NOTE`. */
const KEEPALIVE_ALARM = "inbox-autopilot-keepalive";

/**
 * Chrome terminates an idle service worker after ~30s, taking the WebSocket with it. An
 * alarm is the supported way to be woken again; Chrome enforces a 30s minimum period, so the
 * socket can still drop between ticks — which is exactly why `BridgeClient` treats a drop as
 * routine rather than exceptional. The two together are what make a long run survive.
 */
const KEEPALIVE_PERIOD_MINUTES = 0.5;

interface Settings {
  backendUrl: string;
  pairingCode: string;
  /** The durable credential, once paired. Written from every `welcome`. */
  bridgeToken: string;
  /** Which account this browser is paired to. Display only. */
  account: string;
}

const DEFAULTS: Settings = {
  backendUrl: "ws://localhost:8000/ws/bridge",
  pairingCode: "",
  bridgeToken: "",
  account: "",
};

let client: BridgeClient | null = null;
let session: BridgeSession | null = null;
let status: BridgeStatus = { state: "offline", retryInMs: 0 };

// ── settings ────────────────────────────────────────────────────────────────

async function readSettings(): Promise<Settings> {
  const stored = await chrome.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...stored } as Settings;
}

// ── the Gmail tab ───────────────────────────────────────────────────────────

/**
 * The tab this bridge drives.
 *
 * Deliberately the ACTIVE Gmail tab rather than any Gmail tab: with several open, silently
 * driving whichever one Chrome lists first means the human watches the wrong window while
 * the agent types somewhere else.
 */
async function findGmailTab(): Promise<number> {
  const active = await chrome.tabs.query({
    active: true,
    currentWindow: true,
    url: "https://mail.google.com/*",
  });
  if (active[0]?.id !== undefined) return active[0].id;

  const any = await chrome.tabs.query({ url: "https://mail.google.com/*" });
  if (any[0]?.id !== undefined) return any[0].id;

  throw new DebuggerUnavailable(
    "No Gmail tab is open in this browser. Open mail.google.com, make sure you are signed " +
      "in, and start the run again.",
  );
}

// ── the four methods, plus lifecycle ────────────────────────────────────────

async function handle(frame: BackendFrame): Promise<void> {
  if (frame.type === "welcome") {
    console.info("[bridge] paired, session", frame.sessionId);
    // Persist whatever came back, and CLEAR the pairing code once a token exists: a code is
    // single use, so keeping it around would only let a later reconnect present a burnt one
    // and be refused. The token is now the credential.
    const update: Partial<Settings> = {};
    if (frame.bridgeToken) {
      update.bridgeToken = frame.bridgeToken;
      update.pairingCode = "";
    }
    if (frame.account) update.account = frame.account;
    if (Object.keys(update).length) await chrome.storage.local.set(update);
    return;
  }
  if (frame.type !== "call") return;

  try {
    const result = await dispatch(frame);
    client?.send({ type: "result", id: frame.id, ok: true, result });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const code = error instanceof DebuggerUnavailable ? "SURFACE_UNAVAILABLE" : null;
    client?.send({ type: "error", id: frame.id, ok: false, error: { message, code } });
  }
}

async function dispatch(frame: Extract<BackendFrame, { type: "call" }>): Promise<unknown> {
  switch (frame.method) {
    case "start": {
      await stopSession();
      const tabId = await findGmailTab();
      session = new BridgeSession({
        tabId,
        boundVerbs: frame.params.boundVerbs,
        ...(frame.params.tokenBudget === undefined ? {} : { tokenBudget: frame.params.tokenBudget }),
      });
      await startKeepalive();
      return { tabId };
    }
    case "stop":
      await stopSession();
      return { stopped: true };
    case "observe":
      return requireSession().observe();
    case "act":
      return requireSession().act(frame.params.call);
    case "preview":
      return requireSession().preview(frame.params.call);
    case "fingerprint":
      return requireSession().fingerprintFor(frame.params.call);
    case "approve":
      requireSession().approve(frame.params.fingerprint);
      return { approved: true };
  }
}

function requireSession(): BridgeSession {
  if (!session) {
    throw new DebuggerUnavailable("no session is running; the backend must call start first");
  }
  return session;
}

async function stopSession(): Promise<void> {
  const current = session;
  session = null;
  await stopKeepalive();
  if (current) await current.close();
}

// ── keepalive ───────────────────────────────────────────────────────────────

async function startKeepalive(): Promise<void> {
  await chrome.alarms.create(KEEPALIVE_ALARM, {
    periodInMinutes: KEEPALIVE_PERIOD_MINUTES,
  });
}

async function stopKeepalive(): Promise<void> {
  await chrome.alarms.clear(KEEPALIVE_ALARM);
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== KEEPALIVE_ALARM) return;
  // Waking is the point; the work is incidental. If the socket died while we were asleep,
  // this is where it gets noticed and rebuilt.
  if (session && client && !client.connected) client.connect();
});

// ── the tab going away ──────────────────────────────────────────────────────

chrome.debugger.onDetach.addListener((source, reason) => {
  if (!session || source.tabId !== session.tabId) return;
  // Navigation within Gmail detaches too, and that is routine — the session re-attaches on
  // the next command. Only tell the backend when the target is genuinely gone, so a normal
  // page transition does not read as a failed run.
  session.markDetached();
  if (reason === "target_closed") {
    client?.send({ type: "detached", reason: "the Gmail tab was closed" });
    void stopSession();
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  if (session?.tabId === tabId) {
    client?.send({ type: "detached", reason: "the Gmail tab was closed" });
    void stopSession();
  }
});

// ── wiring ──────────────────────────────────────────────────────────────────

async function connect(): Promise<void> {
  const settings = await readSettings();
  if (!settings.pairingCode && !settings.bridgeToken) {
    status = { state: "rejected", reason: "not paired yet — open the extension to pair" };
    return;
  }

  client?.disconnect();
  client = new BridgeClient({
    url: settings.backendUrl,
    bridgeToken: settings.bridgeToken,
    pairingCode: settings.pairingCode,
    extensionVersion: chrome.runtime.getManifest().version,
    onFrame: handle,
    onStatus: (next) => {
      status = next;
    },
  });
  client.connect();
}

/** The popup asks for status and can trigger a reconnect after pairing. */
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "status") {
    void chrome.storage.local.get({ account: "", bridgeToken: "" }).then((stored) =>
      sendResponse({
        status,
        running: session !== null,
        account: String(stored.account ?? ""),
        paired: Boolean(stored.bridgeToken),
      }),
    );
    return true; // async response
  }
  if (message?.type === "unpair") {
    // Forget the credential entirely. Anything less leaves a browser that still answers for
    // an account the user believes they disconnected.
    void chrome.storage.local
      .set({ bridgeToken: "", pairingCode: "", account: "" })
      .then(() => {
        client?.disconnect();
        void stopSession();
        sendResponse({ ok: true });
      });
    return true;
  }
  if (message?.type === "reconnect") {
    void connect().then(() => sendResponse({ ok: true }));
    return true; // async response
  }
  return false;
});

chrome.runtime.onInstalled.addListener(() => void connect());
chrome.runtime.onStartup.addListener(() => void connect());

// The worker is also revived by events; reconnect on every cold start.
void connect();
