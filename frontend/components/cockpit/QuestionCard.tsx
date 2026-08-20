"use client";

import { useState } from "react";
import type { PendingQuestion } from "@/lib/types";

/**
 * The AskUser interrupt — the 100%-context rule, made visible.
 *
 * Deliberately the only amber thing on screen while it is open. The run is genuinely
 * paused (checkpointed server-side, not a coroutine parked in memory), so this card is not
 * a modal to dismiss — it is the run, waiting.
 *
 * Self-contained with a narrow prop interface so a `shadcn/ui` Dialog can replace the
 * markup without touching the behaviour.
 */
export function QuestionCard({
  question,
  onAnswer,
}: {
  question: PendingQuestion;
  onAnswer: (text: string) => void;
}) {
  const [text, setText] = useState("");

  const submit = (formEvent: React.FormEvent) => {
    formEvent.preventDefault();
    const trimmed = text.trim();
    if (trimmed) {
      onAnswer(trimmed);
      setText("");
    }
  };

  return (
    <form
      onSubmit={submit}
      className="rise mt-4 rounded-[--radius-card] border border-pending/40 bg-pending/[0.05] p-4"
    >
      <div className="flex items-center gap-2">
        <span className="font-mono text-[10px] uppercase tracking-wider text-pending">
          waiting on you
        </span>
        {question.missing.length > 0 && (
          <span className="font-mono text-[10px] text-pending/60">
            {question.missing.map((slot) => slot.replace(/_/g, " ")).join(" · ")}
          </span>
        )}
      </div>

      <p className="mt-2 text-[14px] leading-relaxed text-text">{question.question}</p>

      <div className="mt-3 flex gap-2">
        <input
          autoFocus
          value={text}
          onChange={(changeEvent) => setText(changeEvent.target.value)}
          placeholder="Type your answer…"
          className="transition-smooth min-w-0 flex-1 rounded-[--radius-control] border border-line bg-ink px-3 py-2 text-[13.5px] text-text outline-none placeholder:text-faint focus:border-line2"
        />
        <button
          type="submit"
          disabled={!text.trim()}
          className="transition-smooth shrink-0 rounded-[--radius-control] border border-pending/50 px-4 py-2 text-[13px] font-medium text-pending hover:bg-pending/10 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Answer
        </button>
      </div>
    </form>
  );
}
