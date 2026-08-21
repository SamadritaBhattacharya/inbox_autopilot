/**
 * `PiiTokenizer` — rewrites text so no real identifier survives.
 *
 * Runs as **stage 5 of the observation funnel**, before indexing and formatting. That
 * placement is the whole design: nothing downstream ever *holds* raw PII, so nothing
 * downstream can leak it — not through a log, an error message, a message to the backend, or
 * a feature nobody has written yet. In the extension this matters even more than on the
 * server: everything after this point crosses a network boundary.
 *
 * **How names are handled, and why it is not NER.**
 *
 * Addresses and phones are structural: they can be matched exactly, and they are matched
 * completely. Names in prose are not. A general name matcher applied to email bodies turns
 * "Friday", "Best regards", and "Q3 Financials" into tokens, and the agent then cannot read
 * the mail it was asked to triage — the cure is worse than the disease.
 *
 * So names are learned, not guessed. When the funnel meets a *structured* name — a sender's
 * display name, a recipient chip, a contact row — it calls `registerPerson()`. From then on
 * every occurrence of that name anywhere, including free prose, is tokenized. This catches
 * the names that actually matter (the humans in your mailbox) with zero false positives, at
 * the cost of missing a name that never appears in a header.
 *
 * That trade is stated plainly rather than dressed up: the tested, demonstrable claim is
 * "the model never saw a real address, phone, or thread id".
 *
 * Ported from `backend/app/security/tokenizer.py`.
 */
import { PiiKind, findEmails, findPhones, looksLikeToken } from "./patterns";
import type { SessionPiiVault } from "./vault";

/** Escape a literal string for use inside a RegExp. */
function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

interface PersonPattern {
  pattern: RegExp;
  name: string;
}

export class PiiTokenizer {
  private readonly vault: SessionPiiVault;
  private readonly tokenizeNames: boolean;
  /** Known person names, longest first, so "Priya Nair" matches before "Priya" and we never
   *  leave a dangling surname sitting next to a token. */
  private personPatterns: PersonPattern[] = [];

  constructor(vault: SessionPiiVault, options: { tokenizeNames?: boolean } = {}) {
    this.vault = vault;
    this.tokenizeNames = options.tokenizeNames ?? true;
  }

  // ── learning structured names ─────────────────────────────────────────────

  /**
   * Teach the tokenizer a real person's name; returns its token.
   *
   * Called by the funnel when it meets a name in a structured position. Returns `null` when
   * name tokenization is off or the input is not usable as a name.
   */
  registerPerson(name: string): string | null {
    if (!this.tokenizeNames) return null;
    const cleaned = name.trim().split(/\s+/).join(" ");
    // A single short token is too ambiguous to blanket-replace across prose ("Sam", "May",
    // "Mark" are all words), and an address is handled by the email path.
    if (cleaned.length < 3 || cleaned.includes("@") || looksLikeToken(cleaned)) return null;

    // Reached only from a structured position, so this is a real correspondent in this
    // mailbox and a legitimate target — unlike a name the tokenizer later substitutes into
    // prose, which proves nothing about who the mailbox actually knows.
    const token = this.vault.tokenFor(cleaned, PiiKind.Person, { addressable: true });
    if (!this.personPatterns.some((p) => p.name === cleaned)) {
      this.personPatterns.push({
        pattern: new RegExp(escapeRegExp(cleaned), "gi"),
        name: cleaned,
      });
      this.personPatterns.sort((a, b) => b.name.length - a.name.length);
    }
    return token;
  }

  // ── the stage ─────────────────────────────────────────────────────────────

  /**
   * Rewrite every identifier in `text` as a stable token.
   *
   * Idempotent: running it over already-tokenized text is a no-op, so a double pass cannot
   * produce `PP17`.
   *
   * `addressable` says whether this text came from somewhere the operator controls — a
   * sender chip, say, rather than the body of a message a stranger wrote. It has no effect
   * on redaction (everything is tokenized either way) and everything to do with whether the
   * resulting token may later be used as a recipient.
   */
  tokenize(text: string, options: { addressable?: boolean } = {}): string {
    if (!text) return text;
    let out = text;

    // Emails first. An address contains name-like and word-like parts, so any other pass
    // running first would carve it up and leave fragments of a real address behind.
    for (const email of findEmails(out)) {
      const token = this.vault.tokenFor(email, PiiKind.Email, {
        addressable: options.addressable ?? false,
      });
      out = out.split(email).join(token);
    }

    for (const phone of findPhones(out)) {
      out = out.split(phone).join(this.vault.tokenFor(phone, PiiKind.Phone));
    }

    if (this.tokenizeNames) {
      for (const { pattern, name } of this.personPatterns) {
        const token = this.vault.tokenFor(name, PiiKind.Person);
        out = out.replace(new RegExp(pattern.source, pattern.flags), token);
      }
    }

    return out;
  }

  /**
   * For opaque ids — thread ids, message ids, the observation's `contextId`.
   *
   * These never appear in prose, so they are tokenized by explicit call rather than by
   * pattern. A Gmail URL fragment is an identifier; that is why `Observation` carries
   * `contextId` instead of `url`.
   */
  tokenizeIdentifier(value: string): string {
    if (!value) return value;
    return this.vault.tokenFor(value, PiiKind.Identifier);
  }

  // ── the leak check ────────────────────────────────────────────────────────

  /**
   * Does `text` still hold a raw identifier?
   *
   * The leak suite runs this over every egress point — observations, messages to the
   * backend, events, logs. It is the assertion behind the claim.
   */
  containsPii(text: string): boolean {
    if (!text) return false;
    if (findEmails(text).length > 0 || findPhones(text).length > 0) return true;
    return this.personPatterns.some(({ pattern }) =>
      new RegExp(pattern.source, pattern.flags).test(text),
    );
  }
}
