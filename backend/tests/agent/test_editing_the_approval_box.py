"""Editing the draft in the approval box, end to end through the gate node.

**The reported bug.** Retyping the email in the card and pressing *Apply & review* did
nothing — no change, and no fresh confirmation — while typing the same correction as an
instruction and pressing *Revise* worked. Both paths send the identical decision, so the
difference was never in the edit: it was in the ASK that follows it.

The request id was `Send|index=108` — the button, not the words. So the gate re-asked under
an id the human had already answered, and two independent layers dropped it: the emitter as
a replay of a pending card, and the cockpit as already-decided. The run then sat at an
interrupt with nothing on screen, which reads exactly like "my edit was ignored". *Revise*
appeared to work only because rewriting the body shifted Gmail's element indices often
enough to mint a new id by accident.

The other half of the report: a recipient typed into the box's `To:` line was read for the
subject and body and ignored for the recipient, so a retargeted email went to the original
address anyway.
"""
from __future__ import annotations

import pytest
from inbox_contracts import ActionCall, Element, MailContext, Observation, Viewport

from app.agent.state import AgentState
from app.events.emitter import EventEmitter
from app.events.sink import BufferSink
from app.manager.draft import Draft
from app.manager.intent import Action, TaskIntent
from app.security.vault import SessionPiiVault
from app.workers import approval_gate as gate_module
from app.workers.approval_gate import build_approval_gate_node
from tests.fakes.fake_surface import FakeEmailSurface

SHOWN = "To:      Priya Nair <priya.nair@corp.com>\nSubject: Friday demo\n\nIt moved to 4pm."
DRAFT = Draft(subject="Friday demo", body="It moved to 4pm.", tone="professional")


def compose_observation() -> Observation:
    """A compose window with all three fields on screen, as the funnel would report it."""
    return Observation(
        context_id="T1",
        title="Compose",
        viewport=Viewport(width=1280, height=800),
        elements=[
            Element(index=50, role="textbox", name="To"),
            Element(index=61, role="textbox", name="Subject"),
            Element(index=70, role="textbox", name="Message Body"),
            Element(index=108, role="button", name="Send"),
        ],
        mail=MailContext(
            view="compose",
            composeOpen=True,
            toFilled=True,
            subjectFilled=True,
            bodyFilled=True,
            toIndex=50,
            subjectIndex=61,
            bodyIndex=70,
        ),
    )


def a_state(**overrides) -> AgentState:
    base = dict(
        task="email P1 about the demo",
        thread_id="box-1",
        intent=TaskIntent(
            action=Action.SEND_EMAIL,
            slots={"recipient_identity": "P1", "topic": "the demo"},
            action_confidence=0.95,
        ),
        observation=compose_observation(),
        last_action=ActionCall(name="Send", args={"index": 108}),
        draft=DRAFT,
    )
    base.update(overrides)
    return AgentState(**base)


async def decide(payload: dict, *, monkeypatch, vault=None, revise=None, preview=SHOWN):
    """Run the gate once with a scripted human decision, and return (delta, surface, sink)."""
    monkeypatch.setattr(gate_module, "interrupt", lambda _request: payload)
    surface = FakeEmailSurface(preview=preview)
    sink = BufferSink()
    node = build_approval_gate_node(
        surface, EventEmitter(sink), revise=revise, vault=vault
    )
    return await node(a_state()), surface, sink


def message_text(delta: dict) -> str:
    return "\n".join(m.content for m in delta.get("messages", []))


# ── the ask has to change when the words change ─────────────────────────────


def test_the_request_id_follows_the_words_not_the_button():
    """THE bug. A decision is about an email; two different emails cannot share an id.

    Asserted on `approval_fingerprint` because that is what the id is built from — if the
    fingerprint moves and the id does not, the card is dropped before anyone sees it.
    """
    from app.surface.dispatch import approval_fingerprint

    send = ActionCall(name="Send", args={"index": 108})
    first = approval_fingerprint(send, SHOWN)
    edited = approval_fingerprint(send, SHOWN.replace("4pm", "5pm"))

    assert first != edited
    # And the id has to carry that difference. Truncating the fingerprint to sixteen
    # characters kept `Send|index=108|c` and dropped the content hash entirely.
    assert first[:16] == edited[:16], "the regression this guards is a truncation, not a hash"


@pytest.mark.anyio
async def test_the_id_the_gate_emits_changes_with_the_draft(monkeypatch):
    ids = []
    for preview in (SHOWN, SHOWN.replace("4pm", "5pm")):
        _, _, sink = await decide(
            {"verdict": "approve"}, monkeypatch=monkeypatch, preview=preview
        )
        ids += [
            event.data["requestId"]
            for event in sink.events
            if event.event == "approval_request"
        ]

    assert len(ids) == 2
    assert ids[0] != ids[1], "the gate re-asked under the id the human already answered"


@pytest.mark.anyio
async def test_a_resume_of_the_SAME_draft_keeps_one_id(monkeypatch):
    """The property the old id was protecting, which must survive the fix.

    This node re-executes from the top whenever the run resumes. If an untouched draft got
    a fresh id each time, one pending decision would show up as several cards.
    """
    ids = []
    for _ in range(2):
        _, _, sink = await decide({"verdict": "approve"}, monkeypatch=monkeypatch)
        ids += [e.data["requestId"] for e in sink.events if e.event == "approval_request"]

    assert ids[0] == ids[1]


@pytest.mark.anyio
async def test_a_decision_reopens_the_replay_window(monkeypatch):
    """The emitter's dedup must not outlive the decision it was protecting.

    It exists to swallow the same pending card re-emitted on resume. Once a human has
    decided, the next request is a new question — and dropping it left the run waiting at
    an interrupt for a card that had been discarded one layer below the UI.
    """
    sink = BufferSink()
    emitter = EventEmitter(sink)
    card = dict(kind="send", summary="Send this email", preview=SHOWN, expires_at="z")

    await emitter.approval_request(request_id="ap-1", **card)
    await emitter.approval_request(request_id="ap-1", **card)  # a replay — dropped
    await emitter.approval_result("ap-1", "edit")
    await emitter.approval_request(request_id="ap-1", **card)  # a new ask — must land

    asks = [e for e in sink.events if e.event == "approval_request"]
    assert len(asks) == 2


# ── the body, retyped in the box ────────────────────────────────────────────


@pytest.mark.anyio
async def test_retyped_text_is_applied_verbatim_with_no_model_call(monkeypatch):
    async def never(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("the reviser was called on text the human already wrote")

    delta, _, _ = await decide(
        {"verdict": "edit", "edit": "", "editedPreview": SHOWN.replace("4pm", "5pm")},
        monkeypatch=monkeypatch,
        revise=never,
    )

    assert delta["draft"].body == "It moved to 5pm."
    assert delta["draft"].subject == "Friday demo", "an untouched field was rewritten"
    assert delta["last_action"] is None, "the send must not be dispatched"


@pytest.mark.anyio
async def test_a_subject_edited_in_the_box_is_named_as_stale(monkeypatch):
    delta, _, _ = await decide(
        {
            "verdict": "edit",
            "edit": "",
            "editedPreview": SHOWN.replace("Friday demo", "Friday demo — moved"),
        },
        monkeypatch=monkeypatch,
    )

    assert delta["draft"].subject == "Friday demo — moved"
    text = message_text(delta)
    assert "Subject [61]" in text
    assert "Body [70]" not in text, "an unchanged body must not be retyped"


@pytest.mark.anyio
async def test_text_that_cannot_be_read_back_says_so(monkeypatch):
    """It used to fall through to "Change it: " with an empty instruction — a human's
    retyped email turned into an instruction that said nothing."""
    delta, _, _ = await decide(
        {"verdict": "edit", "edit": "", "editedPreview": "just some words"},
        monkeypatch=monkeypatch,
    )

    text = message_text(delta)
    assert "could not be read back" in text
    assert "AskUser" in text
    assert "draft" not in delta


# ── the recipient, retyped in the box ───────────────────────────────────────


@pytest.mark.anyio
async def test_a_recipient_typed_into_the_To_line_is_actually_changed(monkeypatch):
    vault = SessionPiiVault()
    original = vault.trust("priya.nair@corp.com")
    delta, _, _ = await decide(
        {
            "verdict": "edit",
            "edit": "",
            "editedPreview": SHOWN.replace("Priya Nair <priya.nair@corp.com>", "alex@corp.com"),
        },
        monkeypatch=monkeypatch,
        vault=vault,
    )

    token = delta["intent"].slots["recipient_identity"]
    assert token != original, "the retargeting was read and thrown away"
    assert vault.resolve(token) == "alex@corp.com"
    assert vault.is_addressable(token), "the dispatcher would refuse a non-addressable token"

    text = message_text(delta)
    assert "chip" in text.lower(), "typing beside the old chip sends to both"
    assert "Do NOT Clear" in text


@pytest.mark.anyio
async def test_the_address_never_reaches_the_worker_in_the_clear(monkeypatch):
    vault = SessionPiiVault()
    delta, _, _ = await decide(
        {
            "verdict": "edit",
            "edit": "",
            "editedPreview": SHOWN.replace("Priya Nair <priya.nair@corp.com>", "alex@corp.com"),
        },
        monkeypatch=monkeypatch,
        vault=vault,
    )

    assert "alex@corp.com" not in message_text(delta)


@pytest.mark.anyio
async def test_changing_the_recipient_and_the_body_together_gives_one_coherent_order(
    monkeypatch,
):
    """Two instructions that contradict each other means one of them is silently dropped.

    The recipient-only message ends "leave the subject and body exactly as they are"; the
    draft-only message ends "leave the recipient alone". Emitting both for a single edit
    that changed both is how half of a correction disappears.
    """
    vault = SessionPiiVault()
    original = vault.trust("priya.nair@corp.com")
    edited = SHOWN.replace("Priya Nair <priya.nair@corp.com>", "alex@corp.com").replace(
        "4pm", "5pm"
    )
    delta, _, _ = await decide(
        {"verdict": "edit", "edit": "", "editedPreview": edited},
        monkeypatch=monkeypatch,
        vault=vault,
    )

    text = message_text(delta)
    assert delta["draft"].body == "It moved to 5pm."
    assert vault.resolve(delta["intent"].slots["recipient_identity"]) == "alex@corp.com"
    assert delta["intent"].slots["recipient_identity"] != original
    assert "Body [70]" in text
    assert "Leave the subject and body exactly as they are" not in text
    assert "Leave the recipient alone" not in text


@pytest.mark.anyio
async def test_an_untouched_To_line_is_not_a_recipient_change(monkeypatch):
    """Editing only the body must not retarget the email."""
    vault = SessionPiiVault()
    delta, _, _ = await decide(
        {"verdict": "edit", "edit": "", "editedPreview": SHOWN.replace("4pm", "5pm")},
        monkeypatch=monkeypatch,
        vault=vault,
    )

    assert "intent" not in delta
    assert "chip" not in message_text(delta).lower()


# ── an instruction, typed alongside ─────────────────────────────────────────


@pytest.mark.anyio
async def test_a_tone_instruction_revises_the_existing_draft(monkeypatch):
    seen: list[tuple[Draft, str]] = []

    async def revise(before: Draft, instruction: str) -> Draft:
        seen.append((before, instruction))
        return Draft(subject=before.subject, body="Warmly — it moved to 4pm.", tone="warm")

    delta, _, _ = await decide(
        {"verdict": "edit", "edit": "make it warmer", "editedPreview": ""},
        monkeypatch=monkeypatch,
        revise=revise,
    )

    assert seen == [(DRAFT, "make it warmer")], "the reviser must see the EXISTING draft"
    assert delta["draft"].body == "Warmly — it moved to 4pm."
    text = message_text(delta)
    assert "make it warmer" in text
    assert "Body [70]" in text


@pytest.mark.anyio
async def test_a_recipient_instruction_does_not_go_to_the_reviser(monkeypatch):
    """There are no words to rewrite. The reviser would find nothing and report success."""

    async def never(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("a recipient change was handed to the draft reviser")

    delta, _, _ = await decide(
        {"verdict": "edit", "edit": "send it to P5 instead", "editedPreview": ""},
        monkeypatch=monkeypatch,
        revise=never,
    )

    assert delta["intent"].slots["recipient_identity"] == "P5"
    assert "chip" in message_text(delta).lower()


@pytest.mark.anyio
async def test_a_raw_address_in_an_instruction_is_never_taken_as_a_recipient(monkeypatch):
    """Fails closed. Addresses are tokenized at the socket, exactly as mid-run feedback is;
    if one ever arrives raw, the gate must not put it in the To field — the dispatcher
    accepts vault tokens only, and a literal address there is refused as `UNKNOWN_TOKEN`."""

    async def revise(before: Draft, _instruction: str) -> Draft:
        return before

    delta, _, _ = await decide(
        {"verdict": "edit", "edit": "send it to alex@corp.com", "editedPreview": ""},
        monkeypatch=monkeypatch,
        revise=revise,
    )

    assert "intent" not in delta, "an untokenized address was accepted as a recipient"
