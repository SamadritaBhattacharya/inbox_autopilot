import { describe, expect, it } from "vitest";
import { parseEnv } from "../lib/env";

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
