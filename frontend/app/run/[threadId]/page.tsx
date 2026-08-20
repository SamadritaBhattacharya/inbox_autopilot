import { CockpitClient } from "@/components/cockpit/CockpitClient";

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

  return <CockpitClient threadId={threadId} task={task} />;
}
