"""Detection of personally identifying values in text.

One job: find PII in a string and say what kind it is. No vault, no tokens, no rewriting —
those live next door, so this can be tested exhaustively on its own.

**Precision matters more than recall here, but not equally for every kind.** Addresses,
phones, and identifiers are structural and can be matched exactly; those are the classes
the security story is built on and they must be complete. Personal names in prose are not
structural at all — an aggressive name matcher turns "Friday" and "Regards" into tokens
and destroys the very content the agent has to read to do its job. So names are handled by
a different mechanism entirely (see `tokenizer.py`): only names already *known* to the
session get replaced.
"""
from __future__ import annotations

import re
from enum import StrEnum


class PiiKind(StrEnum):
    """What a matched value is. The token prefix follows from this."""

    EMAIL = "EMAIL"
    PHONE = "PHONE"
    PERSON = "PERSON"
    IDENTIFIER = "IDENTIFIER"


#: Token prefixes. Short because they ride in every observation the model reads, and a
#: verbose scheme would eat the token budget the funnel works to protect.
TOKEN_PREFIX: dict[PiiKind, str] = {
    PiiKind.EMAIL: "P",
    PiiKind.PHONE: "H",
    PiiKind.PERSON: "C",
    PiiKind.IDENTIFIER: "T",
}

#: Matches any token this system mints. Used to keep tokenization idempotent — running the
#: funnel twice over the same text must not produce `PP17`.
TOKEN_RE = re.compile(r"\b(?:P|H|C|T)\d+\b")

EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

# Phones are matched in two shapes and then digit-counted. A single permissive pattern
# would happily swallow dates, order numbers, and prices — every false positive here
# deletes information the agent needs, so the candidates are deliberately narrow.
_PHONE_CANDIDATES = (
    # International: +91 98765 43210, +1-555-123-4567
    re.compile(r"\+\d{1,3}[\s.\-]?\(?\d{1,4}\)?[\s.\-]?\d[\d\s.\-]{4,12}\d"),
    # North-American style without a country code: (555) 123-4567, 555.123.4567
    re.compile(r"\(?\b\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b"),
)
_MIN_PHONE_DIGITS = 10
_MAX_PHONE_DIGITS = 15  # E.164 ceiling; anything longer is an id, not a number


def find_emails(text: str) -> list[str]:
    return EMAIL_RE.findall(text)


def find_phones(text: str) -> list[str]:
    """Candidate phone numbers, filtered by digit count.

    The digit-count check is what keeps "2026-08-20" and "1,234,567.89" out.
    """
    found: list[str] = []
    seen_spans: list[tuple[int, int]] = []

    for pattern in _PHONE_CANDIDATES:
        for match in pattern.finditer(text):
            start, end = match.span()
            # A number already claimed by an earlier (more specific) pattern is not a
            # second number.
            if any(start < s_end and s_start < end for s_start, s_end in seen_spans):
                continue
            digits = sum(character.isdigit() for character in match.group())
            if _MIN_PHONE_DIGITS <= digits <= _MAX_PHONE_DIGITS:
                found.append(match.group())
                seen_spans.append((start, end))
    return found


def looks_like_token(value: str) -> bool:
    return bool(TOKEN_RE.fullmatch(value.strip()))
