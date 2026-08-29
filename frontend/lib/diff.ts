/**
 * A line diff between two versions of the same draft.
 *
 * **Why this is in the cockpit and not the backend.** The renderer already holds every
 * version — each `approval_request` event carries its own preview — so the previous and the
 * current draft are both in hand at render time. Computing the difference is a rendering
 * concern in exactly the way a virtual DOM's is: same data, two versions, show what moved.
 * Sending it over the wire would mean the backend keeping a copy of what a human was last
 * shown, which is state we would then have to persist, redact and reason about.
 *
 * **What it is for.** After an edit the gate comes back with the whole email again, and the
 * only way to find your own correction was to re-read all of it. Nothing is applied from
 * this — it is a reading aid over text the human can already see in full underneath.
 */

export type DiffRow = { kind: "same" | "add" | "remove"; text: string };

/**
 * Above this, the diff is skipped entirely.
 *
 * The algorithm is O(n × m), so it is the product that matters, not the line count. 400 × 400
 * is 160k cells — a fraction of a frame. An email that size is not one anybody is proofreading
 * in a textarea anyway, so refusing beats janking the card.
 */
const MAX_LINES = 400;

/** Normalize the things that differ without anybody having changed anything. */
function toLines(text: string): string[] {
  return text.replace(/\r\n/g, "\n").split("\n");
}

/**
 * The classic LCS table, flattened.
 *
 * A typed array rather than nested arrays: one allocation instead of n, and the whole table
 * for a long email fits in a few hundred KB.
 */
function lcsTable(before: string[], after: string[]): Int32Array {
  const width = after.length + 1;
  const table = new Int32Array((before.length + 1) * width);

  for (let i = before.length - 1; i >= 0; i -= 1) {
    for (let j = after.length - 1; j >= 0; j -= 1) {
      table[i * width + j] =
        before[i] === after[j]
          ? table[(i + 1) * width + (j + 1)] + 1
          : Math.max(table[(i + 1) * width + j], table[i * width + (j + 1)]);
    }
  }
  return table;
}

/**
 * Line-level difference between two drafts.
 *
 * Returns `null` — not an empty list — when there is nothing meaningful to show: no previous
 * version, an identical one, or a pair too large to diff. `null` means "render no diff at
 * all", which is different from "diffed, and nothing changed".
 */
export function diffLines(before: string, after: string): DiffRow[] | null {
  if (!before || !after) return null;
  if (before === after) return null;

  const a = toLines(before);
  const b = toLines(after);
  if (a.length > MAX_LINES || b.length > MAX_LINES) return null;

  const width = b.length + 1;
  const table = lcsTable(a, b);
  const rows: DiffRow[] = [];

  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      rows.push({ kind: "same", text: a[i] });
      i += 1;
      j += 1;
    } else if (table[(i + 1) * width + j] >= table[i * width + (j + 1)]) {
      rows.push({ kind: "remove", text: a[i] });
      i += 1;
    } else {
      rows.push({ kind: "add", text: b[j] });
      j += 1;
    }
  }
  // A removal and an addition of the same line cannot both be left over, because the loop
  // above only exits when one side is exhausted.
  while (i < a.length) rows.push({ kind: "remove", text: a[i++] });
  while (j < b.length) rows.push({ kind: "add", text: b[j++] });

  return rows.some((row) => row.kind !== "same") ? rows : null;
}

/** How much moved, for the one-line header. */
export function changeSummary(rows: DiffRow[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const row of rows) {
    if (row.kind === "add") added += 1;
    else if (row.kind === "remove") removed += 1;
  }
  return { added, removed };
}

/**
 * Drop unchanged stretches, keeping `context` lines either side of every change.
 *
 * A twenty-line email with a one-word fix should show three lines, not twenty — otherwise
 * the diff is the same wall of text it was meant to replace. Elided stretches become a
 * single `null`, which the card renders as a gap marker.
 */
export function toHunks(rows: DiffRow[], context = 1): (DiffRow | null)[] {
  const keep = new Array<boolean>(rows.length).fill(false);
  rows.forEach((row, index) => {
    if (row.kind === "same") return;
    for (let n = index - context; n <= index + context; n += 1) {
      if (n >= 0 && n < rows.length) keep[n] = true;
    }
  });

  const out: (DiffRow | null)[] = [];
  let eliding = false;
  rows.forEach((row, index) => {
    if (keep[index]) {
      out.push(row);
      eliding = false;
    } else if (!eliding) {
      out.push(null);
      eliding = true;
    }
  });
  return out;
}
