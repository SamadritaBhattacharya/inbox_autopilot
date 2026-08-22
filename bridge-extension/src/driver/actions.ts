/**
 * The action handlers — one per verb, all dispatching **trusted** input.
 *
 * Targets arrive as an index and a token; both are resolved by the validator immediately
 * before we get here, and only here. The model never saw the coordinate this clicks or the
 * address this types.
 *
 * Ported from the handlers in `backend/app/surface/playwright_surface.py`, including the two
 * behaviours that were paid for in production debugging: typing long text as one insert, and
 * a timeout wall that scales with the payload.
 */
import type { ActionCall, ActionResult } from "@inbox/contracts";

import type { CdpSession } from "./cdp";
import { isAllTokens, type ResolvedAction } from "../dispatch/validator";

/** Per-verb timeout walls. A breach is `ACTION_TIMEOUT` — a typed failure, not a crash. */
export const TIMEOUTS: Record<string, number> = {
  Navigate: 30_000,
  Click: 10_000,
  ReadThread: 10_000,
  Type: 10_000,
  Clear: 5_000,
  PressKey: 5_000,
  Scroll: 5_000,
  Archive: 10_000,
  Send: 20_000,
  WaitFor: 30_000,
};
export const DEFAULT_TIMEOUT = 10_000;

/**
 * Above this many characters, type in ONE bulk insert instead of key by key.
 *
 * Key-by-key is the honest simulation, and it is what makes Gmail's recipient autocomplete
 * produce a chip — so short fields keep it. But every keystroke is three separate CDP round
 * trips, and against a real browser that is ~50ms each: a 190-character body took ~9.5s and
 * breached its 10s wall. The agent then "helpfully" cleared the field and retried in chunks,
 * breaching it again — writing correct text and deleting it, forever. An email body has no
 * per-keystroke handler worth simulating; a recipient field does.
 */
export const TYPE_KEYSTROKE_LIMIT = 60;

/**
 * Budget per character for the wall. A fixed timeout on an action whose duration is LINEAR
 * in its payload is not a tuning mistake, it is the wrong model.
 */
export const TYPE_MS_PER_CHAR = 80;

/**
 * Ceiling on a scaled wall. The budget above is sized for the KEYSTROKE fallback; the normal
 * path is a single insert and finishes well under a second. Without a cap a pathological 20k
 * body would buy itself a half-hour hang, and the point of a wall is that a stuck action
 * cannot eat the run.
 */
export const TYPE_TIMEOUT_MAX = 60_000;

export function timeoutFor(call: ActionCall): number {
  const base = TIMEOUTS[call.name] ?? DEFAULT_TIMEOUT;
  if (call.name !== "Type") return base;
  const text = String(call.args?.text ?? call.args?.recipient ?? "");
  if (text.length <= TYPE_KEYSTROKE_LIMIT) return base;
  return Math.min(base + text.length * TYPE_MS_PER_CHAR, TYPE_TIMEOUT_MAX);
}

const ok = (reason: string): ActionResult => ({ success: true, reason, errorCode: null }) as ActionResult;
const failed = (reason: string, errorCode: string | null = null): ActionResult =>
  ({ success: false, reason, errorCode }) as ActionResult;

/**
 * The text this action should type.
 *
 * A resolved token wins over literal text: `Type(index=54, recipient="P1")` must put the
 * real address in the box, not the string "P1".
 */
function textFor(action: ResolvedAction): string {
  for (const arg of ["recipient", "cc", "bcc", "text"] as const) {
    const raw = String(action.call.args?.[arg] ?? "");
    if (!raw) continue;
    // Prose goes in verbatim; only a whole-token value is substituted. See
    // `tokenBearingArgs` for why a sentence must never be rewritten.
    if (arg === "text" && !isAllTokens(raw)) return raw;
    let out = raw;
    for (const [token, real] of Object.entries(action.resolvedArgs)) {
      out = out.split(token).join(real);
    }
    return out;
  }
  return "";
}

export class ActionDriver {
  constructor(private readonly cdp: CdpSession) {}

  async perform(action: ResolvedAction): Promise<ActionResult> {
    switch (action.call.name) {
      case "Click":
        return this.click(action);
      case "Type":
        return this.type(action);
      case "Clear":
        return this.clear(action);
      case "PressKey":
        return this.pressKey(action);
      case "Scroll":
        return this.scroll(action);
      case "WaitFor":
        return this.waitFor(action);
      // `Send`, `Archive` and friends are clicks on a resolved control; the gate has already
      // run by the time we are here.
      case "Send":
      case "Archive":
      case "MarkRead":
      case "DeleteForever":
        return this.click(action);
      default:
        return failed(`${action.call.name} is not handled by the bridge`, "VERB_NOT_BOUND");
    }
  }

  private async click(action: ResolvedAction): Promise<ActionResult> {
    if (!action.point) return failed(`${action.call.name} needs an index`);
    const { x, y } = action.point;

    // Move first: hover state is real, and handlers bound to pointerenter will not fire for
    // a click that teleports.
    await this.mouse("mouseMoved", x, y, 0);
    await this.mouse("mousePressed", x, y, 1);
    await this.mouse("mouseReleased", x, y, 1);
    return ok(`clicked [${action.call.args?.index}]`);
  }

  private async type(action: ResolvedAction): Promise<ActionResult> {
    const text = textFor(action);
    if (action.point) {
      const { x, y } = action.point;
      await this.mouse("mouseMoved", x, y, 0);
      await this.mouse("mousePressed", x, y, 1);
      await this.mouse("mouseReleased", x, y, 1);
    }

    if (text.length > TYPE_KEYSTROKE_LIMIT) {
      // One round trip instead of three per character. `Input.insertText` is a real CDP
      // input event — the page sees a trusted `beforeinput`/`input`, which is what a
      // contenteditable body actually listens for. It does NOT synthesize individual key
      // events, which is exactly why short fields are excluded: an address box builds its
      // recipient chip from keystrokes.
      await this.cdp.send("Input.insertText", { text });
    } else {
      for (const character of text) {
        await this.cdp.send("Input.dispatchKeyEvent", { type: "char", text: character });
      }
    }
    // NEVER log `text` — a recipient resolved from a token is raw PII by this point.
    return ok(`typed ${text.length} characters`);
  }

  private async clear(action: ResolvedAction): Promise<ActionResult> {
    if (action.point) {
      const { x, y } = action.point;
      await this.mouse("mouseMoved", x, y, 0);
      await this.mouse("mousePressed", x, y, 1);
      await this.mouse("mouseReleased", x, y, 1);
    }
    await this.key("keyDown", "a", { modifiers: 2 }); // Ctrl
    await this.key("keyUp", "a", { modifiers: 2 });
    await this.key("keyDown", "Delete");
    await this.key("keyUp", "Delete");
    return ok("cleared the field");
  }

  private async pressKey(action: ResolvedAction): Promise<ActionResult> {
    const key = String(action.call.args?.key ?? "Enter");
    const { base, modifiers } = parseKey(key);
    await this.key("keyDown", base, { modifiers });
    await this.key("keyUp", base, { modifiers });
    return ok(`pressed ${key}`);
  }

  private async scroll(action: ResolvedAction): Promise<ActionResult> {
    const direction = String(action.call.args?.direction ?? "down");
    const amount = Number(action.call.args?.amount ?? 1);
    const height = await this.cdp.evaluate<number>("window.innerHeight");
    const delta = (direction === "up" ? -1 : 1) * (height || 800) * 0.85 * amount;

    await this.cdp.send("Input.dispatchMouseEvent", {
      type: "mouseWheel",
      x: 10,
      y: 10,
      deltaX: 0,
      deltaY: delta,
    });
    return ok(`scrolled ${direction}`);
  }

  private async waitFor(action: ResolvedAction): Promise<ActionResult> {
    const seconds = Math.min(Number(action.call.args?.seconds ?? 1), TIMEOUTS.WaitFor! / 1000);
    await new Promise((resolve) => setTimeout(resolve, seconds * 1000));
    return ok(`waited ${seconds}s`);
  }

  // ── primitives ────────────────────────────────────────────────────────────

  private mouse(type: string, x: number, y: number, clickCount: number): Promise<unknown> {
    return this.cdp.send("Input.dispatchMouseEvent", {
      type,
      x: Math.round(x),
      y: Math.round(y),
      button: clickCount > 0 ? "left" : "none",
      buttons: type === "mousePressed" ? 1 : 0,
      clickCount,
    });
  }

  private key(
    type: "keyDown" | "keyUp",
    key: string,
    options: { modifiers?: number } = {},
  ): Promise<unknown> {
    const descriptor = KEY_CODES[key];
    return this.cdp.send("Input.dispatchKeyEvent", {
      type,
      key,
      modifiers: options.modifiers ?? 0,
      ...(descriptor ?? {}),
    });
  }
}

/** `"Control+Enter"` -> base key plus CDP's modifier bitmask. */
export function parseKey(key: string): { base: string; modifiers: number } {
  const parts = key.split("+");
  const base = parts.pop() ?? "Enter";
  let modifiers = 0;
  for (const part of parts) {
    const lowered = part.toLowerCase();
    if (lowered === "alt") modifiers |= 1;
    else if (lowered === "control" || lowered === "ctrl") modifiers |= 2;
    else if (lowered === "meta" || lowered === "cmd") modifiers |= 4;
    else if (lowered === "shift") modifiers |= 8;
  }
  return { base, modifiers };
}

/**
 * Chrome needs `windowsVirtualKeyCode` for non-printing keys; without it Enter and Tab
 * arrive as events the page ignores. Only the keys the agent actually presses are listed —
 * a full table would be dead code pretending to be thorough.
 */
const KEY_CODES: Record<string, { windowsVirtualKeyCode: number; code?: string }> = {
  Enter: { windowsVirtualKeyCode: 13, code: "Enter" },
  Tab: { windowsVirtualKeyCode: 9, code: "Tab" },
  Escape: { windowsVirtualKeyCode: 27, code: "Escape" },
  Backspace: { windowsVirtualKeyCode: 8, code: "Backspace" },
  Delete: { windowsVirtualKeyCode: 46, code: "Delete" },
  ArrowDown: { windowsVirtualKeyCode: 40, code: "ArrowDown" },
  ArrowUp: { windowsVirtualKeyCode: 38, code: "ArrowUp" },
  a: { windowsVirtualKeyCode: 65, code: "KeyA" },
};
