/**
 * Which actions are irreversible — by what they DO, not by what they are called.
 *
 * **The hole this closes.** Gating used to be a set of verb names: `Send`, `DeleteForever`,
 * `SendInvite`. But the compose worker is also bound to `Click`, and Gmail's Send button is
 * an ordinary element with an index. `Click(index=108)` sends the email, is not in the verb
 * set, and would dispatch with no approval at all. The strongest guarantee in the system —
 * "nothing leaves your mailbox without you clicking approve" — was one plausible tool call
 * away from being false, and the model reaches for that call naturally: it writes "Then
 * click Send" on its own.
 *
 * So the question is asked of the TARGET, not the verb. A click lands on an element with an
 * accessible name, and a control named "Send (Ctrl-Enter)" does the same irreversible thing
 * whichever verb was used to press it.
 *
 * Matched on the accessible name rather than a Gmail-specific selector: the name is what the
 * funnel already carries, what a human reads off the screen, and what survives redesigns a
 * CSS path would not. False positives are cheap — an extra approval card — and false
 * negatives are the entire failure this module exists to prevent.
 *
 * Ported from `backend/app/workers/irreversible.py`.
 */
import type { ActionCall, Observation } from "@inbox/contracts";

/** Verbs whose name alone settles it. */
export const GATED_VERBS = new Set(["Send", "DeleteForever", "SendInvite"]);

/** Verbs that press whatever is under them, and so inherit their target's consequences. */
export const TARGETING_VERBS = new Set(["Click", "PressKey"]);

/**
 * Controls that dispatch mail or destroy it. Anchored so "Sender", "Resend later" and
 * "Send feedback" do not trip it, while "Send", "Send (Ctrl-Enter)" and "Send & Archive" do.
 */
export const IRREVERSIBLE_NAMES =
  /^\s*(send\b(?!\s*(feedback|later|to\s+yourself))|delete\s+forever|empty\s+(trash|spam)|delete\s+all)/i;

/**
 * Keystrokes that submit a compose window. Ctrl+Enter sends in Gmail, with no button
 * involved and therefore no element name to inspect.
 */
export const SENDING_KEYS = /^(control|ctrl|meta|cmd)\+enter$/i;

/** The accessible name of the element an action points at, if it can be found. */
export function targetName(observation: Observation | null | undefined, index: unknown): string {
  if (!observation || typeof index !== "number" || !Number.isInteger(index)) return "";
  for (const element of observation.elements) {
    if (element.index === index) return element.name ?? "";
  }
  return "";
}

/**
 * Would dispatching this be impossible to undo?
 *
 * `observation` is optional so callers that genuinely do not have one still get the
 * verb-level answer. Passing it is what closes the click path, so every caller that can
 * should.
 */
export function isIrreversible(
  call: ActionCall | null | undefined,
  observation?: Observation | null,
): boolean {
  if (!call) return false;
  if (GATED_VERBS.has(call.name)) return true;
  if (!TARGETING_VERBS.has(call.name)) return false;

  if (call.name === "PressKey" && SENDING_KEYS.test(String(call.args?.key ?? ""))) {
    return true;
  }

  const name = targetName(observation, call.args?.index);
  return Boolean(name) && IRREVERSIBLE_NAMES.test(name);
}
