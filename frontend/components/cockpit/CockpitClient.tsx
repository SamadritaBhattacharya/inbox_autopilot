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
/** Present tense, because it is happening now. */
const ACTIVITY_VERB: Record<string, string> = {
  looking: "Reading the screen",
  thinking: "Thinking",
  acting: "Acting",
  waiting: "Waiting for you",
  blind: "Running without a live view",
};

export function CockpitClient({ threadId, task }: { threadId: string; task?: string }) {
  const {
    timeline,
    question,
    approval,
    activity,
    usage,
    status,
    connected,
    absent,
    location,
    answer,
    decide,
    choose,
    options,
    feedback,
    stop,
    subscribeFrame,
  } = useAgentRun(threadId, task);

  /**
   * Auto-scroll the transcript — and ONLY the transcript.
   *
   * `scrollIntoView` was the wrong tool: it walks up and scrolls every scrollable
   * ancestor, so once the page itself could scroll it dragged the whole layout, carrying
   * the live browser view off the top of the screen on every new message. Setting
   * `scrollTop` on the container cannot touch anything above it.
   *
   * It also holds position when the user has scrolled up to read. Yanking someone back to
   * the bottom mid-sentence because a token arrived is its own bug.
   */
  const scrollRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !pinnedRef.current) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [timeline, question, approval, options, activity]);

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
    // `fixed inset-0`, not `h-screen`: this takes the cockpit out of page flow entirely,
    // so the document has nothing to scroll no matter what the body or a parent layout
    // does. A height alone is only as good as the height chain above it, and one
    // `min-h-full` anywhere in that chain brings back the bug where the live browser view
    // slides off the top as the transcript grows. Only the transcript scrolls.
    <main className="fixed inset-0 flex flex-col gap-3 overflow-hidden p-3 lg:flex-row lg:gap-4 lg:p-4">
      {/* Live browser: on top on mobile, the hero on the right at desktop widths. */}
      <section className="h-[60vh] [38vh] min-h-0 min-w-0 shrink-0 lg:order-2 lg:h-auto lg:flex-1">
        <Viewport
          subscribeFrame={subscribeFrame}
          status={status}
          action={activity?.label || currentAction}
          url={location}
        />
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

        <div
          ref={scrollRef}
          onScroll={onScroll}
          className="scroll-area min-h-0 flex-1 overflow-y-auto px-4 pt-3"
        >
          <Transcript timeline={timeline} />
          {question && <QuestionCard question={question} onAnswer={answer} />}
          {approval && <ApprovalCard approval={approval} onDecide={decide} />}
          {options && <OptionsCard options={options} onChoose={choose} />}
          {activity && !question && !approval && !options && (
            <div className="rise flex items-center gap-2.5 py-3">
              <span className="flex gap-1" aria-hidden>
                <i className="h-1.5 w-1.5 animate-pulse rounded-full bg-muted [animation-delay:0ms]" />
                <i className="h-1.5 w-1.5 animate-pulse rounded-full bg-muted [animation-delay:150ms]" />
                <i className="h-1.5 w-1.5 animate-pulse rounded-full bg-muted [animation-delay:300ms]" />
              </span>
              <span className="text-[13px] text-muted">
                {ACTIVITY_VERB[activity.phase] ?? activity.phase}
                {activity.label && <span className="text-faint"> — {activity.label}</span>}
              </span>
            </div>
          )}
          <div className="h-3" />
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
