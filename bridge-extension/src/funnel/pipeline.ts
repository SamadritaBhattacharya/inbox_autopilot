/**
 * The funnel — seven stages from raw DOM to a tokenized, numbered `Observation`.
 *
 *     extract -> visibility -> occlusion -> wrapperCollapse -> piiTokenize -> som -> readingOrder
 *                                                             ^^^^^^^^^^^^
 * **Stage 5's position is a security control, not a preference.** The tokenizer runs before
 * indexing and formatting, so no later stage — and therefore nothing that could be
 * serialized, logged, or *sent to the backend* — ever holds a raw address. In the extension
 * that last one is the whole game: everything after this point crosses a network boundary.
 * Moving it later would leave a window in which real PII exists downstream, and
 * `STAGE_ORDER` plus its assertion exist to make that reordering fail loudly.
 *
 * The pipeline returns the `Observation` (which crosses the wire) alongside two things that
 * **never** do: the `index -> geometry` map, and the report of what was dropped.
 *
 * Ported from `backend/app/observation/funnel/pipeline.py`.
 */
import { PROTOCOL_VERSION, type Observation } from "@inbox/contracts";

import { OcclusionCuller } from "./occlusion";
import { ReadingOrderFormatter, type WireElement } from "./readingOrder";
import { emptyReport, reachableButUnlisted, type FunnelReport, type PageMeta, type RawElement } from "./raw";
import { SoMIndexer, type Point } from "./som";
import { VisibilityFilter } from "./visibility";
import { WrapperCollapser } from "./wrapperCollapse";
import type { PiiTokenizer } from "../security/tokenizer";

/** The canonical order. Changing it changes the security properties. */
export const STAGE_ORDER = [
  "extract",
  "visibility",
  "occlusion",
  "wrapperCollapse",
  "piiTokenize",
  "som",
  "readingOrder",
] as const;

/** Stages that must run before anything can serialize an element's text. */
const TOKENIZE_BEFORE = ["som", "readingOrder"] as const;

/**
 * Roles where a name is STRUCTURED rather than prose that happens to mention one. Only these
 * teach the tokenizer a person; see `security/tokenizer.ts` for why guessing at names in
 * free text is the wrong trade.
 */
const PERSON_ROLES = new Set(["sender", "recipient", "contact", "chip"]);

/** Fails at import if the pipeline order stops protecting PII. */
function assertTokenizerPrecedesSerialization(): void {
  const position = STAGE_ORDER.indexOf("piiTokenize");
  for (const later of TOKENIZE_BEFORE) {
    if (STAGE_ORDER.indexOf(later) < position) {
      throw new Error(
        `Funnel order is unsafe: '${later}' runs before 'piiTokenize'. Tokenization must ` +
          "precede any stage that serializes element text.",
      );
    }
  }
}
assertTokenizerPrecedesSerialization();

/**
 * Where the unlisted content is, in words the model can act on.
 *
 * "12 more items" alone is not actionable — an agent scrolls one way, sees the number
 * unchanged, and scrolls the same way again. Naming the direction turns a dead end into a
 * decision.
 */
export function scrollHint(report: FunnelReport): string | null {
  const total = reachableButUnlisted(report);
  if (total <= 0) return null;

  const parts: string[] = [];
  if (report.offscreenAbove) parts.push(`${report.offscreenAbove} above`);
  if (report.offscreenBelow) parts.push(`${report.offscreenBelow} below`);
  if (report.budgetDropped) parts.push(`${report.budgetDropped} trimmed to fit`);

  const where = parts.length ? parts.join(", ") : "elsewhere on the page";
  return `${total} more item${total === 1 ? "" : "s"} not shown: ${where}.`;
}

export interface FunnelResult {
  observation: Observation;
  /** index -> centre point. NEVER crosses the wire. */
  geometry: Map<number, Point>;
  report: FunnelReport;
}

export interface RunOptions {
  screenshotRef?: string | null;
  previousIdentities?: Set<string>;
  changed?: string | null;
}

/** Composes the stages. Add a capability by adding a stage, not by editing this. */
export class ObservationFunnel {
  private readonly tokenizer: PiiTokenizer;
  private readonly visibility = new VisibilityFilter();
  private readonly occlusion = new OcclusionCuller();
  private readonly collapser = new WrapperCollapser();
  private readonly indexer = new SoMIndexer();
  private readonly formatter: ReadingOrderFormatter;

  constructor(tokenizer: PiiTokenizer, options: { tokenBudget?: number } = {}) {
    this.tokenizer = tokenizer;
    this.formatter = new ReadingOrderFormatter(
      options.tokenBudget === undefined ? {} : { tokenBudget: options.tokenBudget },
    );
  }

  run(elements: RawElement[], meta: PageMeta, options: RunOptions = {}): FunnelResult {
    const report = emptyReport();
    report.extracted = elements.length;
    report.stages = [...STAGE_ORDER];

    // Learn people from the RAW element set, before any stage can prune it.
    //
    // This ran inside the tokenize stage once, and it was a latent leak: a later stage
    // (wrapper collapse folding a sender chip into its row) removed the only structured
    // occurrence of a name, so the name was never registered and every mention of it
    // downstream stayed in the clear. Registration must not depend on what survives —
    // pruning decides what the model SEES, never what the vault KNOWS.
    this.registerPeople(elements);

    const visible = this.visibility.apply(elements, meta);
    report.hidden = visible.hidden;
    report.offscreenAbove = visible.offscreenAbove;
    report.offscreenBelow = visible.offscreenBelow;

    const viewportArea = meta.viewportWidth * meta.viewportHeight;
    const unoccluded = this.occlusion.apply(visible.kept, viewportArea);
    report.occluded = unoccluded.occluded;

    const collapsed = this.collapser.apply(unoccluded.kept);
    report.collapsed = collapsed.collapsed;

    // ── stage 5: PII leaves the data here and never comes back ──
    const tokenized = this.tokenize(collapsed.kept);

    const { indexed, geometry } = this.indexer.apply(tokenized);

    const formatted = this.formatter.apply(indexed, {
      viewportHeight: meta.viewportHeight,
      ...(options.previousIdentities ? { previousIdentities: options.previousIdentities } : {}),
      // When a dialog is open, its fields outrank the mailbox behind it.
      focusBox: meta.focusBox,
    });
    report.budgetDropped = formatted.budgetDropped;
    report.shown = formatted.elements.length;

    // Trim the geometry map to what was actually listed. An index the model was never shown
    // must not be dispatchable — otherwise a hallucinated number could land on a real
    // element by coincidence.
    const shown = new Set(formatted.elements.map((e) => e.index));
    const trimmed = new Map<number, Point>();
    for (const [index, point] of geometry) if (shown.has(index)) trimmed.set(index, point);

    // Mint the identifier tokens in a FIXED order, before the literal that uses them.
    //
    // The order is observable: the vault numbers tokens sequentially, so minting the thread
    // before the context yields `T1`/`T2` and the reverse yields `T2`/`T1`. Relying on
    // object-literal evaluation order made this side disagree with the Python funnel, which
    // computes the thread token first — a difference nothing detects until two surfaces
    // produce different observations for the same page. See `fixtures/funnel/`.
    const threadToken = meta.threadRef
      ? this.tokenizer.tokenizeIdentifier(meta.threadRef)
      : null;
    const contextId = this.tokenizer.tokenizeIdentifier(meta.contextRef);

    const observation: Observation = {
      protocolVersion: PROTOCOL_VERSION,
      contextId,
      title: this.tokenizer.tokenize(meta.title),
      viewport: {
        width: meta.viewportWidth,
        height: meta.viewportHeight,
        scrollX: meta.scrollX,
        scrollY: meta.scrollY,
      },
      elements: formatted.elements,
      mail: {
        view: meta.view,
        threadToken,
        unreadCount: meta.unreadCount,
        composeOpen: meta.composeOpen,
      },
      screenshotRef: options.screenshotRef ?? null,
      changed: options.changed ?? null,
      droppedCount: reachableButUnlisted(report),
      hint: scrollHint(report),
    };

    return { observation, geometry: trimmed, report };
  }

  /**
   * Teach the vault every structured name on the page, before anything is pruned.
   *
   * A pass of its own because a sender's display name met on row 40 must already be known
   * when row 1 is tokenized — otherwise one person appears as a token in one row and as
   * plain text in another, and the model reasons about them as two people.
   */
  private registerPeople(elements: RawElement[]): void {
    for (const element of elements) {
      if (PERSON_ROLES.has(element.role) && element.name.trim()) {
        this.tokenizer.registerPerson(element.name);
      }
    }
  }

  /** Rewrite every name and value through the vault. */
  private tokenize(elements: RawElement[]): RawElement[] {
    return elements.map((element) => {
      // A sender or recipient chip is a real correspondent in THIS mailbox, so an address
      // there is somewhere the agent may legitimately write. An address in a subject line
      // or a message body is content a stranger controls: tokenized all the same, but never
      // a valid target.
      const addressable = PERSON_ROLES.has(element.role);
      return {
        ...element,
        name: this.tokenizer.tokenize(element.name, { addressable }),
        value: element.value ? this.tokenizer.tokenize(element.value, { addressable }) : element.value,
      };
    });
  }
}

export type { WireElement };
