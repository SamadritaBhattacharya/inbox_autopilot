import { z } from "zod";

/**
 * The cockpit's entire environment surface.
 *
 * It is ONE variable, and that is deliberate. The backend owns every provider key; a
 * second entry appearing here means a secret is drifting toward the browser bundle, which
 * is a review-blocking finding rather than a config change.
 *
 * The socket points at the backend host directly. A serverless frontend platform cannot
 * hold a long-lived WebSocket, so there is no route-handler proxy to hide behind.
 */
const envSchema = z.object({
  NEXT_PUBLIC_WS_URL: z
    .string()
    .url("NEXT_PUBLIC_WS_URL must be an absolute ws:// or wss:// URL")
    .refine((value) => value.startsWith("ws://") || value.startsWith("wss://"), {
      message: "NEXT_PUBLIC_WS_URL must use the ws:// or wss:// scheme",
    })
    .default("ws://localhost:8000/ws/run"),
});

export type CockpitEnv = z.infer<typeof envSchema>;

/** Pure, so it can be tested without touching process.env. */
export function parseEnv(raw: Record<string, string | undefined>): CockpitEnv {
  const result = envSchema.safeParse(raw);
  if (!result.success) {
    const detail = result.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; ");
    throw new Error(`Invalid cockpit environment -- ${detail}`);
  }
  return result.data;
}

/**
 * Next inlines `NEXT_PUBLIC_*` at build time, so this must be a literal property access —
 * a dynamic `raw[name]` lookup reads as `undefined` in the browser bundle.
 */
export const env: CockpitEnv = parseEnv({
  NEXT_PUBLIC_WS_URL: process.env.NEXT_PUBLIC_WS_URL,
});
