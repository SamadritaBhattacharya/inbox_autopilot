import type { Entry } from "@/lib/types";

/**
 * The left pane: what the agent understood, planned, thought, and did.
 *
 * Monochrome throughout. Colour appears in exactly three places — a failure, a pending
 * human decision, a verified success — and against a grey field those become impossible to
 * skim past. That is the property an approval gate needs, so nothing decorative is allowed
 * to spend it.
 */

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rise flex gap-3 py-2.5">
      <span className="w-[4.5rem] shrink-0 pt-0.5 text-right font-mono text-[10px] uppercase tracking-wider text-faint">
        {label}
      </span>
      <div className="min-w-0 flex-1 text-[13.5px] leading-relaxed">{children}</div>
    </div>
  );
}

function Chip({ children, tone = "grey" }: { children: React.ReactNode; tone?: "grey" | "good" }) {
  const tones = {
    grey: "border-line text-muted",
    good: "border-good/40 text-good",
  } as const;
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 font-mono text-[10.5px] ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

function EntryRow({ entry }: { entry: Entry }) {
  switch (entry.kind) {
    case "task":
      return (
        <div className="rise mb-4 rounded-[--radius-card] border border-line bg-raised px-4 py-3">
          <div className="font-mono text-[10px] uppercase tracking-wider text-faint">You asked</div>
          <p className="mt-1.5 text-[14px] leading-relaxed text-text">{entry.text}</p>
        </div>
      );

    case "intent":
      return (
        <Row label="understood">
          <span className="text-text">{entry.action.replace(/_/g, " ")}</span>
          {Object.entries(entry.slots).length > 0 && (
            <span className="text-faint">
              {" · "}
              {Object.entries(entry.slots)
                .map(([key, value]) => `${key.replace(/_/g, " ")}: ${value}`)
                .join(" · ")}
            </span>
          )}
        </Row>
      );

    case "route":
      return (
        <Row label="route">
          <Chip>{entry.route}</Chip>
          {entry.ruleMatched && (
            <span className="ml-2 text-faint">rule matched — no model call needed</span>
          )}
        </Row>
      );

    case "plan":
      return (
        <Row label="plan">
          <ol className="space-y-1">
            {entry.steps.map((step, index) => (
              <li key={`${step}-${index}`} className="flex gap-2 text-muted">
                <span className="font-mono text-faint">{index + 1}.</span>
                <span>{step}</span>
              </li>
            ))}
          </ol>
        </Row>
      );

    case "status":
      return <Row label="worker"><span className="text-faint">{entry.message}</span></Row>;

    case "observation":
      return (
        <Row label="sees">
          <span className="text-faint">
            {entry.elements} element{entry.elements === 1 ? "" : "s"} · {entry.view}
            {entry.dropped > 0 && (
              // Never hidden: an agent that believes it saw everything concludes a message
              // does not exist, and the user deserves to know the list was cut.
              <span className="text-muted"> · {entry.dropped} more below</span>
            )}
          </span>
        </Row>
      );

    case "assessment":
      return (
        <Row label="checks">
          <span className={entry.outcome === "no_effect" ? "text-pending" : "text-muted"}>
            {entry.text}
          </span>
          {entry.outcome === "no_effect" && (
            <span className="ml-2 font-mono text-[10.5px] text-pending">nothing moved</span>
          )}
        </Row>
      );

    case "reasoning":
      return <Row label="thinks"><p className="whitespace-pre-wrap text-muted">{entry.text}</p></Row>;

    case "action": {
      const args = Object.entries(entry.args)
        .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
        .join(", ");
      return (
        <Row label="does">
          <code className="font-mono text-[12.5px] text-text">
            {entry.name}
            <span className="text-faint">({args})</span>
          </code>
        </Row>
      );
    }

    case "result":
      return (
        <Row label="→">
          <span className={entry.success ? "text-faint" : "text-danger"}>
            {entry.reason}
            {entry.errorCode && (
              <span className="ml-2 font-mono text-[10.5px]">{entry.errorCode}</span>
            )}
          </span>
        </Row>
      );

    case "decision":
      return (
        <Row label="you">
          <span className={entry.verdict === "approve" ? "text-good" : "text-muted"}>
            {entry.verdict === "approve"
              ? "approved it"
              : entry.verdict === "edit"
                ? "asked for a change"
                : "declined"}
          </span>
        </Row>
      );

    case "diagnosis":
      return (
        <Row label="why">
          <span className="text-text">{entry.plain}</span>
          {entry.evidence && (
            <span className="mt-0.5 block font-mono text-[11px] text-faint">{entry.evidence}</span>
          )}
        </Row>
      );

    case "feedback":
      return (
        <Row label="you said">
          <span className="text-text">{entry.text}</span>
          <span className="ml-2 text-faint">— noted, applying next turn</span>
        </Row>
      );

    case "error":
      return (
        <div className="rise my-2 rounded-[--radius-control] border border-danger/40 bg-danger/[0.06] px-3 py-2">
          <span className="text-[13px] text-danger">{entry.message}</span>
          {entry.errorCode && (
            <span className="ml-2 font-mono text-[10.5px] text-danger/70">{entry.errorCode}</span>
          )}
        </div>
      );

    case "finalize":
      return (
        <div
          className={`rise mt-4 rounded-[--radius-card] border px-4 py-3 ${
            entry.success ? "border-good/40 bg-good/[0.05]" : "border-danger/40 bg-danger/[0.05]"
          }`}
        >
          <div className="flex items-center gap-2">
            <span
              className={`font-mono text-[10px] uppercase tracking-wider ${
                entry.success ? "text-good" : "text-danger"
              }`}
            >
              {entry.success ? "done" : "stopped"}
            </span>
            {entry.errorCode && (
              <Chip>{entry.errorCode}</Chip>
            )}
          </div>
          <p className="mt-1.5 text-[13.5px] leading-relaxed text-text">{entry.reason}</p>
        </div>
      );

    default:
      return null;
  }
}

export function Transcript({ timeline }: { timeline: Entry[] }) {
  return (
    <div className="divide-y divide-line/60">
      {timeline.map((entry, index) => (
        <EntryRow key={index} entry={entry} />
      ))}
    </div>
  );
}
