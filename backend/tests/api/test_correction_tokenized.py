"""A mid-run correction is operator text, and must cross the same trust boundary the task does.

Raised from a real session: the user watched an email get composed with no recipient, typed
"add the recipient", and the agent could not carry it out. Two separate reasons, both here.

**It reached the model in the clear.** `Correction from the user: {text}` was injected into
the loop verbatim. An address the user typed went straight past the vault that exists to
stop exactly that, and was persisted in the feedback store on the way — where
`candidates()` reads it back across every thread.

**It could not be acted on even when understood.** The dispatcher only ever accepts minted
tokens (`UNKNOWN_TOKEN` otherwise), so an address with no token behind it is refused however
clearly the model read the instruction. Tokenizing with `trust()` is what makes the
correction *executable*, not merely legible.
"""
from __future__ import annotations

from app.security.vault import SessionPiiVault, trust_addresses


def test_an_address_in_a_correction_becomes_a_token():
    vault = SessionPiiVault()
    out = trust_addresses("also add alex@corp.com to the email", vault)

    assert "alex@corp.com" not in out
    assert out == "also add P1 to the email"


def test_the_token_is_ADDRESSABLE_so_the_dispatcher_accepts_it():
    """The half that makes the correction executable. A token that resolves but is not
    addressable is refused as `UNTRUSTED_RECIPIENT` — correct for an address lifted from a
    hostile email body, wrong for one the operator typed themselves."""
    vault = SessionPiiVault()
    token = trust_addresses("cc bob@corp.com", vault).split()[-1]

    assert vault.is_addressable(token)
    assert vault.resolve(token) == "bob@corp.com"


def test_several_addresses_in_one_correction_all_become_tokens():
    """Directly the "add person A and person B" case, arriving as a correction."""
    vault = SessionPiiVault()
    out = trust_addresses("add alex@corp.com and priya@corp.com", vault)

    assert "@corp.com" not in out
    assert "P1" in out and "P2" in out


def test_the_same_address_twice_gets_one_token():
    vault = SessionPiiVault()
    out = trust_addresses("add alex@corp.com, yes alex@corp.com", vault)

    assert out.count("P1") == 2
    assert vault.size == 1


def test_a_correction_with_no_address_is_untouched():
    """The common case must not be disturbed — most corrections name nobody."""
    vault = SessionPiiVault()
    assert trust_addresses("make it shorter", vault) == "make it shorter"


def test_no_vault_is_not_a_crash():
    """A read-only path or a unit test may have no session; there is nothing to mint
    against and refusing to run would be worse than passing the text through."""
    assert trust_addresses("add alex@corp.com", None) == "add alex@corp.com"


def test_a_token_the_user_retypes_is_left_alone():
    """The user can see tokens in the cockpit and may echo one back. It is already a token;
    re-tokenizing would mint a token for a token."""
    vault = SessionPiiVault()
    vault.trust("alex@corp.com")

    assert trust_addresses("add P1 as well", vault) == "add P1 as well"
