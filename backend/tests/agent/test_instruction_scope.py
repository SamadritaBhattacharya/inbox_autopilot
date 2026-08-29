"""An instruction typed at the approval card is FOUR different jobs, not one.

Everything a human typed went to the reviser, whose whole prompt is "change only what was
asked and return the rest byte for byte". That is right for "add regards" and wrong for:

  - "scrap this, write about the Q3 numbers instead" — the reviser is built to preserve the
    words being rejected, so what comes back is a hedged edit of an email nobody wants.
  - "why did you phrase it that way?" — a question, silently acted on as a command.
  - "send it to P5 instead" — nothing about the words at all.
  - "make it shorter and send it to P5" — BOTH, and first-match-wins dropped the "shorter".

So the instruction is classified first, and the routing is asserted here. The classifier
judges the WORDS only: it can never move the recipient, which is the one mistake with no
undo.
"""
from __future__ import annotations

import json

import pytest
from inbox_contracts import ActionCall, Element, MailContext, Observation, Viewport

from app.agent.state import AgentState
from app.events.emitter import EventEmitter
from app.events.sink import BufferSink
from app.llm.base import LLMResult, ProviderError
from app.manager.draft import Draft
from app.manager.instruction import EditScope, build_edit_classifier
from app.manager.intent import Action, TaskIntent
from app.security.vault import SessionPiiVault
from app.workers import approval_gate as gate_module
from app.workers.approval_gate import _fallback_scope, build_approval_gate_node
from tests.fakes.fake_llm import FakeLLMClient, ok

SHOWN = "To:      Priya Nair <priya.nair@corp.com>\nSubject: Friday demo\n\nIt moved to 4pm."
DRAFT = Draft(subject="Friday demo", body="It moved to 4pm.", tone="professional")


def scope_reply(kind: str, brief: str = "") -> LLMResult:
    return ok(json.dumps({"kind": kind, "brief": brief}))


# ── the classifier itself ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_it_reads_a_kind_and_a_brief():
    classify = build_edit_classifier(FakeLLMClient([scope_reply("rewrite", "the Q3 numbers")]))

    scope = await classify("scrap this, write about the Q3 numbers")

    assert scope == EditScope(kind="rewrite", brief="the Q3 numbers")


@pytest.mark.anyio
async def test_a_brief_is_dropped_for_every_kind_but_rewrite():
    """A brief on an `adjust` would be a second, competing instruction."""
    classify = build_edit_classifier(FakeLLMClient([scope_reply("adjust", "write about cats")]))

    assert (await classify("make it warmer")).brief == ""


@pytest.mark.anyio
async def test_a_rewrite_with_no_brief_falls_back_to_what_the_human_said():
    """Otherwise the writer is handed nothing to write from."""
    classify = build_edit_classifier(FakeLLMClient([scope_reply("rewrite")]))

    assert (await classify("start again, about the delay")).brief == "start again, about the delay"


@pytest.mark.anyio
async def test_an_empty_instruction_costs_no_call():
    llm = FakeLLMClient([])
    classify = build_edit_classifier(llm)

    assert (await classify("   ")) == EditScope(kind="none")
    assert llm.call_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "reply",
    [
        ok("I think this is an adjustment, probably"),  # no JSON at all
        ok('{"kind": "improve"}'),  # a kind that does not exist
        ok('{"kind": '),  # truncated
        ok('["adjust"]'),  # JSON, wrong shape
        ok(""),  # nothing
    ],
)
async def test_an_unreadable_reply_says_so_rather_than_guessing(reply):
    """`None` is "I could not tell". Guessing `rewrite` here would throw away an email."""
    classify = build_edit_classifier(FakeLLMClient([reply]))

    assert await classify("make it warmer") is None


@pytest.mark.anyio
async def test_a_provider_outage_never_raises_into_the_gate():
    classify = build_edit_classifier(FakeLLMClient([ProviderError("groq", "quota exhausted")]))

    assert await classify("make it warmer") is None


@pytest.mark.anyio
async def test_a_runaway_brief_is_clipped():
    classify = build_edit_classifier(FakeLLMClient([scope_reply("rewrite", "x" * 5000)]))

    assert len((await classify("start again")).brief) == 400


def test_the_fallback_is_the_behaviour_that_shipped_before_the_classifier():
    """A provider outage must cost the new routing and nothing else."""
    assert _fallback_scope(None).kind == "adjust"
    assert _fallback_scope("P5").kind == "none", "a recipient change has no words to revise"


# ── the routing that hangs off it ───────────────────────────────────────────


def compose_observation() -> Observation:
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


def a_state() -> AgentState:
    return AgentState(
        task="email P1 about the demo",
        thread_id="scope-1",
        intent=TaskIntent(
            action=Action.SEND_EMAIL,
            slots={"recipient_identity": "P1", "topic": "the demo"},
            action_confidence=0.95,
        ),
        observation=compose_observation(),
        last_action=ActionCall(name="Send", args={"index": 108}),
        draft=DRAFT,
    )


class Calls:
    """Records which of the two writing paths ran, and with what."""

    def __init__(self) -> None:
        self.revised: list[tuple[Draft, str]] = []
        self.rewritten: list[str] = []

    async def revise(self, draft: Draft, instruction: str) -> Draft:
        self.revised.append((draft, instruction))
        return Draft(subject=draft.subject, body="Revised body.", tone=draft.tone)

    async def rewrite(self, _state: AgentState, brief: str) -> Draft | None:
        self.rewritten.append(brief)
        return Draft(subject="Q3 numbers", body="A different email.", tone="professional")


async def route(
    instruction: str,
    *,
    monkeypatch,
    kind: str | None = "adjust",
    brief: str = "",
    typed: str = "",
    calls: Calls | None = None,
    vault=None,
):
    """Run the gate once with a scripted classification. Returns (delta, calls)."""
    calls = calls or Calls()
    monkeypatch.setattr(
        gate_module,
        "interrupt",
        lambda _request: {
            "verdict": "edit",
            "edit": instruction,
            "editedPreview": typed,
        },
    )

    async def classify(_text: str) -> EditScope | None:
        return None if kind is None else EditScope(kind=kind, brief=brief)

    from tests.fakes.fake_surface import FakeEmailSurface

    node = build_approval_gate_node(
        FakeEmailSurface(preview=SHOWN),
        EventEmitter(BufferSink()),
        revise=calls.revise,
        rewrite=calls.rewrite,
        classify=classify,
        vault=vault,
    )
    return await node(a_state()), calls


def message_text(delta: dict) -> str:
    return "\n".join(m.content for m in delta.get("messages", []))


@pytest.mark.anyio
async def test_adjust_goes_to_the_reviser_with_the_existing_draft(monkeypatch):
    delta, calls = await route("make it warmer", monkeypatch=monkeypatch, kind="adjust")

    assert calls.revised == [(DRAFT, "make it warmer")]
    assert calls.rewritten == []
    assert delta["draft"].body == "Revised body."


@pytest.mark.anyio
async def test_rewrite_goes_to_the_writer_and_never_to_the_reviser(monkeypatch):
    """The reviser preserves what it is given, which is exactly what a rewrite rejects."""
    delta, calls = await route(
        "scrap it, write about Q3", monkeypatch=monkeypatch, kind="rewrite", brief="the Q3 numbers"
    )

    assert calls.rewritten == ["the Q3 numbers"]
    assert calls.revised == []
    assert delta["draft"].subject == "Q3 numbers"


@pytest.mark.anyio
async def test_a_failed_rewrite_keeps_the_draft_the_human_already_has(monkeypatch):
    """An empty compose window is a worse outcome than an unchanged one."""

    class Failing(Calls):
        async def rewrite(self, _state, brief):
            self.rewritten.append(brief)
            return None

    delta, calls = await route(
        "start again", monkeypatch=monkeypatch, kind="rewrite", calls=Failing()
    )

    assert calls.rewritten, "the rewrite was attempted"
    assert "draft" not in delta, "a failed rewrite must not blank the email"
    assert "could not be read back" not in message_text(delta)


@pytest.mark.anyio
async def test_a_question_is_answered_and_the_draft_is_left_alone(monkeypatch):
    delta, calls = await route(
        "why did you phrase it like that?", monkeypatch=monkeypatch, kind="question"
    )

    assert calls.revised == [] and calls.rewritten == []
    assert "draft" not in delta
    text = message_text(delta)
    assert "AskUser" in text
    assert "Do NOT change the draft" in text


@pytest.mark.anyio
async def test_none_touches_nothing(monkeypatch):
    """"ok" is not an instruction to rewrite an email."""
    delta, calls = await route("ok", monkeypatch=monkeypatch, kind="none")

    assert calls.revised == [] and calls.rewritten == []
    assert "draft" not in delta


# ── compound instructions: the half that used to get dropped ────────────────


@pytest.mark.anyio
async def test_shorter_AND_a_new_recipient_does_both(monkeypatch):
    """First-match-wins changed the recipient and silently threw away "make it shorter"."""
    delta, calls = await route(
        "make it shorter and send it to P5", monkeypatch=monkeypatch, kind="adjust"
    )

    assert calls.revised, "the words half was dropped"
    assert delta["intent"].slots["recipient_identity"] == "P5"
    assert delta["draft"].body == "Revised body."
    text = message_text(delta)
    assert "chip" in text.lower(), "the recipient half was dropped"
    assert "Body [70]" in text


@pytest.mark.anyio
async def test_a_question_asked_alongside_a_change_rides_with_it(monkeypatch):
    delta, _ = await route(
        "send it to P5 — why was it addressed to P1?", monkeypatch=monkeypatch, kind="question"
    )

    text = message_text(delta)
    assert "chip" in text.lower(), "the recipient change was dropped for the question"
    assert "AskUser" in text, "the question was dropped for the recipient change"


@pytest.mark.anyio
async def test_an_instruction_applies_ON_TOP_of_text_the_human_retyped(monkeypatch):
    """The card offers both at once ("Anything else? (optional)"), so both must land — and
    the instruction has to see the human's own words, not the draft they replaced."""
    typed = SHOWN.replace("4pm", "5pm")

    delta, calls = await route(
        "and make it warmer", monkeypatch=monkeypatch, kind="adjust", typed=typed
    )

    assert len(calls.revised) == 1
    base, instruction = calls.revised[0]
    assert base.body == "It moved to 5pm.", "the reviser was given the draft they replaced"
    assert instruction == "and make it warmer"
    assert delta["draft"].body == "Revised body."


@pytest.mark.anyio
async def test_an_unclassifiable_instruction_still_gets_the_old_behaviour(monkeypatch):
    delta, calls = await route("make it warmer", monkeypatch=monkeypatch, kind=None)

    assert calls.revised, "an outage must not stop a correction being applied"
    assert delta["draft"].body == "Revised body."


@pytest.mark.anyio
async def test_an_unclassifiable_RECIPIENT_instruction_skips_the_reviser(monkeypatch):
    """The pre-classifier rule, preserved: there are no words to revise in "send it to P5"."""
    delta, calls = await route("send it to P5 instead", monkeypatch=monkeypatch, kind=None)

    assert calls.revised == []
    assert delta["intent"].slots["recipient_identity"] == "P5"


@pytest.mark.anyio
async def test_a_recipient_typed_in_the_box_still_wins_over_the_classifier(monkeypatch):
    """The classifier judges WORDS. Who it goes to is decided deterministically, and no
    classification can move it."""
    vault = SessionPiiVault()
    original = vault.trust("priya.nair@corp.com")
    typed = SHOWN.replace("Priya Nair <priya.nair@corp.com>", "alex@corp.com")

    delta, _ = await route(
        "make it warmer", monkeypatch=monkeypatch, kind="rewrite", typed=typed, vault=vault
    )

    token = delta["intent"].slots["recipient_identity"]
    assert token != original
    assert vault.resolve(token) == "alex@corp.com"


@pytest.mark.anyio
async def test_a_rewrite_never_discards_text_the_human_typed_themselves(monkeypatch):
    """The one place a misclassification could destroy something irreplaceable.

    They retyped the email AND added an instruction. A `rewrite` would throw their words
    away on a judgement call, so it is downgraded to an adjustment ON their text.
    """
    typed = SHOWN.replace("It moved to 4pm.", "It moved to 5pm. Please bring the deck.")

    delta, calls = await route(
        "and mention the venue", monkeypatch=monkeypatch, kind="rewrite", typed=typed
    )

    assert calls.rewritten == [], "the human's own words were thrown away"
    assert calls.revised, "the instruction was dropped instead"
    base, _ = calls.revised[0]
    assert base.body == "It moved to 5pm. Please bring the deck."


@pytest.mark.anyio
async def test_an_instruction_with_nothing_to_revise_still_reaches_the_loop(monkeypatch):
    """No draft, no typed text — the loop gets the instruction as plain guidance rather
    than the edit vanishing."""
    monkeypatch.setattr(
        gate_module,
        "interrupt",
        lambda _request: {"verdict": "edit", "edit": "make it warmer", "editedPreview": ""},
    )

    async def classify(_text: str) -> EditScope | None:
        return EditScope(kind="adjust")

    from tests.fakes.fake_surface import FakeEmailSurface

    node = build_approval_gate_node(
        FakeEmailSurface(preview=SHOWN),
        EventEmitter(BufferSink()),
        revise=None,
        rewrite=None,
        classify=classify,
    )
    delta = await node(a_state())

    assert "make it warmer" in message_text(delta)
    assert delta["last_action"] is None, "nothing may be dispatched after an edit"
