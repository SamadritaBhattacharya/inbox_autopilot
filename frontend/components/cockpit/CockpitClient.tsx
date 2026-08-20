"use client";

import Link from "next/link";
import { useEffect, useRef } from "react";
import { useAgentRun } from "@/lib/useAgentRun";
import { ApprovalCard } from "./ApprovalCard";
import { Composer } from "./Composer";
import { OptionsCard } from "./OptionsCard";
import { QuestionCard } from "./QuestionCard";
import { Transcript } from "./Transcript";
import { Viewport } from "./Viewport";

/**
 * The one `"use client"` root. Everything live hangs beneath it.
 *
 * Keeping the boundary here is what stops the whole app becoming client-rendered — the
 * shell, the landing page, and (later) run history stay Server Components, which is the
 * reason this is Next.js rather than a plain SPA. If a second client root appears, that
 * benefit is quietly gone.
 */
export function CockpitClient({ threadId, task }: { threadId: string; task?: string }) {
  const {
    timeline,
    question,
    approval,
    usage,
    status,
    connected,
    absent,
    answer,
    decide,
    choose,
    options,
    feedback,
    stop,
    subscribeFrame,
  } = useAgentRun(threadId, task);

  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [timeline, question, approval, options]);

  const lastAction = [...timeline].reverse().find((entry) => entry.kind === "action");
  const currentAction = lastAction?.kind === "action" ? lastAction.name : undefined;

  if (absent) {
    return (
      <main className="mx-auto flex w-full max-w-md flex-1 flex-col items-center justify-center gap-4 px-6 text-center">
        <h1 className="text-lg font-medium text-text">That run has finished and been cleared</h1>
        <p className="text-[13.5px] leading-relaxed text-muted">
          Runs stay available for a while after they end, then are released.
        </p>
        <Link
          href="/"
          className="transition-smooth rounded-[--radius-control] border border-line px-4 py-2 text-[13px] text-muted hover:border-line2 hover:text-text"
        >
          Start a new one
        </Link>
      </main>
    );
  }

  return (
    <main className="flex min-h-0 flex-1 flex-col gap-3 p-3 lg:flex-row lg:gap-4 lg:p-4">
      {/* Live browser: on top on mobile, the hero on the right at desktop widths. */}
      <section className="h-[38vh] min-h-0 min-w-0 shrink-0 lg:order-2 lg:h-auto lg:flex-1">
        <Viewport subscribeFrame={subscribeFrame} status={status} action={currentAction} />
      </section>

      {/* Conversation rail: header → transcript → composer, one continuous column. */}
      <section className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[--radius-card] border border-line bg-surface/40 lg:order-1 lg:w-[28rem] lg:flex-none">
        <header className="flex shrink-0 items-center gap-2 border-b border-line px-4 py-2.5">
          <Link href="/" className="transition-smooth text-[13px] font-medium text-text hover:text-muted">
            Inbox Autopilot
          </Link>
          <span className="ml-auto flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-faint">
            <i
              className={`h-1.5 w-1.5 rounded-full ${
                status === "awaiting"
                  ? "bg-pending"
                  : status === "failed"
                    ? "bg-danger"
                    : status === "done"
                      ? "bg-good"
                      : connected
                        ? "animate-pulse bg-muted"
                        : "bg-line2"
              }`}
            />
            {connected ? status : "offline"}
          </span>
        </header>

        <div className="scroll-area min-h-0 flex-1 overflow-y-auto px-4 pt-3">
          <Transcript timeline={timeline} />
          {question && <QuestionCard question={question} onAnswer={answer} />}
          {approval && <ApprovalCard approval={approval} onDecide={decide} />}
          {options && <OptionsCard options={options} onChoose={choose} />}
          <div ref={bottomRef} className="h-3" />
        </div>

        <footer className="shrink-0 space-y-2 border-t border-line bg-ink/40 px-4 pb-3 pt-2.5">
          <Composer status={status} onFeedback={feedback} onStop={stop} />
          {usage.calls > 0 && (
            <div className="flex gap-3 font-mono text-[10px] text-faint">
              <span>{usage.calls} model calls</span>
              <span>{usage.inputTokens.toLocaleString()} in</span>
              <span>{usage.outputTokens.toLocaleString()} out</span>
              {usage.cachedTokens > 0 && <span>{usage.cachedTokens.toLocaleString()} cached</span>}
            </div>
          )}
        </footer>
      </section>
    </main>
  );
}
