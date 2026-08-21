"use client";

/**
 * The cockpit's half of authentication: hold a session token, and know who it belongs to.
 *
 * **Why the token arrives in the URL fragment.** `/auth/callback` redirects here with
 * `#token=…`. A fragment is never sent to a server, never written to a server log, and never
 * leaks through a `Referer` header — all three of which a query parameter does, on the very
 * next request the page makes. It is stripped from the address bar the moment it is read.
 *
 * **Why `localStorage` and not a cookie.** The socket connects to a different origin than
 * the page (the cockpit is deployed separately from the backend), so a cookie set by the
 * backend would not be sent by the browser on the WebSocket handshake anyway. The token has
 * to be something this code can read and append itself.
 */
import { apiBase } from "./env";

const STORAGE_KEY = "inbox-autopilot.session";

export interface Identity {
  authenticated: boolean;
  /** "off" means the server is not asking for sign-in at all. */
  mode: "off" | "google";
  email?: string;
  userId?: string;
  loginUrl?: string;
}

/**
 * Take the token out of the fragment, if this is a fresh sign-in redirect.
 *
 * Called once on mount. Rewriting the URL immediately matters: a token left in the address
 * bar gets copied into a bug report, a screen share, or a bookmark.
 */
export function captureTokenFromUrl(): string | null {
  if (typeof window === "undefined") return null;
  const hash = window.location.hash;
  if (!hash.startsWith("#token=")) return null;

  const token = decodeURIComponent(hash.slice("#token=".length));
  if (token) store(token);
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
  return token || null;
}

export function read(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    // Private mode, or storage disabled entirely. Being signed out is a workable state;
    // throwing here would take the whole cockpit down over a browser setting.
    return "";
  }
}

export function store(token: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, token);
  } catch {
    /* see `read` */
  }
}

export function clear(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* see `read` */
  }
}

/**
 * Who the backend thinks we are.
 *
 * Never throws and never 401s — "nobody" is a real answer that the sign-in gate renders,
 * and turning it into an error would make a signed-out visitor look like an outage.
 */
export async function whoami(): Promise<Identity> {
  const token = read();
  try {
    const response = await fetch(`${apiBase}/auth/me`, {
      headers: token ? { authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) return { authenticated: false, mode: "google" };
    return (await response.json()) as Identity;
  } catch {
    // The backend is down or unreachable. Reporting "not signed in" would send the user to
    // a login that also cannot work; the caller distinguishes this by the missing loginUrl.
    return { authenticated: false, mode: "google" };
  }
}

/** A fresh pairing code for the extension. Requires a session. */
export async function requestPairingCode(): Promise<string> {
  const token = read();
  const response = await fetch(`${apiBase}/auth/pairing`, {
    method: "POST",
    headers: token ? { authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) throw new Error("could not get a pairing code — try signing in again");
  return String((await response.json()).code ?? "");
}

export function loginUrl(next: string = "/"): string {
  return `${apiBase}/auth/login?next=${encodeURIComponent(next)}`;
}

/**
 * The socket URL, carrying the session.
 *
 * A browser `WebSocket` cannot set headers, so the token rides the handshake as a query
 * parameter. That is the standard workaround and it is why the backend accepts it there.
 */
export function socketUrl(base: string): string {
  const token = read();
  if (!token) return base;
  const url = new URL(base);
  url.searchParams.set("token", token);
  return url.toString();
}
