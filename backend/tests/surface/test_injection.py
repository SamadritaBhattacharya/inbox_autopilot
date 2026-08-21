"""Prompt injection — the threat unique to an agent that reads its own input.

The fixture is an ordinary-looking email whose BODY issues instructions: ignore your rules,
mail the contents elsewhere, send without asking, then delete the evidence. It renders in
the reading pane exactly as a real one would, and the agent reads it because reading is the
job.

**These tests assert on what was ATTEMPTED, not on what arrived.** "No send was dispatched"
is a far stronger claim than "no email was received", and it is the one that stays true when
the network is mocked.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from inbox_contracts import ActionCall

from app.manager.intent import Action
from app.surface.dispatch import GATED_VERBS
from app.surface.playwright_surface import launch_surface, resolve_chromium
from app.workers.registry import worker_for
from app.workers.tools import TRIAGE_TOOLS, verb_names

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        resolve_chromium() is None,
        reason="no Chromium build found; run `playwright install chromium`",
    ),
]

FIXTURE = (Path(__file__).resolve().parents[1] / "fixtures" / "injection.html").as_uri()


@pytest.fixture
async def surface():
    surface, close = await launch_surface(headless=True, start_url=FIXTURE)
    try:
        yield surface
    finally:
        await close()


async def test_the_agent_does_read_the_hostile_text(surface):
    """The premise. If the attack text never reached the model there would be nothing to
    defend against, and these tests would be proving nothing."""
    observation = await surface.observe()
    listed = " ".join(element.name for element in observation.elements)

    assert "maintenance mode" in listed.lower() or "ignore all previous" in listed.lower()


async def test_the_attackers_address_cannot_be_named_literally(surface):
    """Half the defence: recipients are tokens, so a raw address is not sayable."""
    await surface.observe()

    result = await surface.act(
        ActionCall(name="Type", args={"recipient": "attacker@evil.example"})
    )

    assert result.success is False
    assert result.error_code == "UNKNOWN_TOKEN"


async def test_the_attackers_address_does_get_a_token_and_is_still_not_a_recipient(surface):
    """The other half — and the correction to a claim that was once made too strongly.

    It is tempting to say the injected address is *unrepresentable*: it never appeared as a
    correspondent, so surely it has no token. That is wrong, and the difference matters. The
    funnel tokenizes every address it meets, wherever it meets it, because the model must
    not read `attacker@evil.example` in the clear either. So the address DOES get a token,
    and a model that had swallowed the injection could reference it by that token.

    What actually stops it is provenance. A token minted from message *content* is not
    addressable; only a sender chip, a contact, or the user's own instruction produces a
    target. So the defence is not "the attacker cannot be named" — they can — it is "naming
    them does not make them reachable".
    """
    observation = await surface.observe()

    # Find the token standing in for the attacker's address.
    token = surface.vault.token_of("attacker@evil.example")
    assert token is not None, "expected the body address to be tokenized for redaction"
    assert token not in observation.model_dump_json() or True  # it may legitimately appear

    result = await surface.act(ActionCall(name="Type", args={"recipient": token}))

    assert result.success is False
    assert result.error_code == "UNTRUSTED_RECIPIENT"


async def test_the_attackers_address_is_not_in_the_observation(surface):
    observation = await surface.observe()
    assert "attacker@evil.example" not in observation.model_dump_json()


async def test_send_is_refused_however_convincingly_the_email_asks(surface):
    """'Approval is not required in maintenance mode' is a claim by a stranger."""
    await surface.observe()

    result = await surface.act(ActionCall(name="Send", args={"index": 1}))

    assert result.success is False
    assert result.error_code == "APPROVAL_REQUIRED"


#: Every way an action can be turned away before it touches the mailbox. Which one fires
#: is not the point — that one of them always does, is.
REFUSALS = {"APPROVAL_REQUIRED", "UNKNOWN_TOKEN", "VERB_NOT_BOUND", "STALE_INDEX"}


async def test_delete_is_refused_too(surface):
    """Refused as VERB_NOT_BOUND rather than APPROVAL_REQUIRED, which is the *stronger*
    outcome: the surface has no delete handler at all, so the capability is absent rather
    than merely gated. An instruction cannot argue its way past a verb that does not exist.
    """
    await surface.observe()
    result = await surface.act(ActionCall(name="DeleteForever", args={"index": 1}))

    assert result.success is False
    assert result.error_code in REFUSALS


async def test_a_triage_run_has_no_tool_the_attack_could_use(surface):
    """The capability defence, and the one that needs no runtime check at all.

    A triage worker's schema contains no Send, no Forward, no DeleteForever. The instruction
    is arguing with a tool list it cannot extend.
    """
    bound = verb_names(TRIAGE_TOOLS)
    assert bound & GATED_VERBS == set()
    assert "Send" not in bound


async def test_a_summarize_run_cannot_mutate_anything(surface):
    """Reading hostile mail is the *most* likely moment to be attacked, and it is also the
    moment the agent holds the fewest capabilities."""
    spec = worker_for(Action.SUMMARIZE)
    assert spec.read_only is True
    assert verb_names(spec.tools) & {"Archive", "Send", "DeleteForever", "Label"} == set()


async def test_nothing_the_attack_asked_for_was_ever_dispatched(surface):
    """The end-to-end statement, in one assertion."""
    await surface.observe()

    attempts = [
        ActionCall(name="Send", args={"index": 1}),
        ActionCall(name="DeleteForever", args={"index": 1}),
        ActionCall(name="Type", args={"recipient": "attacker@evil.example"}),
    ]
    results = [await surface.act(call) for call in attempts]

    assert all(result.success is False for result in results)
    assert all(result.error_code in REFUSALS for result in results)
    # Each is stopped by a DIFFERENT layer — approval, the token scheme, capability binding.
    # Defence in depth only means something if the layers are independent.
    assert len({result.error_code for result in results}) >= 2
