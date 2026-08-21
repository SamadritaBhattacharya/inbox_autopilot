/**
 * One agent session over one Gmail tab — the extension's half of the `EmailSurface` port.
 *
 * Same four methods the backend knows: `observe`, `act`, `preview`, `approve`. The backend
 * calls them by RPC and cannot tell this from the Playwright surface, which is the whole
 * point of keeping that port at four methods.
 *
 * **Everything sensitive is held here and nowhere else.** The vault, the `index -> geometry`
 * map, the set of approved fingerprints. The backend reasons over tokens and integers; it
 * could not resolve one if it tried.
 *
 * One session per run, never per process: a vault shared between runs would let tokens
 * correlate across sessions, and a geometry map shared between tabs would dispatch clicks at
 * coordinates from somebody else's screen.
 */
import { observationSchema, type ActionCall, type ActionResult, type Observation } from "@inbox/contracts";

import { ActionDriver, timeoutFor } from "./driver/actions";
import { CdpSession, DebuggerUnavailable } from "./driver/cdp";
import { ActionValidator, DispatchRejected, approvalFingerprint } from "./dispatch/validator";
import { isIrreversible } from "./dispatch/irreversible";
import { ObservationFunnel } from "./funnel/pipeline";
import { identitySet } from "./funnel/readingOrder";
import type { Point } from "./funnel/som";
import { EXTRACT_JS, MAX_NODES, parseElements, parseMeta, type ExtractResult } from "./page/extract";
import { PiiTokenizer } from "./security/tokenizer";
import { SessionPiiVault } from "./security/vault";

/** Reads the live compose fields for the approval preview. */
const PREVIEW_JS = String.raw`
(() => {
  const pick = (selectors) => {
    for (const selector of selectors) {
      const el = document.querySelector(selector);
      if (!el) continue;
      const text = (el.value !== undefined ? el.value : el.innerText) || '';
      if (text.trim()) return text.trim();
    }
    return '';
  };
  const to = pick([
    '[role="dialog"] [name="to"]',
    '[role="dialog"] input[aria-label*="To"]',
    'input[name="to"]',
    'input[type="email"]',
  ]);
  const subject = pick([
    '[role="dialog"] [name="subjectbox"]',
    '[role="dialog"] input[aria-label*="Subject"]',
    'input[name="subject"]',
  ]);
  const body = pick([
    '[role="dialog"] [g_editable="true"]',
    '[role="dialog"] [contenteditable="true"]',
    '[contenteditable="true"]',
    'textarea',
  ]);
  return { to, subject, body };
})()
`;

export interface SessionOptions {
  tabId: number;
  boundVerbs: string[];
  tokenBudget?: number;
  tokenizeNames?: boolean;
}

export class BridgeSession {
  readonly tabId: number;

  private readonly vault = new SessionPiiVault();
  private readonly tokenizer: PiiTokenizer;
  private readonly funnel: ObservationFunnel;
  private readonly cdp: CdpSession;
  private readonly driver: ActionDriver;
  private readonly boundVerbs: Set<string>;

  /** Rebuilt on every `observe()`. Never carried across turns, never sent anywhere. */
  private geometry = new Map<number, Point>();
  private lastObservation: Observation | null = null;
  private previousIdentities = new Set<string>();

  /**
   * Approval fingerprints. Held HERE because the extension is what dispatches, so the
   * extension is what must hold the authorization — a check anywhere else could be routed
   * around by anything that reaches `act()`.
   */
  private readonly approved = new Set<string>();

  constructor(options: SessionOptions) {
    this.tabId = options.tabId;
    this.tokenizer = new PiiTokenizer(this.vault, {
      tokenizeNames: options.tokenizeNames ?? true,
    });
    this.funnel = new ObservationFunnel(
      this.tokenizer,
      options.tokenBudget === undefined ? {} : { tokenBudget: options.tokenBudget },
    );
    this.cdp = new CdpSession(options.tabId);
    this.driver = new ActionDriver(this.cdp);
    this.boundVerbs = new Set(options.boundVerbs);
  }

  // ── the port ──────────────────────────────────────────────────────────────

  /** A fresh, tokenized, numbered view. Rebuilt from scratch every call. */
  async observe(): Promise<Observation> {
    const raw = await this.cdp.evaluate<ExtractResult>(`(${EXTRACT_JS})(${MAX_NODES})`);
    if (!raw || !Array.isArray(raw.elements)) {
      throw new Error("the page returned no elements; it may still be loading");
    }

    const elements = parseElements(raw.elements);
    const meta = parseMeta(raw.meta ?? {});

    const { observation, geometry } = this.funnel.run(elements, meta, {
      previousIdentities: this.previousIdentities,
    });

    // ── the egress checkpoint ──
    //
    // This is the last line of code that runs before mailbox-derived data leaves the
    // machine. Two guards, in the order that matters:

    // 1. Does it still contain a real identifier? The funnel is supposed to make this
    //    impossible, which is exactly why it is worth asserting: a leak here would be
    //    silent, permanent, and discovered by someone else. Refusing to send beats sending
    //    a person's address to a model because a stage regressed.
    const serialized = JSON.stringify(observation);
    if (this.tokenizer.containsPii(serialized)) {
      throw new PiiLeakBlocked(
        "the observation still contained a real address, phone number, or name after " +
          "tokenization, so it was NOT sent. This is a bug in the funnel, not in your " +
          "mailbox.",
      );
    }

    // 2. Does it match the shared contract? Validating our own output means a drifted
    //    funnel fails here, named, rather than becoming a confusing 422 in someone else's
    //    service.
    const checked = observationSchema.safeParse(observation);
    if (!checked.success) {
      throw new Error(`the funnel produced an invalid observation: ${checked.error.message}`);
    }

    this.geometry = geometry;
    this.lastObservation = observation;
    this.previousIdentities = identitySet(observation.elements);
    return observation;
  }

  /** Perform one action, targeted by index and token. */
  async act(call: ActionCall): Promise<ActionResult> {
    // Re-read the live draft before validating anything irreversible. The human approved
    // WORDS, not a button, so consent is checked against what the fields say at the moment
    // of dispatch — not against what they said when the card was rendered.
    let preview = "";
    if (isIrreversible(call, this.lastObservation)) {
      preview = await this.preview(call).catch(() => "");
    }

    let resolved;
    try {
      resolved = new ActionValidator({
        vault: this.vault,
        geometry: this.geometry,
        boundVerbs: this.boundVerbs,
        approved: this.approved,
        observation: this.lastObservation,
        preview,
      }).validate(call);
    } catch (error) {
      if (error instanceof DispatchRejected) {
        // A refusal is information, not a crash: the agent sees a typed failure and can
        // re-observe or choose differently.
        return { success: false, reason: error.message, errorCode: error.errorCode } as ActionResult;
      }
      throw error;
    }

    try {
      return await withTimeout(this.driver.perform(resolved), timeoutFor(call), call.name);
    } catch (error) {
      if (error instanceof ActionTimeout) {
        return {
          success: false,
          reason: error.message,
          errorCode: "ACTION_TIMEOUT",
        } as ActionResult;
      }
      if (error instanceof DebuggerUnavailable) throw error;
      return {
        success: false,
        reason: `${call.name} failed: ${error instanceof Error ? error.message : String(error)}`,
        errorCode: null,
      } as ActionResult;
    }
  }

  /**
   * A human-readable, RESOLVED description of what `call` is about to do.
   *
   * An approval card showing "send to P17" is useless — verifying the recipient is the whole
   * point of the gate, and only this side can turn a token back into a name. The result goes
   * to the authenticated cockpit and nowhere else; it must never re-enter the model's
   * context.
   */
  async preview(call: ActionCall): Promise<string> {
    if (call.name === "DeleteForever") {
      return `Permanently delete [${call.args?.index}] — this cannot be undone.`;
    }
    const fields = await this.cdp.evaluate<{ to: string; subject: string; body: string }>(
      PREVIEW_JS,
    );
    if (!fields) return `${call.name} with ${JSON.stringify(call.args ?? {})}`;
    return [
      `To:      ${fields.to || "(empty)"}`,
      `Subject: ${fields.subject || "(empty)"}`,
      "",
      fields.body || "(empty body)",
    ].join("\n");
  }

  /**
   * Record a human decision authorizing ONE exact payload.
   *
   * The fingerprint covers the previewed CONTENT, so an edit after approval no longer
   * matches and the gate asks again.
   */
  approve(fingerprint: string): void {
    this.approved.add(fingerprint);
  }

  /** The fingerprint for a call as it stands right now — what the gate must approve. */
  async fingerprintFor(call: ActionCall): Promise<string> {
    const preview = isIrreversible(call, this.lastObservation)
      ? await this.preview(call).catch(() => "")
      : "";
    return approvalFingerprint(call, preview);
  }

  async close(): Promise<void> {
    await this.cdp.detach();
  }

  /** Chrome told us the debugger went away (navigation, tab close, DevTools). */
  markDetached(): void {
    this.cdp.markDetached();
  }
}

/**
 * Raised when tokenized data still contains PII. Deliberately fatal to the turn: the
 * alternative is shipping it and hoping.
 */
export class PiiLeakBlocked extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PiiLeakBlocked";
  }
}

class ActionTimeout extends Error {}

function withTimeout<T>(promise: Promise<T>, ms: number, verb: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new ActionTimeout(`${verb} exceeded its ${ms / 1000}s wall`)),
      ms,
    );
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}
