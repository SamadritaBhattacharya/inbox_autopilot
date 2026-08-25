"""`ProceduralMemory` — the five rules from docs/IMPROVEMENT-PLAN.md §B5, each pinned.

No browser, no fakes of a browser either: `verify` is a plain callable the test controls
directly, which is exactly the seam the module is built around — this store never has a DOM
of its own, so every test drives it the same way a real surface eventually would, by
supplying the answer to "does this still match?" itself.
"""
from __future__ import annotations

import pytest

from app.surface.memory import (
    InMemoryProceduralMemory,
    LocatorDescriptor,
    PageSignature,
    Provenance,
    UnsafeMemoryValue,
)

COMPOSE = PageSignature(host="mail.google.com", view="compose")
INBOX = PageSignature(host="mail.google.com", view="inbox")

SEND = LocatorDescriptor(role="button", name_pattern="Send", container_path="dialog")


def always_matches(_descriptor: LocatorDescriptor) -> bool:
    return True


def never_matches(_descriptor: LocatorDescriptor) -> bool:
    return False


# ── rule 1: a cached locator is a hypothesis, never a fact ──────────────────


def test_recall_returns_nothing_without_a_prior_remember():
    memory = InMemoryProceduralMemory()
    assert memory.recall(COMPOSE, "Send", verify=always_matches) is None


def test_recall_always_calls_verify_even_on_a_hit():
    """The whole point: a remembered descriptor is never handed back on trust alone."""
    memory = InMemoryProceduralMemory()
    memory.remember(COMPOSE, "Send", SEND)

    calls = []

    def counting_verify(descriptor: LocatorDescriptor) -> bool:
        calls.append(descriptor)
        return True

    memory.recall(COMPOSE, "Send", verify=counting_verify)
    assert calls == [SEND]


def test_a_failed_verification_does_not_return_the_descriptor():
    memory = InMemoryProceduralMemory()
    memory.remember(COMPOSE, "Send", SEND)
    assert memory.recall(COMPOSE, "Send", verify=never_matches) is None


def test_a_successful_verification_returns_the_exact_descriptor():
    memory = InMemoryProceduralMemory()
    memory.remember(COMPOSE, "Send", SEND)
    assert memory.recall(COMPOSE, "Send", verify=always_matches) == SEND


# ── rule 2: provenance is typed, and learned never outranks curated ─────────


def test_a_learned_write_never_overwrites_a_curated_entry():
    memory = InMemoryProceduralMemory()
    curated = LocatorDescriptor(role="button", name_pattern="Send", container_path="dialog")
    learned = LocatorDescriptor(role="button", name_pattern="Send Now", container_path="")

    memory.remember(COMPOSE, "Send", curated, provenance=Provenance.CURATED)
    memory.remember(COMPOSE, "Send", learned, provenance=Provenance.LEARNED)

    assert memory.recall(COMPOSE, "Send", verify=always_matches) == curated


def test_a_curated_write_can_replace_a_previous_curated_entry():
    """A human is allowed to correct their own earlier entry."""
    memory = InMemoryProceduralMemory()
    first = LocatorDescriptor(role="button", name_pattern="Send", container_path="")
    corrected = LocatorDescriptor(role="button", name_pattern="Send Email", container_path="")

    memory.remember(COMPOSE, "Send", first, provenance=Provenance.CURATED)
    memory.remember(COMPOSE, "Send", corrected, provenance=Provenance.CURATED)

    assert memory.recall(COMPOSE, "Send", verify=always_matches) == corrected


def test_a_learned_write_can_replace_a_previous_learned_entry():
    memory = InMemoryProceduralMemory()
    first = LocatorDescriptor(role="button", name_pattern="Send", container_path="")
    updated = LocatorDescriptor(role="button", name_pattern="Send now", container_path="")

    memory.remember(COMPOSE, "Send", first, provenance=Provenance.LEARNED)
    memory.remember(COMPOSE, "Send", updated, provenance=Provenance.LEARNED)

    assert memory.recall(COMPOSE, "Send", verify=always_matches) == updated


def test_learned_is_the_default_provenance():
    """A caller that forgets to specify provenance gets the LOWER trust level, never the
    higher one — the unsafe default would be silently trusting an unverified write."""
    memory = InMemoryProceduralMemory()
    memory.remember(COMPOSE, "Send", SEND)
    entry = memory.entry(COMPOSE, "Send")
    assert entry is not None
    assert entry.provenance is Provenance.LEARNED


# ── rule 3: decay and evict ──────────────────────────────────────────────────


def test_one_miss_does_not_evict():
    """A single transient failure (an animation mid-render) must not throw away a locator
    that is actually still correct."""
    memory = InMemoryProceduralMemory()
    memory.remember(COMPOSE, "Send", SEND)

    memory.recall(COMPOSE, "Send", verify=never_matches)

    assert memory.entry(COMPOSE, "Send") is not None


def test_consecutive_misses_evict_the_entry():
    memory = InMemoryProceduralMemory()
    memory.remember(COMPOSE, "Send", SEND)

    for _ in range(3):
        memory.recall(COMPOSE, "Send", verify=never_matches)

    assert memory.entry(COMPOSE, "Send") is None
    assert memory.recall(COMPOSE, "Send", verify=always_matches) is None


def test_a_hit_between_misses_resets_the_streak():
    memory = InMemoryProceduralMemory()
    memory.remember(COMPOSE, "Send", SEND)

    memory.recall(COMPOSE, "Send", verify=never_matches)  # miss 1
    memory.recall(COMPOSE, "Send", verify=always_matches)  # hit — resets
    memory.recall(COMPOSE, "Send", verify=never_matches)  # miss 1 again, not 2

    assert memory.entry(COMPOSE, "Send") is not None


def test_a_curated_entry_decays_exactly_like_a_learned_one():
    """Gmail does not spare hand-written entries when it ships a redesign — a stale curated
    locator is exactly as wrong as a stale learned one."""
    memory = InMemoryProceduralMemory()
    memory.remember(COMPOSE, "Send", SEND, provenance=Provenance.CURATED)

    for _ in range(3):
        memory.recall(COMPOSE, "Send", verify=never_matches)

    assert memory.entry(COMPOSE, "Send") is None


def test_hits_are_counted():
    memory = InMemoryProceduralMemory()
    memory.remember(COMPOSE, "Send", SEND)

    memory.recall(COMPOSE, "Send", verify=always_matches)
    memory.recall(COMPOSE, "Send", verify=always_matches)

    entry = memory.entry(COMPOSE, "Send")
    assert entry is not None
    assert entry.hits == 2


# ── rule 4: memory never shortcuts a gate ────────────────────────────────────


def test_the_store_has_no_dispatch_capability_at_all():
    """Structural, not behavioural: this module cannot bypass approval because it has no
    verb that touches the surface, the vault, or the approval gate. `act`, `approve`, and
    `preview` — the surface port's own methods — do not exist on this class."""
    memory = InMemoryProceduralMemory()
    for forbidden in ("act", "approve", "preview", "dispatch", "send"):
        assert not hasattr(memory, forbidden)


# ── rule 5: no PII in the store, ever ────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_name",
    ["alice@corp.com", "Click here alice@corp.com to confirm", "call +1 415 555 0100"],
)
def test_a_descriptor_carrying_an_address_or_phone_is_refused(bad_name):
    with pytest.raises(UnsafeMemoryValue):
        LocatorDescriptor(role="button", name_pattern=bad_name)


def test_a_page_signature_carrying_an_address_is_refused():
    with pytest.raises(UnsafeMemoryValue):
        PageSignature(host="mail.google.com", view="alice@corp.com")


def test_remember_refuses_an_unsafe_verb():
    memory = InMemoryProceduralMemory()
    with pytest.raises(UnsafeMemoryValue):
        memory.remember(COMPOSE, "email alice@corp.com", SEND)


def test_a_vault_token_in_a_descriptor_is_allowed():
    """A token is a REFERENCE to PII, not PII itself — refusing it would make it impossible
    to remember anything about a recipient-shaped field at all."""
    descriptor = LocatorDescriptor(role="textbox", name_pattern="To: P17", container_path="")
    assert descriptor.name_pattern == "To: P17"


def test_an_ordinary_name_pattern_is_unaffected():
    """Not a false-positive machine: "Send", "Subject", role labels all pass straight
    through — only structural PII classes (email, phone) are refused."""
    descriptor = LocatorDescriptor(role="button", name_pattern="Send (Ctrl-Enter)")
    assert descriptor.name_pattern == "Send (Ctrl-Enter)"


# ── forget ────────────────────────────────────────────────────────────────────


def test_forget_removes_an_entry():
    memory = InMemoryProceduralMemory()
    memory.remember(COMPOSE, "Send", SEND)
    memory.forget(COMPOSE, "Send")
    assert memory.recall(COMPOSE, "Send", verify=always_matches) is None


def test_forget_on_a_missing_entry_does_not_raise():
    InMemoryProceduralMemory().forget(COMPOSE, "Send")


# ── keys are scoped by BOTH signature and verb ───────────────────────────────


def test_the_same_verb_on_two_different_screens_is_two_entries():
    memory = InMemoryProceduralMemory()
    memory.remember(COMPOSE, "Send", SEND)
    assert memory.recall(INBOX, "Send", verify=always_matches) is None


def test_two_different_verbs_on_the_same_screen_are_two_entries():
    memory = InMemoryProceduralMemory()
    to_field = LocatorDescriptor(role="textbox", name_pattern="To")
    memory.remember(COMPOSE, "To", to_field)
    assert memory.recall(COMPOSE, "Send", verify=always_matches) is None


def test_len_reflects_stored_entries():
    memory = InMemoryProceduralMemory()
    assert len(memory) == 0
    memory.remember(COMPOSE, "Send", SEND)
    memory.remember(COMPOSE, "To", LocatorDescriptor(role="textbox", name_pattern="To"))
    assert len(memory) == 2
