"""A vault token must never reach Gmail's To field as literal characters.

**The live failure.** A human added a second recipient in the approval box by typing a
space between the two addresses. That separator survived into the slot as `"P1 P3"`, the
dispatcher's token check understood commas only, so the value was never resolved — and the
executor typed the characters `P1 P3` into the To field as if they were an address.

Three layers were fixed so this cannot happen (canonical form at the producer, whitespace
in the splitter, and the check here). This one is the last of them, and it is the only one
that holds when the other two are wrong: `text` reaching the To field is post-resolution,
so a token still in it PROVES resolution did not happen.

A literal ADDRESS in this field is already refused (`UNKNOWN_TOKEN`). A literal token is
the same class of mistake and gets the same answer — refused, typed, and visible in the
trajectory, rather than a garbage recipient shown to a human as a real draft.
"""
from __future__ import annotations

import pytest
from inbox_contracts import ActionCall, Element, MailContext, Observation, Viewport

from app.surface.dispatch import ResolvedAction
from app.surface.playwright_surface import PlaywrightEmailSurface


def compose_observation() -> Observation:
    return Observation(
        context_id="T1",
        title="Compose",
        viewport=Viewport(width=1280, height=800),
        elements=[Element(index=50, role="textbox", name="To")],
        mail=MailContext(
            view="compose", composeOpen=True, toIndex=50, subjectIndex=61, bodyIndex=70
        ),
    )


def surface() -> PlaywrightEmailSurface:
    """Bound to nothing. The guard runs before the page is touched, which is the point —
    a refused write never reaches the browser at all."""
    instance = PlaywrightEmailSurface.__new__(PlaywrightEmailSurface)
    instance._last_observation = compose_observation()
    return instance


def typing(text: str, index: int = 50) -> ResolvedAction:
    return ResolvedAction(call=ActionCall(name="Type", args={"index": index, "text": text}))


@pytest.mark.anyio
@pytest.mark.parametrize("text", ["P1 P3", "P1", "P1 P3 P7", "priya P1", "P17"])
async def test_an_unresolved_token_never_reaches_the_To_field(text):
    result = await surface()._do_type(typing(text))

    assert result.success is False
    assert result.error_code == "UNRESOLVED_TOKEN"
    assert "literal text" in result.reason


@pytest.mark.anyio
async def test_the_refusal_says_how_to_do_it_correctly():
    """A rejection the model cannot act on just costs a turn."""
    result = await surface()._do_type(typing("P1 P3"))

    assert "recipient" in result.reason
    assert "P1, P3" in result.reason


@pytest.mark.anyio
async def test_the_guard_is_scoped_to_the_RECIPIENT_field():
    """A body legitimately says "the P2 bug" — refusing that would break ordinary prose,
    which is a worse failure than the one being fixed and much harder to notice."""
    body = await surface()._do_type(typing("We shipped the P2 bug fix.", index=70))

    assert body.error_code != "UNRESOLVED_TOKEN"


@pytest.mark.anyio
async def test_a_subject_mentioning_a_token_is_not_refused_either():
    subject = await surface()._do_type(typing("P1 planning", index=61))

    assert subject.error_code != "UNRESOLVED_TOKEN"


# ── the point of all of it: the REAL address is what gets typed ─────────────
#
# Refusing a value that could not be resolved is only half the job. The other half is that
# a value that CAN be resolved becomes the actual address — a token is bookkeeping, and the
# mail has to reach a person.
#
# Asserted at the two stages that exist. `_text_for` SUBSTITUTES, and it deliberately
# preserves whatever separated the tokens (it serves email bodies too, where rewriting
# punctuation would be vandalism). `_do_type` then re-joins recipients with commas, which
# is what Gmail needs to build a chip per address — proved end to end in
# `test_duplicate_recipient.py`, against a real browser.


def resolved(text: str, vault, **extra) -> str:
    """What the substitution stage produces for this call."""
    from inbox_contracts import ActionCall

    from app.surface.dispatch import ActionValidator

    observation = compose_observation()
    validator = ActionValidator(
        vault=vault,
        geometry={50: (10.0, 20.0), 61: (10.0, 40.0), 70: (10.0, 60.0)},
        bound_verbs={"Type"},
        observation=observation,
    )
    action = validator.validate(
        ActionCall(name="Type", args={"index": 50, **({"text": text} if text else {}), **extra})
    )
    return surface()._text_for(action)


@pytest.fixture
def vault():
    from app.security.vault import SessionPiiVault

    store = SessionPiiVault()
    store.trust("priya@corp.com")
    store.trust("alex@corp.com")
    return store


@pytest.mark.parametrize("text", ["P1, P2", "P1 P2", "P1;P2", "P1,P2", "P1  P2"])
def test_two_tokens_become_two_real_addresses(text, vault):
    """THE thing to get right: `P1` reaches Gmail as the address, never as "P1"."""
    out = resolved(text, vault)

    assert "priya@corp.com" in out
    assert "alex@corp.com" in out


@pytest.mark.parametrize("text", ["P1, P2", "P1 P2", "P1;P2", "P1,P2", "P1  P2"])
def test_no_token_survives_resolution(text, vault):
    """A token still in the text at this point is exactly what put "P1 P3" into the To
    field, and is what the guard above refuses."""
    from app.security.patterns import TOKEN_RE

    assert not TOKEN_RE.search(resolved(text, vault))


def test_one_token_becomes_one_real_address(vault):
    assert resolved("P1", vault) == "priya@corp.com"


def test_the_recipient_argument_resolves_the_same_way(vault):
    """`Type(recipient=…)` and `Type(text=…)` are two ways to say the same thing, and the
    model uses both."""
    out = resolved("", vault, recipient="P1 P2")

    assert "priya@corp.com" in out and "alex@corp.com" in out


def test_an_unknown_token_is_refused_rather_than_typed(vault):
    """A token with nothing behind it must never become literal text either."""
    from app.surface.dispatch import DispatchRejected

    with pytest.raises(DispatchRejected) as rejected:
        resolved("P1, P99", vault)

    assert rejected.value.error_code == "UNKNOWN_TOKEN"


def test_the_resolved_addresses_are_ready_for_a_chip_each(vault):
    """What `_do_type` hands the keyboard: one comma-separated list, whatever the model
    typed between the tokens. Gmail commits a chip on a comma; two addresses joined by a
    space are one unbroken string to it, and a single Enter makes them one bad recipient."""
    from app.surface.playwright_surface import new_recipients

    assert new_recipients(resolved("P1 P2", vault), set()) == [
        "priya@corp.com",
        "alex@corp.com",
    ]
