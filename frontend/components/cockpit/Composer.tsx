"use client";

import { useState } from "react";
import { useStartRun } from "@/lib/startRun";
import type { RunStatus } from "@/lib/types";

/**
 * The one input, in two modes.
 *
 * **While the agent works** it is mid-run steering — what you type is recorded and reaches
 * the model on its next turn, and the transcript acknowledges it so you can see it landed.
 * Available *during* the run, not only between runs: that is the difference between
 * feedback and a post-mortem.
 *
 * **Once the run ends** — finished, failed, or stopped by you — the same box starts the
 * next task instead. Disabling it there was a dead end: stopping a run is usually the
 * prelude to asking for something else, and the only way out was to notice the box was
 * inert and navigate home by hand.
 *
 * It starts a *new run at a new URL* rather than reusing this one. The thread id is the
 * checkpointer key, so feeding a second task to the same id would resume the finished run
 * rather than begin anything. See `useStartRun`.
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
  const startRun = useStartRun();
  const live = status === "running" || status === "starting" || status === "awaiting";

  const submit = (formEvent: React.FormEvent) => {
    formEvent.preventDefault();
    const trimmed = text.trim();
    if (!trimmed) return;
    if (live) onFeedback(trimmed);
    else startRun(trimmed);
    setText("");
  };

  return (
    <form onSubmit={submit} className="flex gap-2">
      <input
        value={text}
        onChange={(changeEvent) => setText(changeEvent.target.value)}
        placeholder={live ? "Steer it — “skip the newsletters”" : "Ask for something else…"}
        className="transition-smooth min-w-0 flex-1 rounded-[--radius-control] border border-line bg-surface px-3 py-2 text-[13.5px] text-text outline-none placeholder:text-faint focus:border-line2"
      />
      <button
        type="submit"
        disabled={!text.trim()}
        className="transition-smooth shrink-0 rounded-[--radius-control] border border-line px-3.5 py-2 text-[13px] text-muted hover:border-line2 hover:text-text disabled:cursor-not-allowed disabled:opacity-40"
      >
        {live ? "Send" : "Run"}
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
