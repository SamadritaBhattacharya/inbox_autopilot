"use client";

import { useState } from "react";
import { useStartRun } from "@/lib/startRun";

/**
 * The landing page's way in. The navigation itself lives in `useStartRun`, shared with the
 * cockpit composer so both mint thread ids the same way.
 */
export function StartForm({ examples }: { examples: string[] }) {
  const start = useStartRun();
  const [task, setTask] = useState("");

  return (
    <div>
      <form
        onSubmit={(formEvent) => {
          formEvent.preventDefault();
          start(task);
        }}
        className="flex gap-2"
      >
        <input
          autoFocus
          value={task}
          onChange={(changeEvent) => setTask(changeEvent.target.value)}
          placeholder="Ask it anything about your mail…"
          className="transition-smooth min-w-0 flex-1 rounded-[--radius-control] border border-line bg-surface px-4 py-3 text-[14px] text-text outline-none placeholder:text-faint focus:border-line2"
        />
        <button
          type="submit"
          disabled={!task.trim()}
          className="transition-smooth shrink-0 rounded-[--radius-control] border border-line2 bg-raised px-5 py-3 text-[13.5px] font-medium text-text hover:bg-raised2 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Run
        </button>
      </form>

      <div className="mt-6">
        <div className="mb-2 font-mono text-[10px] uppercase tracking-wider text-faint">Try one</div>
        <div className="flex flex-col gap-2">
          {examples.map((example) => (
            <button
              key={example}
              type="button"
              onClick={() => start(example)}
              className="transition-smooth group flex items-start gap-3 rounded-[--radius-control] border border-line bg-surface px-4 py-2.5 text-left text-[13.5px] leading-relaxed text-muted hover:border-line2 hover:bg-raised hover:text-text"
            >
              <span className="mt-px font-mono text-faint transition-colors group-hover:text-muted">
                →
              </span>
              <span>{example}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
