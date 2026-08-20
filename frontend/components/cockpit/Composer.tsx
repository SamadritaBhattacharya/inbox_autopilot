"use client";

import { useState } from "react";
import type { RunStatus } from "@/lib/types";

/**
 * Mid-run steering.
 *
 * Available *while the agent works*, not only between runs — that is the difference
 * between feedback and a post-mortem. What you type is recorded and reaches the model on
 * its next turn, and the transcript acknowledges it so you can see it landed rather than
 * guess.
 */
export function Composer({
  status,
  onFeedback,
  onStop,
}: {
  status: RunStatus;
  onFeedback: (text: string) => void;
  onStop: () => void;
}) {
  const [text, setText] = useState("");
  const live = status === "running" || status === "starting" || status === "awaiting";

  const submit = (formEvent: React.FormEvent) => {
    formEvent.preventDefault();
    const trimmed = text.trim();
    if (trimmed) {
      onFeedback(trimmed);
      setText("");
    }
  };

  return (
    <form onSubmit={submit} className="flex gap-2">
      <input
        value={text}
        onChange={(changeEvent) => setText(changeEvent.target.value)}
        placeholder={live ? "Steer it — “skip the newsletters”" : "Run finished"}
        disabled={!live}
        className="transition-smooth min-w-0 flex-1 rounded-[--radius-control] border border-line bg-surface px-3 py-2 text-[13.5px] text-text outline-none placeholder:text-faint focus:border-line2 disabled:opacity-50"
      />
      <button
        type="submit"
        disabled={!live || !text.trim()}
        className="transition-smooth shrink-0 rounded-[--radius-control] border border-line px-3.5 py-2 text-[13px] text-muted hover:border-line2 hover:text-text disabled:cursor-not-allowed disabled:opacity-40"
      >
        Send
      </button>
      {live && (
        <button
          type="button"
          onClick={onStop}
          className="transition-smooth shrink-0 rounded-[--radius-control] border border-line px-3.5 py-2 text-[13px] text-muted hover:border-danger/50 hover:text-danger"
        >
          Stop
        </button>
      )}
    </form>
  );
}
