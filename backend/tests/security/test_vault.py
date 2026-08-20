"""The PII vault and tokenizer — R7, "no raw data to the AI".

These tests are the evidence behind the product claim. If they weaken, the claim is no
longer true regardless of what the docs say.
"""
from __future__ import annotations

import pytest

from app.security.patterns import PiiKind, find_emails, find_phones
from app.security.tokenizer import PiiTokenizer
from app.security.vault import SessionPiiVault, UnknownToken


@pytest.fixture
def vault() -> SessionPiiVault:
    return SessionPiiVault()


@pytest.fixture
def tokenizer(vault: SessionPiiVault) -> PiiTokenizer:
    return PiiTokenizer(vault)


# ── stability and isolation ─────────────────────────────────────────────────


def test_the_same_address_gets_the_same_token_all_run(tokenizer):
    """The model must be able to reason about 'the same person' without learning who."""
    first = tokenizer.tokenize("from priya@corp.com")
    second = tokenizer.tokenize("reply to priya@corp.com about Friday")
    assert first.split()[-1] == second.split()[2]


def test_different_people_get_different_tokens(tokenizer):
    out = tokenizer.tokenize("priya@corp.com and dev@corp.com")
    tokens = [word for word in out.split() if word.startswith("P")]
    assert len(set(tokens)) == 2


def test_case_and_formatting_are_not_identity(tokenizer):
    """Two spellings of one address would show as two recipients on the approval card."""
    out = tokenizer.tokenize("Priya@Corp.com wrote; reply to priya@corp.com")
    assert out.count("P1") == 2


def test_tokens_are_never_reused_across_sessions():
    """A globally stable token IS a pseudonym, and pseudonyms correlate."""
    session_a, session_b = SessionPiiVault(), SessionPiiVault()
    PiiTokenizer(session_a).tokenize("first@corp.com second@corp.com")
    token_b = PiiTokenizer(session_b).tokenize("second@corp.com").strip()

    # Same numbering restarts, so P1 in session B is a DIFFERENT human than P1 in A.
    assert token_b == "P1"
    assert session_a.resolve("P1") == "first@corp.com"
    assert session_b.resolve("P1") == "second@corp.com"


# ── coverage: the deterministic classes must be complete ────────────────────


def test_no_raw_address_survives(tokenizer):
    text = "Forward to ops@example.co.uk, cc a.b+tag@sub.domain.org"
    out = tokenizer.tokenize(text)
    assert find_emails(out) == []
    assert "@" not in out


@pytest.mark.parametrize(
    "phone",
    ["+91 98765 43210", "+1-555-123-4567", "(555) 123-4567", "555.123.4567", "+919876543210"],
)
def test_phone_formats_are_tokenized(tokenizer, phone):
    out = tokenizer.tokenize(f"call me on {phone} tomorrow")
    assert phone not in out
    assert find_phones(out) == []


@pytest.mark.parametrize(
    "not_a_phone",
    ["the 2026-08-20 deadline", "invoice 1234", "$1,234.56 total", "version 1.2.3"],
)
def test_ordinary_numbers_are_left_alone(tokenizer, not_a_phone):
    """Every false positive deletes information the agent needs to do its job."""
    assert tokenizer.tokenize(not_a_phone) == not_a_phone


def test_phone_formatting_is_not_identity(tokenizer):
    out = tokenizer.tokenize("+91 98765 43210 and +919876543210")
    assert out.count("H1") == 2


def test_identifiers_are_tokenized_on_demand(tokenizer):
    token = tokenizer.tokenize_identifier("thread-18f3a9c2b1")
    assert token.startswith("T")
    assert "18f3a9c2b1" not in token


# ── names: learned, not guessed ─────────────────────────────────────────────


def test_an_unregistered_name_is_left_alone(tokenizer):
    """Blanket name matching would turn 'Friday' and 'Regards' into tokens."""
    assert tokenizer.tokenize("Best regards, see you Friday") == "Best regards, see you Friday"


def test_a_registered_name_is_replaced_everywhere_including_prose(tokenizer):
    tokenizer.register_person("Priya Nair")
    out = tokenizer.tokenize("Priya Nair asked whether priya nair could join")
    assert "Priya" not in out
    assert "Nair" not in out
    assert out.count("C1") == 2


def test_longer_names_win_so_no_surname_is_left_dangling(tokenizer):
    tokenizer.register_person("Priya")
    tokenizer.register_person("Priya Nair")
    out = tokenizer.tokenize("Priya Nair replied")
    assert "Nair" not in out


def test_ambiguous_short_names_are_refused(tokenizer):
    """'Sam', 'May', and 'Mark' are all ordinary words."""
    assert tokenizer.register_person("Al") is None
    assert tokenizer.tokenize("Al said May works") == "Al said May works"


def test_an_address_is_not_accepted_as_a_name(tokenizer):
    assert tokenizer.register_person("priya@corp.com") is None


def test_name_tokenization_can_be_disabled_but_addresses_cannot(vault):
    """Addresses and phones are non-optional; the flag only widens coverage to names."""
    tokenizer = PiiTokenizer(vault, tokenize_names=False)
    assert tokenizer.register_person("Priya Nair") is None

    out = tokenizer.tokenize("Priya Nair <priya@corp.com> +91 98765 43210")
    assert "Priya Nair" in out, "names are best-effort and this flag turns them off"
    assert "priya@corp.com" not in out, "addresses are never optional"
    assert "98765" not in out, "phones are never optional"


# ── idempotency ─────────────────────────────────────────────────────────────


def test_tokenizing_twice_is_a_no_op(tokenizer):
    """The funnel may run twice over a settled page; PP17 would be a real bug."""
    once = tokenizer.tokenize("mail priya@corp.com on +91 98765 43210")
    assert tokenizer.tokenize(once) == once


def test_empty_input_is_safe(tokenizer):
    assert tokenizer.tokenize("") == ""
    assert tokenizer.tokenize_identifier("") == ""


# ── resolution is one-way and closed ────────────────────────────────────────


def test_resolve_returns_the_original_spelling(vault, tokenizer):
    tokenizer.tokenize("Mail Priya@Corp.com")
    assert vault.resolve("P1") == "Priya@Corp.com", "what we type must be what the user wrote"


def test_an_unminted_token_is_refused(vault):
    """A token the model invented must not reach a real action."""
    with pytest.raises(UnknownToken):
        vault.resolve("P999")


def test_a_literal_address_is_not_a_token(vault):
    """This is the injected-recipient case: 'send it to attacker@evil.com'."""
    assert vault.knows("attacker@evil.com") is False
    with pytest.raises(UnknownToken):
        vault.resolve("attacker@evil.com")


# ── the vault itself must not leak ──────────────────────────────────────────


def test_the_vault_never_renders_its_contents(vault, tokenizer):
    """A vault reaches an exception or a debug log eventually."""
    tokenizer.tokenize("priya@corp.com and +91 98765 43210")
    rendered = f"{vault!r} {vault}"
    assert "priya@corp.com" not in rendered
    assert "98765" not in rendered
    assert "2 tokens" in rendered


def test_contains_pii_is_the_leak_assertion(tokenizer):
    tokenizer.register_person("Priya Nair")
    assert tokenizer.contains_pii("ping priya@corp.com") is True
    assert tokenizer.contains_pii("ping +91 98765 43210") is True
    assert tokenizer.contains_pii("ping Priya Nair") is True
    assert tokenizer.contains_pii(tokenizer.tokenize("ping priya@corp.com")) is False
    assert tokenizer.contains_pii("") is False


# ── realistic end-to-end shape ──────────────────────────────────────────────


def test_a_realistic_inbox_row_is_fully_tokenized(vault, tokenizer):
    tokenizer.register_person("Priya Nair")
    row = (
        "Priya Nair <priya.nair@corp.com> - Friday demo moved to 4pm. "
        "Call me on +91 98765 43210 if that clashes. Also cc dev@corp.com."
    )

    out = tokenizer.tokenize(row)

    assert tokenizer.contains_pii(out) is False
    # The MEANING survives: that is what makes the agent still able to do its job.
    assert "Friday demo moved to 4pm" in out
    assert vault.size == 4  # person, two addresses, one phone
    assert {t[0] for t in vault.tokens()} == {"C", "P", "H"}


def test_kind_prefixes_are_distinct():
    vault = SessionPiiVault()
    assert vault.token_for("a@b.com", PiiKind.EMAIL).startswith("P")
    assert vault.token_for("+911234567890", PiiKind.PHONE).startswith("H")
    assert vault.token_for("Priya Nair", PiiKind.PERSON).startswith("C")
    assert vault.token_for("thread-1", PiiKind.IDENTIFIER).startswith("T")
