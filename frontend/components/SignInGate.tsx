"use client";

import { useEffect, useState } from "react";

import { captureTokenFromUrl, clear, loginUrl, whoami, type Identity } from "@/lib/session";

/**
 * Wraps anything that needs a signed-in user.
 *
 * **Three states, not two.** Signed in, signed out, and *the server is not asking* — the
 * last is what `AUTH_MODE=off` looks like, and it renders children directly. Collapsing it
 * into "signed in" would work, but the distinction is what lets a localhost setup run with
 * no Google project while a deployed one cannot silently do the same.
 *
 * A fourth case is deliberately handled too: the backend being unreachable. Telling someone
 * to sign in when the login endpoint is also down sends them in a circle.
 */
export function SignInGate({ children }: { children: React.ReactNode }) {
  const [identity, setIdentity] = useState<Identity | null>(null);

  useEffect(() => {
    // Before anything else: a fresh redirect carries the token in the fragment, and it is
    // stripped from the URL the moment it is read.
    captureTokenFromUrl();
    void whoami().then(setIdentity);
  }, []);

  if (identity === null) {
    return (
      <main className="mx-auto flex w-full max-w-md flex-1 items-center justify-center px-6">
        <p className="text-[13.5px] text-muted">Checking your session…</p>
      </main>
    );
  }

  if (identity.authenticated) return <>{children}</>;

  // No login URL means the backend never answered, not that the user is signed out.
  const reachable = Boolean(identity.loginUrl);

  return (
    <main className="mx-auto flex w-full max-w-md flex-1 flex-col justify-center gap-5 px-6 py-14">
      <div>
        <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-faint">
          Inbox Autopilot
        </p>
        <h1 className="mt-3 text-[1.9rem] font-semibold leading-[1.15] tracking-tight text-text">
          {reachable ? "Sign in to continue" : "Can't reach the backend"}
        </h1>
      </div>

      {reachable ? (
        <>
          <p className="text-[14px] leading-relaxed text-muted">
            Google tells us who you are — nothing more. The agent reads your mail through
            your own browser, not through Google, so this grants no access to your mailbox.
          </p>
          <a
            href={loginUrl(typeof window === "undefined" ? "/" : window.location.pathname)}
            onClick={clear}
            className="transition-smooth inline-flex items-center justify-center gap-2 rounded-[--radius-control] border border-line2 bg-raised px-5 py-3 text-[13.5px] font-medium text-text hover:bg-raised2"
          >
            Continue with Google
          </a>
        </>
      ) : (
        <p className="text-[14px] leading-relaxed text-muted">
          The cockpit is running but the agent backend did not answer. Start it, then reload
          this page.
        </p>
      )}
    </main>
  );
}
