/**
 * Turning a connection state into something a person can act on.
 *
 * Its own module because it is pure, and because "offline" versus "not paired" is exactly
 * the distinction users get wrong — one is waiting, the other needs them to do something.
 */
export type BridgeStatus =
  | { state: "connecting" }
  | { state: "connected" }
  | { state: "rejected"; reason: string }
  | { state: "offline"; retryInMs: number };

export function describeStatus(
  status: BridgeStatus,
  running: boolean,
  account = "",
): string {
  if (status.state === "connected" && account) {
    return running ? `${account} · a run is in progress` : `${account} · idle`;
  }
  return describeState(status, running);
}

function describeState(status: BridgeStatus, running: boolean): string {
  switch (status.state) {
    case "connected":
      return running ? "connected · a run is in progress" : "connected · idle";
    case "connecting":
      return "connecting…";
    case "rejected":
      // Never retried automatically, so the text has to say what to do instead of implying
      // it will sort itself out.
      return status.reason;
    case "offline": {
      const seconds = Math.max(1, Math.round(status.retryInMs / 1000));
      return `backend unreachable · retrying in ${seconds}s`;
    }
  }
}
