/**
 * Detection of personally identifying values in text.
 *
 * One job: find PII in a string and say what kind it is. No vault, no tokens, no rewriting —
 * those live next door, so this can be tested exhaustively on its own.
 *
 * **Precision matters more than recall here, but not equally for every kind.** Addresses,
 * phones, and identifiers are structural and can be matched exactly; those are the classes
 * the security story is built on and they must be complete. Personal names in prose are not
 * structural at all — an aggressive name matcher turns "Friday" and "Regards" into tokens
 * and destroys the very content the agent has to read to do its job. So names are handled by
 * a different mechanism entirely (see `tokenizer.ts`): only names already *known* to the
 * session get replaced.
 *
 * Ported from `backend/app/security/patterns.py`. The regexes are deliberately identical;
 * `tests/conformance.test.ts` pins that they classify the same strings the same way.
 */

/** What a matched value is. The token prefix follows from this. */
export enum PiiKind {
  Email = "EMAIL",
  Phone = "PHONE",
  Person = "PERSON",
  Identifier = "IDENTIFIER",
}

/**
 * Token prefixes. Short because they ride in every observation the model reads, and a
 * verbose scheme would eat the token budget the funnel works to protect.
 */
export const TOKEN_PREFIX: Record<PiiKind, string> = {
  [PiiKind.Email]: "P",
  [PiiKind.Phone]: "H",
  [PiiKind.Person]: "C",
  [PiiKind.Identifier]: "T",
};

/**
 * Matches any token this system mints. Used to keep tokenization idempotent — running the
 * funnel twice over the same text must not produce `PP17`.
 */
export const TOKEN_RE = /\b(?:P|H|C|T)\d+\b/g;

export const EMAIL_RE = /\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b/g;

/**
 * Phones are matched in two shapes and then digit-counted. A single permissive pattern would
 * happily swallow dates, order numbers, and prices — every false positive here deletes
 * information the agent needs, so the candidates are deliberately narrow.
 */
const PHONE_CANDIDATES = [
  // International: +91 98765 43210, +1-555-123-4567
  /\+\d{1,3}[\s.\-]?\(?\d{1,4}\)?[\s.\-]?\d[\d\s.\-]{4,12}\d/g,
  // North-American style without a country code: (555) 123-4567, 555.123.4567
  /\(?\b\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b/g,
];
const MIN_PHONE_DIGITS = 10;
const MAX_PHONE_DIGITS = 15; // E.164 ceiling; anything longer is an id, not a number

/** Fresh regex per call: a global regex carries `lastIndex`, and sharing one across calls
 *  makes the SECOND search start halfway through the string. */
const fresh = (re: RegExp): RegExp => new RegExp(re.source, re.flags);

export function findEmails(text: string): string[] {
  return text.match(fresh(EMAIL_RE)) ?? [];
}

function countDigits(value: string): number {
  let digits = 0;
  for (const character of value) if (character >= "0" && character <= "9") digits += 1;
  return digits;
}

/**
 * Candidate phone numbers, filtered by digit count.
 *
 * The digit-count check is what keeps "2026-08-20" and "1,234,567.89" out.
 */
export function findPhones(text: string): string[] {
  const found: string[] = [];
  const seenSpans: Array<[number, number]> = [];

  for (const pattern of PHONE_CANDIDATES) {
    const re = fresh(pattern);
    let match: RegExpExecArray | null;
    while ((match = re.exec(text)) !== null) {
      const start = match.index;
      const end = start + match[0].length;
      // A number already claimed by an earlier (more specific) pattern is not a second
      // number.
      if (seenSpans.some(([s, e]) => start < e && s < end)) continue;
      const digits = countDigits(match[0]);
      if (digits >= MIN_PHONE_DIGITS && digits <= MAX_PHONE_DIGITS) {
        found.push(match[0]);
        seenSpans.push([start, end]);
      }
    }
  }
  return found;
}

export function looksLikeToken(value: string): boolean {
  const trimmed = value.trim();
  const match = fresh(TOKEN_RE).exec(trimmed);
  return match !== null && match[0] === trimmed;
}
