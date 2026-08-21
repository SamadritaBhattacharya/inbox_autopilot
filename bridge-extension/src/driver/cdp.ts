/**
 * A thin, typed wrapper over `chrome.debugger`.
 *
 * Everything the agent does to the page goes through here, and every call is a **trusted**
 * input event: the page sees `isTrusted: true`, indistinguishable from a human. That matters
 * beyond fidelity — Gmail ignores synthetic `.click()` on several controls, and a JS click
 * skips the pointer handlers a real press fires.
 *
 * **Attachment is the fragile part, so it is all in one place.** Gmail is a single-page app;
 * the debugger detaches on navigation, on tab close, and whenever a human opens DevTools on
 * the same tab. Scattering `attach` calls through the driver produces a class of bug that
 * only reproduces after a user does something ordinary, so attachment is owned here and
 * re-established lazily on every command.
 */

const PROTOCOL_VERSION = "1.3";

/** Chrome's own message when DevTools already owns the tab. Worth naming precisely. */
const DEVTOOLS_CONFLICT = /another debugger|devtools is already attached|already attached/i;

export class DebuggerUnavailable extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DebuggerUnavailable";
  }
}

export class CdpSession {
  private attached = false;

  constructor(private readonly tabId: number) {}

  get target(): chrome.debugger.Debuggee {
    return { tabId: this.tabId };
  }

  /**
   * Attach if not already attached. Idempotent, because every command calls it: a session
   * that assumes it is still attached is a session that breaks the first time the user
   * navigates.
   */
  async ensureAttached(): Promise<void> {
    if (this.attached) return;
    try {
      await chrome.debugger.attach(this.target, PROTOCOL_VERSION);
      this.attached = true;
    } catch (error) {
      const message = describe(error);
      // Already attached by US (a race between two commands) is success, not failure.
      if (/already attached to this target/i.test(message)) {
        this.attached = true;
        return;
      }
      if (DEVTOOLS_CONFLICT.test(message)) {
        throw new DebuggerUnavailable(
          "Chrome DevTools is open on this tab, and only one debugger can attach at a " +
            "time. Close DevTools on the Gmail tab and start the run again.",
        );
      }
      throw new DebuggerUnavailable(`could not attach to the Gmail tab: ${message}`);
    }
  }

  /** Called by the worker when Chrome tells us the session went away. */
  markDetached(): void {
    this.attached = false;
  }

  async detach(): Promise<void> {
    if (!this.attached) return;
    this.attached = false;
    try {
      await chrome.debugger.detach(this.target);
    } catch {
      // Already gone. Nothing to clean up, and throwing here would mask the real error that
      // usually precedes it.
    }
  }

  async send<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    await this.ensureAttached();
    try {
      return (await chrome.debugger.sendCommand(this.target, method, params)) as T;
    } catch (error) {
      const message = describe(error);
      // A detach between `ensureAttached` and `sendCommand` is normal on an SPA. Re-attach
      // once and retry; failing here would surface a navigation as an action failure.
      if (/not attached|detached/i.test(message)) {
        this.attached = false;
        await this.ensureAttached();
        return (await chrome.debugger.sendCommand(this.target, method, params)) as T;
      }
      throw error;
    }
  }

  /**
   * Evaluate an expression in the page and return its value.
   *
   * `awaitPromise` and `returnByValue` are both required: without the first an async
   * expression resolves to a Promise handle, and without the second we get a remote object
   * reference that is useless on this side.
   */
  async evaluate<T>(expression: string): Promise<T> {
    const result = await this.send<{
      result?: { value?: T };
      exceptionDetails?: { text?: string; exception?: { description?: string } };
    }>("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });

    if (result.exceptionDetails) {
      const detail =
        result.exceptionDetails.exception?.description ??
        result.exceptionDetails.text ??
        "unknown error";
      throw new Error(`page evaluation failed: ${detail}`);
    }
    return result.result?.value as T;
  }
}

function describe(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  // `chrome.debugger` rejects with `{message: string}` rather than an Error.
  if (error && typeof error === "object" && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}
