"""Which recipients are actually new — the arithmetic behind the duplicate guard.

Separated from the browser-backed tests deliberately: reading the live To field needs
Chrome, but deciding "which of these do I still need to type?" is pure and deserves
exhaustive cases rather than a couple of representative ones.

The property that matters: a duplicate is dropped, a genuinely new address is kept, and
"add Bob to the email that already has Alice" therefore keeps working. A guard that refused
the whole action whenever ANY address was already present would have fixed the double-add
by breaking the add-later case.
"""
from __future__ import annotations

import pytest

from app.surface.playwright_surface import new_recipients


def test_an_address_already_present_is_dropped():
    assert new_recipients("priya@corp.com", {"priya@corp.com"}) == []


def test_a_new_address_is_kept():
    assert new_recipients("alex@corp.com", {"priya@corp.com"}) == ["alex@corp.com"]


def test_only_the_new_one_survives_a_mixed_list():
    """THE add-later case. Alice is in the field; the user asks to add Bob. Bob is typed,
    Alice is not typed a second time."""
    assert new_recipients(
        "priya@corp.com, alex@corp.com", {"priya@corp.com"}
    ) == ["alex@corp.com"]


def test_an_empty_field_keeps_everything():
    assert new_recipients("a@x.com, b@x.com", set()) == ["a@x.com", "b@x.com"]


def test_order_is_preserved():
    assert new_recipients("c@x.com, a@x.com, b@x.com", set()) == [
        "c@x.com",
        "a@x.com",
        "b@x.com",
    ]


@pytest.mark.parametrize(
    "typed,present",
    [
        ("Priya@Corp.com", {"priya@corp.com"}),
        ("priya@corp.com", {"PRIYA@CORP.COM".lower()}),
        ("  priya@corp.com  ", {"priya@corp.com"}),
    ],
)
def test_case_and_whitespace_do_not_make_a_different_person(typed, present):
    assert new_recipients(typed, present) == []


def test_a_semicolonless_single_address_is_not_split_oddly():
    assert new_recipients("priya@corp.com", set()) == ["priya@corp.com"]


def test_empty_text_yields_nothing():
    assert new_recipients("", {"a@x.com"}) == []


def test_trailing_separators_are_ignored():
    assert new_recipients("a@x.com, , b@x.com,", set()) == ["a@x.com", "b@x.com"]
