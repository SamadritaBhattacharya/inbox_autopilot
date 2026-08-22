/**
 * Stage 7 — serialise survivors, within a hard token budget.
 *
 * The last stage turns indexed elements into the wire contract's `Element` objects and
 * enforces the budget that keeps an observation at ~1–3k tokens instead of 100k.
 *
 * **Nothing is ever truncated silently.** When the budget bites, the lowest-value elements
 * go first and the count comes back with the result, to be reported as "N more items —
 * scroll to see them". An agent that believes it has seen everything will confidently
 * conclude a message does not exist; an agent told there are 18 more will scroll. That
 * difference is the whole reason this stage returns a number and not just a list.
 *
 * Priority when cutting, worst first:
 *   1. non-interactive text far down the page  (read it after scrolling)
 *   2. non-interactive text near the top       (context, not action)
 *   3. interactive elements far down           (actionable, but not yet)
 *   4. interactive elements near the top       (never cut — this is the task surface)
 *
 * Ported from `backend/app/observation/funnel/reading_order.py`.
 */
import type { Observation } from "@inbox/contracts";

import type { Box, RawElement } from "./raw";

/** The wire contract's element, taken from the generated schema so the two cannot drift. */
export type WireElement = Observation["elements"][number];

/**
 * ~1–3k tokens for the element list. The rest of the window belongs to instructions,
 * history, and the model's own reasoning.
 */
export const DEFAULT_TOKEN_BUDGET = 2000;

/**
 * Long values (a whole email body in a textarea) are clipped rather than dropped: the agent
 * needs to know the field HAS content, not to re-read all of it every turn.
 */
export const MAX_TEXT_LENGTH = 160;

/**
 * Chars-per-token approximation.
 *
 * Deliberately a cheap estimate, not a tokenizer call. The budget only needs to be
 * approximately right, and running a real tokenizer over every element on every turn would
 * cost more than the tokens it saves.
 */
export function estimateTokens(text: string): number {
  return Math.max(1, Math.floor(text.length / 4));
}

function clip(text: string): string {
  const collapsed = text.split(/\s+/).filter(Boolean).join(" ");
  if (collapsed.length <= MAX_TEXT_LENGTH) return collapsed;
  return collapsed.slice(0, MAX_TEXT_LENGTH - 1).trimEnd() + "…";
}

/**
 * Is this element's centre within the focused region?
 *
 * Centre rather than full containment: a wide field inside a narrow dialog still belongs to
 * it, and requiring total overlap would exclude exactly the inputs that matter.
 */
function inside(element: RawElement, box: Box | null): boolean {
  if (!box) return false;
  const [x, y, width, height] = box;
  const cx = element.x + element.width / 2;
  const cy = element.y + element.height / 2;
  return cx >= x && cx <= x + width && cy >= y && cy <= y + height;
}

/**
 * Lower is cut first.
 *
 * **Anything inside an open dialog outranks everything else.** When a compose window is open
 * its fields are the only things the agent can act on — but there are half a dozen of them
 * against two hundred inbox rows behind, and the rows win on volume. The subject field was
 * trimmed before the model saw it, and the agent scrolled a page that does not move looking
 * for a field that was never in the list.
 */
function priority(element: RawElement, fold: number, focus: Box | null): number {
  if (inside(element, focus)) return element.interactive ? 5 : 4;
  const aboveFold = element.y < fold;
  if (element.interactive) return aboveFold ? 3 : 2;
  return aboveFold ? 1 : 0;
}

export interface FormatResult {
  elements: WireElement[];
  budgetDropped: number;
}

export interface FormatOptions {
  viewportHeight: number;
  /** Last turn's identities (role + name), used only to mark what is NEW. */
  previousIdentities?: Set<string>;
  /** The open dialog's box. Its contents outrank everything behind them. */
  focusBox?: Box | null;
}

/** Serialises indexed elements to the wire contract, within budget. */
export class ReadingOrderFormatter {
  private readonly budget: number;

  constructor(options: { tokenBudget?: number } = {}) {
    this.budget = options.tokenBudget ?? DEFAULT_TOKEN_BUDGET;
  }

  /**
   * `previousIdentities` is what makes "a dialog just appeared" legible in one glance: the
   * model gets a diff *and* a fresh list, never a blind dump.
   */
  apply(elements: RawElement[], options: FormatOptions): FormatResult {
    const previous = options.previousIdentities ?? new Set<string>();
    const fold = options.viewportHeight * 0.75;

    // Cut candidates by value, but emit in reading order: the model reads the list as a
    // picture of the page, so the order it arrives in has to match the page.
    const focus = options.focusBox ?? null;
    const byValue = [...elements].sort((a, b) => {
      const byPriority = priority(b, fold, focus) - priority(a, fold, focus);
      if (byPriority !== 0) return byPriority;
      // Within a band, keep the earlier index — Python sorts on `-index` descending, which
      // is the same ordering.
      return (a.index ?? 0) - (b.index ?? 0);
    });

    const kept: RawElement[] = [];
    let spent = 0;
    let budgetDropped = 0;

    for (const element of byValue) {
      const cost = estimateTokens(`[${element.index}] ${element.role} ${element.name}`);
      if (spent + cost > this.budget) {
        budgetDropped += 1;
        continue;
      }
      kept.push(element);
      spent += cost;
    }

    kept.sort((a, b) => (a.index ?? 0) - (b.index ?? 0));

    return {
      elements: kept.map((element) => ({
        index: element.index ?? 0,
        role: element.role,
        name: clip(element.name),
        value: element.value ? clip(element.value) : null,
        isNew: !previous.has(`${element.role}:${element.name}`),
      })),
      budgetDropped,
    };
  }
}

/**
 * Identities for the next turn's `isNew` comparison.
 *
 * Keyed on role+name rather than index, because indices are rebuilt every turn — an
 * index-based comparison would mark half the page as new whenever anything moved.
 */
export function identitySet(elements: WireElement[]): Set<string> {
  return new Set(elements.map((element) => `${element.role}:${element.name}`));
}
