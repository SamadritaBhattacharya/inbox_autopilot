"use client";

import { useRouter } from "next/navigation";
import { useCallback } from "react";

/**
 * Start a run by navigating to its URL.
 *
 * **One thread id per task, always a fresh one.** The thread id is the checkpointer key, so
 * reusing it for a second task would resume the first task's saved state instead of
 * starting anything — the new instruction would land in the middle of a finished run.
 *
 * Minting it on the client rather than the server gives the run a shareable address before
 * it has produced a single event, and keeps the cockpit's "the URL *is* the run" promise:
 * a reload reattaches instead of restarting.
 *
 * Shared by the landing form and the in-cockpit composer so the two cannot drift into
 * minting ids differently.
 */
export function useStartRun(): (task: string) => void {
  const router = useRouter();

  return useCallback(
    (task: string) => {
      const trimmed = task.trim();
      if (!trimmed) return;
      const threadId = `run-${Math.random().toString(36).slice(2, 10)}`;
      router.push(`/run/${threadId}?task=${encodeURIComponent(trimmed)}`);
    },
    [router],
  );
}
