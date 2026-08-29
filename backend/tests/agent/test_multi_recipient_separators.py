"""Two recipients must mean two recipients, whatever the human typed between them.

**The live failure.** Somebody added a second address in the approval box by typing a
SPACE between them — the natural way — and Gmail's To field ended up holding the literal
characters `P1 P3`. Not an address. Not a chip. Text.

One value, one separator, and THREE independent places that only understood commas:

  1. `_retyped_recipient` minted by string replacement, so it preserved the space and
     produced the slot `"P1 P3"` where the instruction path (`_recipient_change`) would
     have produced `"P1, P3"`. Two producers of one value, one of them normalizing.
  2. `dispatch._split_tokens` split on commas and semicolons, so `"P1 P3"` was a single
     part that was not a token — the value was never recognised as resolvable, and the raw
     characters were typed.
  3. `slots.split_recipients` split on commas, `and`, and `&`, so `"P1 P3"` counted as ONE
     person. `_delivery_instruction` then said nothing at all about how to send to two
     people — and under "separate" it would have enumerated `1. P1 P3`, one email to both.

Only the third is silent, and it is the worst: nothing fails, the mail just goes out wrong.
These tests pin all three, plus the executor's last-resort refusal.
"""
from __future__ import annotations

import pytest

from app.manager.intent import Action, TaskIntent
from app.manager.slots import recipient_count, resolved_delivery_mode, split_recipients
from app.security.vault import SessionPiiVault
from app.surface.dispatch import _is_all_tokens, _split_tokens
from app.workers.approval_gate import _recipient_change, _replace_recipient, _retyped_recipient

SHOWN = "To:      Priya Nair <priya.nair@corp.com>\nSubject: Friday demo\n\nIt moved to 4pm."


# ── the dispatcher: is this value resolvable? ───────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "P1, P3",  # what the instruction path produces
        "P1 P3",  # THE regression: a human's space
        "P1;P3",  # a semicolon, as mail clients use
        "P1,P3",  # no space after the comma
        "P1  P3",  # doubled whitespace
        " P1 , P3 ",  # padded every way
        "P1\nP3",  # a newline, from a pasted list
        "P1, P3, P7",  # three
        "P1",  # one is still all-tokens
    ],
)
def test_a_value_made_only_of_tokens_is_resolvable_however_it_is_punctuated(value):
    assert _is_all_tokens(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "the P2 bug is fixed",  # prose that HAPPENS to contain a token
        "P1 and P3",  # a word between them is not a separator here
        "alex@corp.com",  # a literal address is never resolvable
        "P1, alex@corp.com",  # half tokens is not all tokens
        "Priya",  # a name
        "",
        "   ",
    ],
)
def test_anything_else_is_not_treated_as_tokens(value):
    """The check has to stay narrow. Substituting inside prose would rewrite "the P2 bug"
    into somebody's address — a far worse failure than the one being fixed."""
    assert _is_all_tokens(value) is False


def test_the_splitter_keeps_order_and_drops_nothing():
    assert _split_tokens("P7 P1, P3") == ["P7", "P1", "P3"]


# ── the gate: what the human typed into the To line ─────────────────────────


def test_a_second_recipient_added_with_a_SPACE_becomes_canonical_tokens():
    """THE regression, at the producer that got it wrong."""
    vault = SessionPiiVault()
    edited = SHOWN.replace("Priya Nair <priya.nair@corp.com>", "priya@corp.com alex@corp.com")

    result = _retyped_recipient(SHOWN, edited, vault)

    assert result == "P1, P2", "the human's separator survived into the slot"
    assert vault.resolve("P1") == "priya@corp.com"
    assert vault.resolve("P2") == "alex@corp.com"
    assert vault.is_addressable("P2"), "a recipient the dispatcher would refuse"


@pytest.mark.parametrize("separator", [" ", ", ", ",", "; ", " and ", "  "])
def test_every_separator_a_person_might_type_gives_the_same_result(separator):
    vault = SessionPiiVault()
    edited = SHOWN.replace(
        "Priya Nair <priya.nair@corp.com>", f"priya@corp.com{separator}alex@corp.com"
    )

    assert _retyped_recipient(SHOWN, edited, vault) == "P1, P2"


def test_three_recipients_all_survive():
    vault = SessionPiiVault()
    edited = SHOWN.replace(
        "Priya Nair <priya.nair@corp.com>", "a@corp.com b@corp.com, c@corp.com"
    )

    assert _retyped_recipient(SHOWN, edited, vault) == "P1, P2, P3"


def test_the_same_address_typed_twice_becomes_one_recipient():
    """Otherwise the mail is addressed to somebody twice, which Gmail shows as two chips."""
    vault = SessionPiiVault()
    edited = SHOWN.replace("Priya Nair <priya.nair@corp.com>", "a@corp.com a@corp.com")

    assert _retyped_recipient(SHOWN, edited, vault) == "P1"


def test_a_bare_NAME_is_still_passed_through_for_autocomplete():
    """Gmail's To field completes from contacts; a name is how a person does this."""
    vault = SessionPiiVault()
    edited = SHOWN.replace("Priya Nair <priya.nair@corp.com>", "Biyash")

    assert _retyped_recipient(SHOWN, edited, vault) == "Biyash"


def test_a_name_beside_an_address_is_left_alone_rather_than_half_converted():
    """Returning just the token would silently DROP the person named only by name."""
    vault = SessionPiiVault()
    edited = SHOWN.replace("Priya Nair <priya.nair@corp.com>", "Biyash, alex@corp.com")

    result = _retyped_recipient(SHOWN, edited, vault)

    assert "Biyash" in result
    assert "alex@corp.com" not in result, "a raw address would reach the model"


def test_both_producers_of_this_value_agree_on_the_format():
    """`_recipient_change` (an instruction) and `_retyped_recipient` (the box) feed the same
    slot and the same instruction. One normalizing and the other not IS the bug."""
    vault = SessionPiiVault()
    vault.trust("a@corp.com")
    vault.trust("b@corp.com")
    edited = SHOWN.replace("Priya Nair <priya.nair@corp.com>", "a@corp.com b@corp.com")

    assert _retyped_recipient(SHOWN, edited, vault) == _recipient_change("send it to P1, P2")


# ── the slots layer: how many people is that? ───────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("P1, P2", ["P1", "P2"]),
        ("P1 P2", ["P1", "P2"]),  # THE silent one
        ("P1;P2", ["P1", "P2"]),
        ("P1 P2 P3", ["P1", "P2", "P3"]),
        ("P1  P2", ["P1", "P2"]),
        ("P1, P1", ["P1"]),
        ("P1", ["P1"]),
        ("", []),
    ],
)
def test_tokens_split_on_whitespace(value, expected):
    assert split_recipients(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Priya Nair", ["Priya Nair"]),  # a NAME must not be split on its space
        ("Biyash and Priya", ["Biyash", "Priya"]),
        ("Priya Nair, Alex Kim", ["Priya Nair", "Alex Kim"]),
        ("alice@x.com and bob@y.com", ["alice@x.com", "bob@y.com"]),
    ],
)
def test_names_are_never_split_on_their_spaces(value, expected):
    """The reason whitespace splitting is scoped to all-token values: half the people in a
    mailbox have two-word names, and splitting "Priya Nair" invents a recipient."""
    assert split_recipients(value) == expected


def test_the_count_and_the_split_agree():
    """They disagreed: `recipient_count("P1 P2")` said 2 (it counts tokens) while
    `split_recipients` said 1. So the gate ASKED together-or-separately, got an answer, and
    then the instruction that answer feeds was never emitted."""
    for value in ("P1 P2", "P1, P2", "P1 P2 P3", "P1"):
        assert recipient_count(value) == len(split_recipients(value)), value


# ── the instruction the worker reads ────────────────────────────────────────


def _intent(recipients: str, **slots) -> TaskIntent:
    return TaskIntent(
        action=Action.SEND_EMAIL,
        slots={"recipient_identity": recipients, "topic": "the demo", **slots},
        action_confidence=0.95,
    )


def test_two_recipients_typed_with_a_space_still_get_a_delivery_instruction():
    """The silent failure, end to end: no instruction at all meant the worker was never
    told whether this was one email or two."""
    from app.agent.state import AgentState
    from app.workers.rendering import task_block

    state = AgentState(task="email them", thread_id="mr-1", intent=_intent("P1 P2"))
    block = task_block(state)

    assert "P1" in block and "P2" in block
    assert "TOGETHER" in block or "SEPARATELY" in block


def test_separate_delivery_enumerates_each_person_individually():
    """Under the old split this read "1. P1 P2" — one email to both, which is precisely
    what the human said they did not want."""
    from app.agent.state import AgentState
    from app.workers.rendering import task_block

    state = AgentState(
        task="email them",
        thread_id="mr-2",
        intent=_intent("P1 P2", delivery_mode="separately please"),
    )
    block = task_block(state)

    assert "SEPARATELY" in block
    assert "1. P1" in block and "2. P2" in block
    assert "1. P1 P2" not in block, "two people were enumerated as one"


def test_together_delivery_names_everyone_in_one_email():
    from app.agent.state import AgentState
    from app.workers.rendering import task_block

    state = AgentState(
        task="email them",
        thread_id="mr-3",
        intent=_intent("P1 P2", delivery_mode="one email please"),
    )
    block = task_block(state)

    assert "TOGETHER" in block
    assert "P1, P2" in block


def test_the_delivery_mode_survives_the_separator():
    assert resolved_delivery_mode(_intent("P1 P2", delivery_mode="one at a time")) == "separate"
    assert resolved_delivery_mode(_intent("P1 P2", delivery_mode="together")) == "together"


def test_the_recipient_instruction_names_every_token():
    """Two tokens in the slot must be two tokens in what the worker is told to type."""
    from app.agent.state import AgentState

    text = _replace_recipient(AgentState(task="t", thread_id="mr-4"), "P1, P2")

    assert "P1, P2" in text
    assert "Clear the To field" in text
