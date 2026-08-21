/**
 * Stage 4 — fold layout wrappers into the thing that matters.
 *
 * Modern web apps nest deeply: `div > div > div > button` is ordinary, and every one of
 * those divs has a role, a box, and a name inherited from its subtree. Left alone, the model
 * sees four entries that are all "the same button" and has to guess which to click — and
 * picking a wrapper often does nothing, because the click handler is on the leaf.
 *
 * A wrapper is an element that adds no information: not interactive, and geometrically
 * indistinguishable from a single child it contains. Fold it away and keep the child.
 *
 * **Direction matters.** The child is kept, not the parent. The parent is where the layout
 * lives; the child is where the behaviour lives.
 *
 * Ported from `backend/app/observation/funnel/wrapper_collapse.py`.
 */
import { area, type RawElement } from "./raw";

/**
 * How closely a parent's box must match its child's before the parent counts as pure layout.
 * Padding of a pixel or two is still a wrapper; a parent noticeably bigger than its child is
 * doing something (a row containing a button, say) and must survive.
 */
export const GEOMETRY_TOLERANCE = 0.92;

export interface CollapseResult {
  kept: RawElement[];
  collapsed: number;
}

/** Removes non-interactive parents that merely wrap one child. */
export class WrapperCollapser {
  private readonly tolerance: number;

  constructor(options: { tolerance?: number } = {}) {
    this.tolerance = options.tolerance ?? GEOMETRY_TOLERANCE;
  }

  apply(elements: RawElement[]): CollapseResult {
    const childrenByParent = new Map<number, RawElement[]>();
    for (const element of elements) {
      if (element.parentId === null) continue;
      const siblings = childrenByParent.get(element.parentId);
      if (siblings) siblings.push(element);
      else childrenByParent.set(element.parentId, [element]);
    }

    const byId = new Map<number, RawElement>();
    for (const element of elements) byId.set(element.nodeId, element);

    const kept: RawElement[] = [];
    let collapsed = 0;

    for (const element of elements) {
      if (this.isWrapper(element, childrenByParent.get(element.nodeId) ?? [])) {
        collapsed += 1;
        continue;
      }
      if (isRedundantChild(element, byId)) {
        collapsed += 1;
        continue;
      }
      kept.push(element);
    }

    return { kept, collapsed };
  }

  private isWrapper(element: RawElement, children: RawElement[]): boolean {
    // Interactive elements are never wrappers, however plain they look — the handler may
    // well be on this node and the child a span with no behaviour at all.
    if (element.interactive) return false;

    // Exactly one child: with two or more, this element is a container that gives its
    // children meaning by grouping them, and removing it loses that grouping.
    if (children.length !== 1) return false;
    const child = children[0]!;

    // If the parent carries text the child does not, it is contributing information.
    const parentText = element.name.trim();
    if (parentText && parentText !== child.name.trim()) return false;

    const parentArea = area(element);
    const childArea = area(child);
    if (parentArea <= 0 || childArea <= 0) return false;
    return childArea / parentArea >= this.tolerance;
  }
}

/**
 * Is this inert text already readable from the clickable thing that contains it?
 *
 * A mail row is one actionable unit: click the row, and the sender and subject inside it are
 * description rather than targets. Listing all three triples the tokens the row costs and
 * asks the model to choose between numbers that do the same thing — and picking the inert
 * one does nothing, which reads to the agent as a dead click.
 *
 * Only inert children of an INTERACTIVE parent are folded away, and only when the parent's
 * name already contains their text, so nothing readable is lost.
 */
function isRedundantChild(element: RawElement, byId: Map<number, RawElement>): boolean {
  if (element.interactive || element.parentId === null) return false;
  const parent = byId.get(element.parentId);
  if (!parent || !parent.interactive) return false;

  const text = element.name.trim();
  return Boolean(text) && parent.name.includes(text);
}
