import { PROTOCOL_VERSION } from "@inbox/contracts";
import { StartForm } from "@/components/StartForm";
import { env } from "@/lib/env";

/**
 * The landing shell — a Server Component, and deliberately static.
 *
 * Everything here renders on the server and is cached; only the small form beneath is
 * interactive. Keeping the split at this granularity is what Next.js buys us over a plain
 * SPA, and it is why the cockpit is one client island rather than a client application.
 */

const EXAMPLES = [
  "Summarize what's waiting in my inbox",
  "What did Priya say about the Friday demo?",
  "Archive all the newsletters",
  "Draft a reply to the latest thread saying I'll get back to them Monday",
];

const PROMISES = [
  {
    title: "Won't start half-informed",
    body: "If a request is missing something it needs, it asks first. Nothing touches the mailbox until the context is complete.",
  },
  {
    title: "Never sees your data",
    body: "Addresses, phone numbers, and thread ids become tokens before anything leaves the machine holding the page. The model reasons over P17, not your contacts.",
  },
  {
    title: "Never sends without you",
    body: "Send, delete, and calendar invites pause for approval. That gate is graph structure, not a line in a prompt — there is no path around it.",
  },
];

export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col justify-center px-6 py-14">
      <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-faint">
        Browser-driven email agent
      </p>

      <h1 className="mt-4 text-[2.1rem] font-semibold leading-[1.1] tracking-tight text-text sm:text-[2.6rem]">
        Ask it about your mail.
        <br />
        <span className="text-muted">Watch it work.</span>
      </h1>

      <p className="mt-4 max-w-lg text-[14.5px] leading-relaxed text-muted">
        It reads the page, reasons one step at a time, and acts in a real browser — every
        thought and click streamed to you live.
      </p>

      <div className="mt-8">
        <StartForm examples={EXAMPLES} />
      </div>

      <ul className="mt-12 grid gap-3 sm:grid-cols-3">
        {PROMISES.map((promise) => (
          <li
            key={promise.title}
            className="transition-smooth rounded-[--radius-card] border border-line bg-surface p-4 hover:border-line2 hover:bg-raised"
          >
            <h2 className="text-[13px] font-medium text-text">{promise.title}</h2>
            <p className="mt-1.5 text-[12.5px] leading-relaxed text-faint">{promise.body}</p>
          </li>
        ))}
      </ul>

      <footer className="mt-10 flex flex-wrap items-center gap-x-3 gap-y-1.5 border-t border-line pt-4 font-mono text-[10px] text-faint">
        <span>protocol v{PROTOCOL_VERSION}</span>
        <span aria-hidden className="text-line2">·</span>
        <span>brain {env.NEXT_PUBLIC_WS_URL}</span>
      </footer>
    </main>
  );
}
