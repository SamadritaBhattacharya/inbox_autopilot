"use client";

import { useState } from "react";
import type { PendingOptions } from "@/lib/types";

/**
 * Self-heal, as a human sees it: what went wrong, and four things to do about it.
 *
 * The diagnosis leads, in plain language — "a dialog is covering the button", never
 * `STUCK`. An error code tells you a run ended; only a cause tells you what to pick next,
 * and this card is the whole of what the person has to go on.
 *
 * The evidence sits underneath, quieter. Someone who disagrees with the diagnosis can see
 * what it was based on rather than being told a conclusion — which matters because the
 * classifier is a heuristic and will sometimes be wrong.
 *
 * Option 4 always exists. A curated registry cannot anticipate everything, and admitting
 * that is more honest than a menu that pretends to be exhaustive.
 */
export function OptionsCard({
  options,
  onChoose,
}: {
  options: PendingOptions;
  onChoose: (option: number, text?: string) => void;
}) {
  const [writing, setWriting] = useState(false);
  const [text, setText] = useState("");

  const submit = (formEvent: React.FormEvent) => {
    formEvent.preventDefault();
    const trimmed = text.trim();
    if (trimmed) {
      onChoose(options.choices.length, trimmed);
      setText("");
      setWriting(false);
    }
  };

  return (
    <section
      aria-label="Recovery options"
      className="rise mt-4 overflow-hidden rounded-[--radius-card] border border-line2 bg-raised/60"
    >
      <header className="px-4 pt-3.5">
        <span className="font-mono text-[10px] uppercase tracking-wider text-faint">
          couldn&apos;t finish
        </span>
        <p className="mt-1.5 text-[14px] leading-relaxed text-text">{options.plain}</p>
        {options.evidence && (
          <p className="mt-1 font-mono text-[11px] leading-relaxed text-faint">
            {options.evidence}
          </p>
        )}
      </header>

      {writing ? (
        <form onSubmit={submit} className="flex gap-2 p-4">
          <input
            autoFocus
            value={text}
            onChange={(changeEvent) => setText(changeEvent.target.value)}
            placeholder="What should I do instead?"
            className="transition-smooth min-w-0 flex-1 rounded-[--radius-control] border border-line bg-ink px-3 py-2 text-[13px] text-text outline-none placeholder:text-faint focus:border-line2"
          />
          <button
            type="submit"
            disabled={!text.trim()}
            className="transition-smooth shrink-0 rounded-[--radius-control] border border-line2 px-3.5 py-2 text-[13px] text-text hover:bg-raised2 disabled:opacity-40"
          >
            Do that
          </button>
          <button
            type="button"
            onClick={() => setWriting(false)}
            className="transition-smooth shrink-0 px-2 py-2 text-[13px] text-faint hover:text-muted"
          >
            Back
          </button>
        </form>
      ) : (
        <ol className="space-y-1.5 p-4">
          {options.choices.map((choice) => (
            <li key={choice.n}>
              <button
                type="button"
                onClick={() =>
                  choice.freeform ? setWriting(true) : onChoose(choice.n)
                }
                className={`transition-smooth group flex w-full items-start gap-3 rounded-[--radius-control] border px-3.5 py-2.5 text-left ${
                  choice.recommended
                    ? "border-good/40 bg-good/[0.05] hover:bg-good/10"
                    : "border-line bg-surface hover:border-line2 hover:bg-raised"
                }`}
              >
                <span
                  className={`mt-px font-mono text-[11px] ${
                    choice.recommended ? "text-good" : "text-faint"
                  }`}
                >
                  {choice.n}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="text-[13.5px] text-text">{choice.label}</span>
                    {choice.recommended && (
                      <span className="rounded-full border border-good/40 px-1.5 py-px font-mono text-[9.5px] uppercase tracking-wider text-good">
                        recommended
                      </span>
                    )}
                  </span>
                  <span className="mt-0.5 block text-[12px] leading-relaxed text-faint">
                    {choice.detail}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
