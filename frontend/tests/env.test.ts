import { describe, expect, it } from "vitest";
import { apiBaseFrom, parseEnv } from "../lib/env";

describe("cockpit env", () => {
  it("falls back to the local backend when unset", () => {
    expect(parseEnv({}).NEXT_PUBLIC_WS_URL).toBe("ws://localhost:8000/ws/run");
  });

  it("accepts a wss endpoint", () => {
    const parsed = parseEnv({ NEXT_PUBLIC_WS_URL: "wss://brain.example.dev/ws/run" });
    expect(parsed.NEXT_PUBLIC_WS_URL).toBe("wss://brain.example.dev/ws/run");
  });

  it("rejects an http endpoint -- the cockpit needs a bidirectional socket", () => {
    expect(() => parseEnv({ NEXT_PUBLIC_WS_URL: "https://brain.example.dev/ws/run" })).toThrow(
      /ws:\/\/ or wss:\/\//,
    );
  });

  it("rejects a relative url", () => {
    expect(() => parseEnv({ NEXT_PUBLIC_WS_URL: "/ws/run" })).toThrow();
  });
});

// ── the API origin is derived, not configured ───────────────────────────────

describe("apiBaseFrom", () => {
  it("derives the backend origin from the socket URL", () => {
    // One variable, by design: a second would be a second thing to get out of step, and
    // the env module is explicit that adding one is a review-blocking finding.
    expect(apiBaseFrom("ws://localhost:8000/ws/run")).toBe("http://localhost:8000");
  });

  it("keeps TLS when the socket is secure", () => {
    // Downgrading here would send the session token over plaintext to a host that only
    // speaks HTTPS — a silent failure in development, a leak in production.
    expect(apiBaseFrom("wss://agent.example.com/ws/run")).toBe("https://agent.example.com");
  });

  it("keeps a non-default port", () => {
    expect(apiBaseFrom("ws://127.0.0.1:9100/ws/run")).toBe("http://127.0.0.1:9100");
  });

  it("drops any path, query, or fragment", () => {
    expect(apiBaseFrom("ws://localhost:8000/ws/run?x=1#y")).toBe("http://localhost:8000");
  });
});
