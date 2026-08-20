"""`PiiTokenizer` — rewrites text so no real identifier survives.

Runs as **stage 5 of the observation funnel**, before indexing and formatting. That
placement is the whole design: nothing downstream ever *holds* raw PII, so nothing
downstream can leak it — not through a log, an exception message, a checkpoint, a
trajectory dump, or a feature nobody has written yet. Moving this later in the pipeline
would be a security regression, not a refactor, and a test asserts the ordering.

**How names are handled, and why it is not NER.**

Addresses and phones are structural: they can be matched exactly, and they are matched
completely. Names in prose are not. A general name matcher applied to email bodies turns
"Friday", "Best regards", and "Q3 Financials" into tokens, and the agent then cannot read
the mail it was asked to triage — the cure is worse than the disease.

So names are learned, not guessed. When the funnel meets a *structured* name — a sender's
display name, a recipient chip, a contact-list row — it calls `register_person()`. From
then on, every occurrence of that name anywhere, including free prose in a body, is
tokenized. This catches the names that actually matter (the humans in your mailbox) with
zero false positives, at the cost of missing a name that never appears in a header.

That trade is stated plainly in the security model rather than dressed up: the tested,
demonstrable claim is "the model never saw a real address, phone, or thread id".
"""
from __future__ import annotations

import re

from app.security.patterns import TOKEN_RE, PiiKind, find_emails, find_phones
from app.security.vault import SessionPiiVault


class PiiTokenizer:
    """Replaces PII with vault tokens. Stateless apart from the vault it writes to."""

    def __init__(self, vault: SessionPiiVault, *, tokenize_names: bool = True) -> None:
        self._vault = vault
        self._tokenize_names = tokenize_names
        # Known person names, longest first, so "Priya Nair" is matched before "Priya" and
        # we never leave a dangling surname next to a token.
        self._person_patterns: list[tuple[re.Pattern[str], str]] = []

    # ── learning structured names ───────────────────────────────────────────

    def register_person(self, name: str) -> str | None:
        """Teach the tokenizer a real person's name; returns its token.

        Called by the funnel when it meets a name in a structured position. Returns None
        when name tokenization is off or the input is not usable as a name.
        """
        if not self._tokenize_names:
            return None
        cleaned = " ".join(name.strip().split())
        # A single short token is too ambiguous to blanket-replace across prose ("Sam",
        # "May", "Mark" are all words), and an address is handled by the email path.
        if len(cleaned) < 3 or "@" in cleaned or TOKEN_RE.fullmatch(cleaned):
            return None

        token = self._vault.token_for(cleaned, PiiKind.PERSON)
        if not any(existing == cleaned for _, existing in self._person_patterns):
            self._person_patterns.append((re.compile(re.escape(cleaned), re.IGNORECASE), cleaned))
            self._person_patterns.sort(key=lambda item: len(item[1]), reverse=True)
        return token

    # ── the stage ───────────────────────────────────────────────────────────

    def tokenize(self, text: str) -> str:
        """Rewrite every identifier in `text` as a stable token.

        Idempotent: running it over already-tokenized text is a no-op, so a double pass
        cannot produce `PP17`.
        """
        if not text:
            return text

        # Emails first. An address contains name-like and word-like parts, so any other
        # pass running first would carve it up and leave fragments of a real address behind.
        for email in find_emails(text):
            text = text.replace(email, self._vault.token_for(email, PiiKind.EMAIL))

        for phone in find_phones(text):
            text = text.replace(phone, self._vault.token_for(phone, PiiKind.PHONE))

        if self._tokenize_names:
            for pattern, name in self._person_patterns:
                token = self._vault.token_for(name, PiiKind.PERSON)
                text = pattern.sub(token, text)

        return text

    def tokenize_identifier(self, value: str) -> str:
        """For opaque ids — thread ids, message ids, the observation's `context_id`.

        These never appear in prose, so they are tokenized by explicit call rather than by
        pattern. A Gmail URL fragment is an identifier; that is why `Observation` carries
        `context_id` instead of `url`.
        """
        if not value:
            return value
        return self._vault.token_for(value, PiiKind.IDENTIFIER)

    # ── the leak check ──────────────────────────────────────────────────────

    def contains_pii(self, text: str) -> bool:
        """Does `text` still hold a raw identifier?

        The leak suite runs this over every egress point — observations, LLM request
        bodies, events, trajectories, logs. It is the assertion behind the claim.
        """
        if not text:
            return False
        if find_emails(text) or find_phones(text):
            return True
        return any(pattern.search(text) for pattern, _ in self._person_patterns)
