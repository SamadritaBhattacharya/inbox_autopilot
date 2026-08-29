"use client";

import { useMemo, useState } from "react";
import { changeSummary, diffLines, toHunks } from "@/lib/diff";
import type { PendingApproval } from "@/lib/types";

/**
 * The approval gate, as a human sees it.
 *
 * The most consequential control in the product, so it is designed to be **read, not
 * clicked past**:
 *
 * - The draft is shown in full, monospaced, with the REAL recipient. Verifying who this is
 *   going to is the entire point; "send to P17" would be unverifiable.
 * - Approve is not the visually loudest thing. A card that makes the irreversible action
 *   the most attractive target trains people to hit it reflexively, and then the gate is
 *   theatre that also wastes a second of their time.
 * - Rejecting and editing are equally available, because "not quite" is the common case
 *   and forcing it through a reject-and-restart makes people approve bad drafts instead.
 *
 * Self-contained with a narrow prop interface, so a `shadcn/ui` Dialog can replace the
 * markup without touching any of this behaviour.
 */

const TONE = {
  send: { label: "about to send", verb: "Send it" },
  invite: { label: "about to send an invite", verb: "Send invite" },
  delete: { label: "about to delete permanently", verb: "Delete forever" },
  bulk: { label: "about to make a bulk change", verb: "Go ahead" },
} as const;

export function ApprovalCard({
  approval,
  onDecide,
}: {
  approval: PendingApproval;
  onDecide: (
    verdict: "approve" | "edit" | "reject",
    edit?: string,
    editedPreview?: string,
  ) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [edit, setEdit] = useState("");
  // The draft, as the human may retype it.
  //
  // Seeded once, because the caller mounts a NEW card per ask (`key`) rather than reusing
  // this one. It used to be re-seeded from an effect, which is a cascading render for
  // something a remount does for free — and, more to the point, an effect can only reset
  // what it is told changed. Identity belongs to the ask.
  const [draft, setDraft] = useState(approval.preview);
  const [showDiff, setShowDiff] = useState(true);
  const tone = TONE[approval.kind as keyof typeof TONE] ?? TONE.bulk;
  const destructive = approval.kind === "delete";

  // Against what the human was shown LAST time, not against what they are typing now —
  // this answers "what did my last correction actually do", which is the question the
  // second card leaves you with. Memoized because it is O(n × m) over the two versions.
  const rows = useMemo(
    () => diffLines(approval.previousPreview, approval.preview),
    [approval.previousPreview, approval.preview],
  );
  const hunks = useMemo(() => (rows ? toHunks(rows) : null), [rows]);
  const changed = useMemo(() => changeSummary(rows ?? []), [rows]);

  const rewritten = draft.trim() !== approval.preview.trim();

  const applyEdit = () => {
    const instruction = edit.trim();
    // Either is enough on its own. Retyping the draft is the precise way to change one
    // sentence; an instruction is the fast way to ask for something open-ended.
    if (!instruction && !rewritten) return;
    onDecide("edit", instruction, rewritten ? draft : "");
    setEdit("");
    setEditing(false);
  };

  const submitEdit = (formEvent: React.FormEvent) => {
    formEvent.preventDefault();
    applyEdit();
  };

  // Approving a draft the human has edited but not yet applied would send the ORIGINAL
  // text — their edits silently discarded at the one moment that cannot be undone. Route
  // it through the edit path instead, so what they see is what goes.
  const approve = () => (rewritten ? applyEdit() : onDecide("approve"));

  return (
    <section
      aria-label="Approval required"
      className={`rise mt-4 overflow-hidden rounded-[--radius-card] border ${
        destructive ? "border-danger/45 bg-danger/[0.05]" : "border-pending/45 bg-pending/[0.05]"
      }`}
    >
      <header className="flex items-center gap-2 px-4 pt-3.5">
        <span
          className={`font-mono text-[10px] uppercase tracking-wider ${
            destructive ? "text-danger" : "text-pending"
          }`}
        >
          {tone.label}
        </span>
        <span className="ml-auto font-mono text-[10px] text-faint">needs your approval</span>
      </header>

      <p className="px-4 pt-1.5 text-[14px] font-medium text-text">{approval.summary}</p>

      {/* What moved since the last time this was asked.
        *
        * An edit brings back the whole email, and finding your own correction in it meant
        * re-reading all of it. Nothing here is applied — the full text is editable below;
        * this is a reading aid, shown open because seeing the change is the point. */}
      {hunks && (
        <div className="mx-4 mt-3 overflow-hidden rounded-[--radius-control] border border-line">
          <button
            type="button"
            onClick={() => setShowDiff(!showDiff)}
            className="transition-smooth flex w-full items-center gap-2 px-3 py-1.5 text-left font-mono text-[10px] text-faint hover:text-muted"
            aria-expanded={showDiff}
          >
            <span>what changed</span>
            {changed.added > 0 && <span className="text-good">+{changed.added}</span>}
            {changed.removed > 0 && <span className="text-danger">−{changed.removed}</span>}
            <span className="ml-auto">{showDiff ? "hide" : "show"}</span>
          </button>
          {showDiff && (
            <div className="scroll-area max-h-48 overflow-auto border-t border-line bg-ink px-3 py-2 font-mono text-[11px] leading-relaxed">
              {hunks.map((row, position) =>
                row === null ? (
                  <div key={position} className="select-none text-faint">
                    ⋯
                  </div>
                ) : (
                  <div
                    key={position}
                    className={
                      row.kind === "add"
                        ? "text-good"
                        : row.kind === "remove"
                          ? "text-danger line-through decoration-danger/40"
                          : "text-faint"
                    }
                  >
                    <span className="select-none opacity-60">
                      {row.kind === "add" ? "+ " : row.kind === "remove" ? "− " : "  "}
                    </span>
                    {row.text || " "}
                  </div>
                ),
              )}
            </div>
          )}
        </div>
      )}

      {/* The draft, verbatim and resolved — and editable in place.
        *
        * A textarea rather than a `<pre>` because the fastest way to fix one sentence is to
        * fix that sentence. Describing an edit in words meant a model re-derived the whole
        * message from the description, and "the greeting appears twice" came back as an
        * email with the greeting deleted and the sign-off reworded. Text typed here is
        * applied byte for byte; nothing the human did not touch can change.
        */}
      <label className="sr-only" htmlFor="approval-draft">
        The message, editable
      </label>
      <textarea
        id="approval-draft"
        value={draft}
        onChange={(changeEvent) => setDraft(changeEvent.target.value)}
        spellCheck={false}
        rows={Math.min(14, Math.max(6, draft.split("\n").length + 1))}
        className="scroll-area transition-smooth mx-4 mt-3 block w-[calc(100%-2rem)] resize-y overflow-auto whitespace-pre-wrap rounded-[--radius-control] border border-line bg-ink px-3 py-2.5 font-mono text-[12px] leading-relaxed text-muted outline-none focus:border-line2 focus:text-text"
      />
      {rewritten && (
        <p className="px-4 pt-1.5 font-mono text-[10px] text-pending">
          edited — your text is applied exactly as written, then shown again to confirm
        </p>
      )}

      {editing ? (
        <form onSubmit={submitEdit} className="flex gap-2 p-4">
          <input
            autoFocus
            value={edit}
            onChange={(changeEvent) => setEdit(changeEvent.target.value)}
            placeholder={rewritten ? "Anything else? (optional)" : "What should change?"}
            className="transition-smooth min-w-0 flex-1 rounded-[--radius-control] border border-line bg-ink px-3 py-2 text-[13px] text-text outline-none placeholder:text-faint focus:border-line2"
          />
          <button
            type="submit"
            disabled={!edit.trim() && !rewritten}
            className="transition-smooth shrink-0 rounded-[--radius-control] border border-line2 px-3.5 py-2 text-[13px] text-text hover:bg-raised disabled:opacity-40"
          >
            Revise
          </button>
          <button
            type="button"
            onClick={() => setEditing(false)}
            className="transition-smooth shrink-0 rounded-[--radius-control] px-2 py-2 text-[13px] text-faint hover:text-muted"
          >
            Cancel
          </button>
        </form>
      ) : (
        <div className="flex flex-wrap gap-2 p-4">
          <button
            type="button"
            onClick={approve}
            className={`transition-smooth rounded-[--radius-control] border px-4 py-2 text-[13px] font-medium ${
              destructive
                ? "border-danger/50 text-danger hover:bg-danger/10"
                : "border-good/50 text-good hover:bg-good/10"
            }`}
          >
            {rewritten ? "Apply & review" : tone.verb}
          </button>
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="transition-smooth rounded-[--radius-control] border border-line px-4 py-2 text-[13px] text-muted hover:border-line2 hover:text-text"
          >
            {/* Two buttons that did the same thing was the confusing part. When the draft
              * has been retyped, THIS one adds words on top of it ("and make it warmer");
              * the primary applies the text alone. Labelling both "Revise" made the
              * primary look like the one that did nothing. */}
            {rewritten ? "Add an instruction" : "Change something"}
          </button>
          <button
            type="button"
            onClick={() => onDecide("reject")}
            className="transition-smooth rounded-[--radius-control] border border-line px-4 py-2 text-[13px] text-muted hover:border-line2 hover:text-text"
          >
            Don&apos;t
          </button>
        </div>
      )}
    </section>
  );
}
