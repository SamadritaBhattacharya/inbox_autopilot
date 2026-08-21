"use client";

import { useState } from "react";

import { requestPairingCode } from "@/lib/session";

/**
 * Hands the user a pairing code for their extension.
 *
 * **Codes are minted on demand, not on page load.** They are single-use and expire in ten
 * minutes, so generating one for everybody who opens the cockpit would burn a fresh code on
 * every refresh and leave the previous one dead — which reads to the user as "the code
 * stopped working", the least debuggable failure in the flow.
 */
export function PairBrowser() {
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  const generate = async () => {
    setBusy(true);
    setError("");
    try {
      setCode(await requestPairingCode());
      setCopied(false);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
    } catch {
      // Clipboard access is denied in plenty of contexts. The code is on screen either way,
      // so this is a convenience failing, not the feature failing.
    }
  };

  return (
    <section className="rounded-[--radius-card] border border-line bg-surface p-5">
      <h2 className="text-[13px] font-medium text-text">Connect your browser</h2>
      <p className="mt-1.5 text-[12.5px] leading-relaxed text-faint">
        The agent drives Gmail in <em>your</em> Chrome, where you are already signed in. Install
        the bridge extension, then pair it with the code below.
      </p>

      {code ? (
        <div className="mt-4">
          <div className="flex items-center gap-2">
            <code className="flex-1 rounded-[--radius-control] border border-line2 bg-raised px-4 py-3 text-center font-mono text-[18px] tracking-[0.3em] text-text">
              {code}
            </code>
            <button
              type="button"
              onClick={() => void copy()}
              className="transition-smooth shrink-0 rounded-[--radius-control] border border-line px-3.5 py-3 text-[13px] text-muted hover:border-line2 hover:text-text"
            >
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
          <p className="mt-2 text-[11.5px] text-faint">
            Valid for 10 minutes, and only once. Click the extension icon, paste it, and
            press Save and connect.
          </p>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => void generate()}
          disabled={busy}
          className="transition-smooth mt-4 w-full rounded-[--radius-control] border border-line2 bg-raised px-4 py-2.5 text-[13px] font-medium text-text hover:bg-raised2 disabled:opacity-50"
        >
          {busy ? "Generating…" : "Get a pairing code"}
        </button>
      )}

      {error && <p className="mt-3 text-[12.5px] text-danger">{error}</p>}
    </section>
  );
}
