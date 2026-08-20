import { PROTOCOL_VERSION } from "@inbox/contracts";
import { env } from "@/lib/env";

/**
 * Landing shell — a Server Component, and deliberately static.
 *
 * The live cockpit arrives at M3 as a single client island beneath this shell. Keeping
 * everything that does not need a socket on the server is the reason this app is Next.js
 * rather than a plain SPA; if this file ever needs `"use client"`, something has been put
 * in the wrong place.
 */

const PROMISES = [
  {
    title: "Won't start half-informed",
    body: "If the task is missing something the action needs, it asks first. Nothing touches the mailbox until the context is complete.",
  },
  {
    title: "Never sees your data",
    body: "Addresses, phone numbers, and thread ids become tokens before anything leaves the machine holding the page. The model reasons over P17, not over your contacts.",
  },
  {
    title: "Never sends without you",
    body: "Send, delete, and calendar invites pause for approval. That gate is graph structure, not a line in a prompt — there is no path around it.",
  },
];

export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col justify-center px-6 py-16">
      <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-faint">
        Browser-driven email agent
      </p>

      <h1 className="mt-4 text-4xl font-semibold leading-[1.1] tracking-tight text-text sm:text-5xl">
        Give it a task.
        <br />
        <span className="text-muted">Watch it work your inbox.</span>
      </h1>

      <p className="mt-5 max-w-xl text-[15px] leading-relaxed text-muted">
        It reads the page, reasons one step at a time, and acts in a real browser — every
        thought and click streamed to you live.
      </p>

      <ul className="mt-10 grid gap-3 sm:grid-cols-3">
        {PROMISES.map((promise) => (
          <li
            key={promise.title}
            className="transition-smooth rounded-[--radius-card] border border-line bg-surface p-4 hover:border-line2 hover:bg-raised"
          >
            <h2 className="text-sm font-medium text-text">{promise.title}</h2>
            <p className="mt-2 text-[13px] leading-relaxed text-faint">{promise.body}</p>
          </li>
        ))}
      </ul>

      <footer className="mt-12 flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-line pt-5 font-mono text-[11px] text-faint">
        <span>protocol v{PROTOCOL_VERSION}</span>
        <span aria-hidden className="text-line2">
          ·
        </span>
        <span>brain {env.NEXT_PUBLIC_WS_URL}</span>
        <span aria-hidden className="text-line2">
          ·
        </span>
        <span className="text-muted">cockpit lands at M3</span>
      </footer>
    </main>
  );
}
