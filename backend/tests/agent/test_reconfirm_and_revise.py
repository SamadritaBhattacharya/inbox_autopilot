"""Consent covers WORDS, and a correction edits rather than rewrites.

Two complaints, one underlying cause: the system treated a send as an action on a button
instead of an action on an email.

- The approval fingerprint was `Send|index=108` — where the button is, not what it says. One
  "yes" therefore authorised that button for the rest of the run, so an edited body could
  go out under consent given for different words.
- The correction was handed to the loop as free text, which retyped the whole body and
  quietly reworded everything the human had already accepted.
"""
from __future__ import annotations

import pytest
from inbox_contracts import ActionCall

from app.llm.base import LLMResult
from app.manager.draft import Draft
from app.manager.writer import build_reviser
from app.surface.dispatch import approval_fingerprint
from tests.fakes.fake_llm import FakeLLMClient, ok

ORIGINAL = Draft(
    subject="Afternoon motivation",
    body="Good afternoon. The hard part is starting. Keep going!",
    tone="warm",
)


def send(index: int = 108) -> ActionCall:
    return ActionCall(name="Send", args={"index": index})


# ── every send is confirmed against what it actually says ───────────────────


def test_editing_the_body_invalidates_an_earlier_approval():
    """The complaint, stated as a test: correcting the text must force a fresh confirmation
    rather than riding on the previous yes."""
    approved = {approval_fingerprint(send(), "To: P1\nSubject: X\n\nfirst version")}

    later = approval_fingerprint(send(), "To: P1\nSubject: X\n\nSECOND version")

    assert later not in approved


def test_changing_only_the_subject_also_invalidates_it():
    before = approval_fingerprint(send(), "Subject: Afternoon motivation\n\nbody")
    after = approval_fingerprint(send(), "Subject: Quick hello\n\nbody")

    assert before != after


def test_changing_the_recipient_invalidates_it():
    """The original guarantee, which must survive the change."""
    before = approval_fingerprint(send(), "To: alice@x.com\n\nbody")
    after = approval_fingerprint(send(), "To: mallory@evil.com\n\nbody")

    assert before != after


def test_an_unchanged_draft_keeps_its_approval():
    """The gate re-executes on resume. If an untouched draft fingerprinted differently, the
    human would be asked twice for one decision — and a gate that nags gets clicked
    through."""
    preview = "To: P1\nSubject: X\n\nbody"

    assert approval_fingerprint(send(), preview) == approval_fingerprint(send(), preview)


# ── a correction edits; it does not regenerate ──────────────────────────────


def revision(**fields) -> LLMResult:
    import json

    payload = {"subject": ORIGINAL.subject, "body": ORIGINAL.body, "tone": ORIGINAL.tone}
    payload.update(fields)
    return ok(json.dumps(payload))


async def test_the_reviser_is_given_the_existing_text():
    """It cannot preserve what it was never shown. This is the whole mechanism: the old
    draft goes in, so only the requested part comes back different."""
    llm = FakeLLMClient([revision(body=ORIGINAL.body + "\n\nRegards")])
    revise = build_reviser(llm)

    await revise(ORIGINAL, "add regards")

    brief = " ".join(m.content for _, messages, _ in llm.requests for m in messages)
    assert ORIGINAL.body in brief
    assert "add regards" in brief


async def test_a_subject_only_change_leaves_the_body_alone():
    llm = FakeLLMClient([revision(subject="Quick hello")])

    revised = await build_reviser(llm)(ORIGINAL, "change the subject to Quick hello")

    assert revised.subject == "Quick hello"
    assert revised.body == ORIGINAL.body, "the body was not the subject of the correction"


async def test_a_failed_revision_keeps_the_draft_it_had():
    """A blank email would be a far worse outcome than an unapplied edit — the human is
    partway through approving this."""
    llm = FakeLLMClient([ok("sorry, I could not do that")])

    revised = await build_reviser(llm)(ORIGINAL, "add regards")

    assert revised == ORIGINAL


@pytest.mark.parametrize(
    "instruction", ["add regards", "change the last sentence", "make the subject shorter"]
)
async def test_a_revision_is_never_empty(instruction):
    llm = FakeLLMClient([revision()])

    revised = await build_reviser(llm)(ORIGINAL, instruction)

    assert revised.body.strip()
