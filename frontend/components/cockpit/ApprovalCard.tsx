"use client";

import { useEffect, useState } from "react";
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
  // The draft, as the human may retype it. Seeded from the preview and reset whenever a new
  // approval arrives — a revised draft must not be shown under the previous edit.
  const [draft, setDraft] = useState(approval.preview);
  const tone = TONE[approval.kind as keyof typeof TONE] ?? TONE.bulk;
  const destructive = approval.kind === "delete";

  useEffect(() => {
    setDraft(approval.preview);
    setEdit("");
    setEditing(false);
  }, [approval.requestId, approval.preview]);

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
          edited — Revise applies your text exactly as written
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
            {rewritten ? "Revise" : "Change something"}
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
