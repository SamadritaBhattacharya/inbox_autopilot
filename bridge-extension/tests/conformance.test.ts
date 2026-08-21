/**
 * The TypeScript funnel against the SAME fixtures the Python one is held to.
 *
 * This is the file that makes "two implementations of the funnel" survivable. Both read
 * `fixtures/funnel/cases/` and must produce byte-identical `fixtures/funnel/expected/`.
 * Without it the two drift silently, and the symptom — a bug that reproduces on the
 * Playwright surface but not the extension, or the reverse — points at the agent for a day
 * before anyone suspects the surface.
 *
 * The goldens are generated from the Python side (`scripts/gen_funnel_goldens.py`), which is
 * the older and better-exercised implementation. A failure here means THIS side diverged, or
 * that a deliberate change was made on the other and both need updating together.
 */
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { ObservationFunnel } from "@/funnel/pipeline";
import type { PageMeta, RawElement } from "@/funnel/raw";
import { PiiTokenizer } from "@/security/tokenizer";
import { SessionPiiVault } from "@/security/vault";

const FIXTURES = fileURLToPath(new URL("../../fixtures/funnel", import.meta.url));

interface Case {
  elements: RawElement[];
  meta: PageMeta;
}

const load = (kind: string, name: string): unknown =>
  JSON.parse(readFileSync(`${FIXTURES}/${kind}/${name}.json`, "utf8"));

const CASES = readdirSync(`${FIXTURES}/cases`)
  .filter((file) => file.endsWith(".json"))
  .map((file) => file.replace(/\.json$/, ""))
  .sort();

function run(input: Case): unknown {
  const vault = new SessionPiiVault();
  const funnel = new ObservationFunnel(new PiiTokenizer(vault));
  // Round-tripped through JSON so the comparison is of DATA, not of object identity or of
  // key insertion order — the golden came from another language and has neither.
  const { observation } = funnel.run(input.elements, input.meta);
  return JSON.parse(JSON.stringify(observation));
}

describe("funnel conformance with the Python implementation", () => {
  it("has fixtures to run", () => {
    // A conformance suite with no cases passes silently and proves nothing.
    expect(CASES.length).toBeGreaterThan(0);
  });

  it.each(CASES)("matches the committed output for %s", (name) => {
    const input = load("cases", name) as Case;
    const expected = load("expected", name);

    expect(run(input)).toEqual(expected);
  });
});
