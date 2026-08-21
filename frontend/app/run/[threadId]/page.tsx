import { CockpitClient } from "@/components/cockpit/CockpitClient";
import { SignInGate } from "@/components/SignInGate";

/**
 * One run, at its own URL.
 *
 * A Server Component shell around a single client island. The URL is the run: it can be
 * shared, bookmarked, and reloaded, and a reload **reattaches** rather than restarting —
 * the backend keeps the run alive and replays what happened. That is the whole payoff of
 * per-run routing, and the reason a durable interrupt is worth having.
 *
 * `params` and `searchParams` are async in Next 16 — reading them synchronously no longer
 * works, and the failure is a build error rather than a subtle bug.
 */
export default async function RunPage({ params, searchParams }: PageProps<"/run/[threadId]">) {
  const { threadId } = await params;
  const query = await searchParams;
  const task = typeof query.task === "string" ? query.task : undefined;

  // The gate wraps the run too, not only the landing page: a run URL is shareable, and an
  // unauthenticated visitor opening one must meet a sign-in rather than a dead socket.
  return (
    <SignInGate>
      <CockpitClient threadId={threadId} task={task} />
    </SignInGate>
  );
}
