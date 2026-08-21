/**
 * The PII vault — the reason the model never sees a real address.
 *
 * One vault per session. It holds the only mapping from token back to real value, it lives
 * **in the extension** next to the DOM, and it is never persisted — not to storage, not to
 * the backend, not to a log.
 *
 * That location is the entire point of the bridge architecture. The backend does the
 * reasoning and holds the model keys; it never holds this map, so a compromised backend
 * still cannot turn `P17` back into a person.
 *
 * Three properties carry the security story:
 *
 * **Stable within a session.** `alice@corp.com` is `P17` for the whole run, so the model can
 * reason about "the same person" across turns without learning who that is.
 *
 * **Never reused across sessions.** A fresh session gets a fresh vault and fresh numbering.
 * Global stable tokens would themselves become identifiers — `P17` meaning the same human
 * every day is just a pseudonym, and pseudonyms correlate.
 *
 * **One-way for the brain.** Only this side resolves a token, and only at dispatch.
 *
 * Ported from `backend/app/security/vault.py`.
 */
import { PiiKind, TOKEN_PREFIX } from "./patterns";

/**
 * A token this vault never minted.
 *
 * Thrown rather than passed through. A silent passthrough would let an injected string like
 * `P999` — or a literal address the model invented — reach a real action, which is precisely
 * the attack the token scheme exists to stop.
 */
export class UnknownToken extends Error {
  constructor(token: string) {
    super(
      `${JSON.stringify(token)} was never minted by this session's vault. A token the ` +
        "model invented, or one carried over from another session, must not reach an action.",
    );
    this.name = "UnknownToken";
  }
}

export class SessionPiiVault {
  /** value -> token, and token -> value. Two maps rather than one plus a scan: the forward
   *  direction runs on every element of every observation. */
  private readonly forward = new Map<string, string>();
  private readonly reverse = new Map<string, string>();
  private readonly counters = new Map<PiiKind, number>();

  /**
   * Tokens that may be used as an ACTION TARGET.
   *
   * Every address on the page is tokenized — that is redaction, and it is unconditional. But
   * tokenizing an address is not endorsing it as a recipient. An address sitting in the body
   * of a hostile email gets a token so the model never sees it in the clear; it must NOT
   * thereby become somewhere the agent can send mail.
   *
   * Addressable means the value came from somewhere the OPERATOR controls: a sender or
   * recipient chip (a person genuinely in this mailbox), or the user's own instruction.
   */
  private readonly addressable = new Set<string>();

  // ── minting ───────────────────────────────────────────────────────────────

  /**
   * The token for `value`, minting one on first sight.
   *
   * Normalised on the way in so `Alice@Corp.com` and `alice@corp.com` are one person rather
   * than two — otherwise the model reasons about them as different people and, worse, the
   * approval preview shows two recipients where there is one.
   */
  tokenFor(value: string, kind: PiiKind, options: { addressable?: boolean } = {}): string {
    const normalised = normalise(value, kind);
    const existing = this.forward.get(normalised);
    if (existing !== undefined) {
      // Upgrade only, never downgrade: an address seen once in a structured position is a
      // real correspondent, whatever else it also appears inside.
      if (options.addressable) this.addressable.add(existing);
      return existing;
    }

    const next = (this.counters.get(kind) ?? 0) + 1;
    this.counters.set(kind, next);
    const token = `${TOKEN_PREFIX[kind]}${next}`;
    this.forward.set(normalised, token);
    // The reverse map keeps the ORIGINAL spelling: what gets typed into Gmail should be what
    // the user actually wrote, not our lowercased version.
    this.reverse.set(token, value);
    if (options.addressable) this.addressable.add(token);
    return token;
  }

  /**
   * Mint a token the operator supplied, and mark it addressable.
   *
   * An address in the USER's own instruction is trusted input: they typed it, so it is
   * somewhere they meant to write. An address in an email body is not, however confidently
   * the email asserts otherwise.
   */
  trust(value: string, kind: PiiKind = PiiKind.Email): string {
    return this.tokenFor(value, kind, { addressable: true });
  }

  // ── resolution (extension-side only) ──────────────────────────────────────

  resolve(token: string): string {
    const value = this.reverse.get(token.trim());
    if (value === undefined) throw new UnknownToken(token);
    return value;
  }

  knows(token: string): boolean {
    return this.reverse.has(token.trim());
  }

  /** The token standing in for `value`, if this session ever minted one. For tests and the
   *  approval card — never for the model. */
  tokenOf(value: string, kind: PiiKind = PiiKind.Email): string | null {
    return this.forward.get(normalise(value, kind)) ?? null;
  }

  /**
   * May this token be used as an action TARGET?
   *
   * False for anything the vault only ever saw inside page content. That is the difference
   * between "the model must not read this address" and "the agent may send mail here", and
   * conflating them is what lets an injected instruction pick a recipient.
   */
  isAddressable(token: string): boolean {
    return this.addressable.has(token.trim());
  }

  // ── introspection, for tests and the leak suite ───────────────────────────

  get size(): number {
    return this.reverse.size;
  }

  tokens(): string[] {
    return [...this.reverse.keys()];
  }

  /**
   * Never render the mapping.
   *
   * A vault ends up inside an error or a debug log eventually, and a default object dump
   * would print every address it holds at exactly that moment.
   */
  toString(): string {
    return `<SessionPiiVault ${this.size} tokens>`;
  }

  toJSON(): string {
    // `JSON.stringify(vault)` is the likeliest accidental leak of all — a structured logger
    // does it without being asked.
    return this.toString();
  }
}

function normalise(value: string, kind: PiiKind): string {
  const stripped = value.trim();
  if (kind === PiiKind.Email) return stripped.toLowerCase();
  if (kind === PiiKind.Phone) {
    // Formatting is not identity: +91 98765 43210 and +919876543210 are one number.
    return [...stripped].filter((c) => (c >= "0" && c <= "9") || c === "+").join("");
  }
  if (kind === PiiKind.Person) return stripped.toLowerCase().split(/\s+/).join(" ");
  return stripped;
}
