"""The assembled funnel — and the leak assertion the product claim rests on."""
from __future__ import annotations

import pytest

from app.observation.funnel import pipeline as pipeline_module
from app.observation.funnel.pipeline import STAGE_ORDER, ObservationFunnel
from app.security.patterns import find_emails, find_phones
from tests.observation.conftest import VIEWPORT_H, element, meta


def inbox_elements() -> list:
    """A realistic inbox: sender chips, subjects, buttons, and a row below the fold."""
    return [
        element(1, role="button", name="Compose", x=20, y=20, width=90, height=36,
                interactive=True),
        element(10, role="sender", name="Priya Nair", x=40, y=100, width=160, height=20),
        element(11, role="listitem", name="Friday demo moved to 4pm - priya.nair@corp.com",
                x=220, y=100, width=600, height=20, interactive=True),
        element(20, role="sender", name="Dev Kapoor", x=40, y=140, width=160, height=20),
        element(21, role="listitem", name="Call me on +91 98765 43210 about Q3",
                x=220, y=140, width=600, height=20, interactive=True),
        element(30, role="listitem", name="Newsletter from shop@store.example",
                x=220, y=180, width=600, height=20, interactive=True),
        # Below the fold: reachable by scrolling, so it must be COUNTED, not silently gone.
        element(40, role="listitem", name="Older thread", x=220, y=VIEWPORT_H + 200,
                width=600, height=20, interactive=True),
        # Not rendered at all.
        element(50, role="listitem", name="hidden row", displayed=False),
    ]


@pytest.fixture
def funnel(tokenizer) -> ObservationFunnel:
    return ObservationFunnel(tokenizer)


# ── stage ordering IS a security control ────────────────────────────────────


def test_tokenizer_runs_before_anything_serializes_text():
    position = STAGE_ORDER.index("pii_tokenize")
    assert position < STAGE_ORDER.index("som")
    assert position < STAGE_ORDER.index("reading_order")


def test_reordering_the_pipeline_fails_loudly(monkeypatch):
    """Moving the tokenizer later is a security regression, not a refactor."""
    unsafe = ("extract", "visibility", "som", "reading_order", "pii_tokenize")
    monkeypatch.setattr(pipeline_module, "STAGE_ORDER", unsafe)

    with pytest.raises(RuntimeError, match="unsafe"):
        pipeline_module._assert_tokenizer_precedes_serialization()


# ── the leak assertion ──────────────────────────────────────────────────────


def test_no_raw_pii_survives_the_funnel(funnel):
    """The evidence behind 'the model never saw a real address'."""
    observation, _, _ = funnel.run(inbox_elements(), meta())
    serialized = observation.model_dump_json()

    assert find_emails(serialized) == []
    assert find_phones(serialized) == []
    for raw in ("priya.nair@corp.com", "shop@store.example", "98765", "Priya Nair", "Dev Kapoor"):
        assert raw not in serialized, f"{raw!r} leaked into the observation"


def test_the_meaning_survives_tokenization(funnel):
    """Tokenizing must not destroy the content the agent has to reason about."""
    observation, _, _ = funnel.run(inbox_elements(), meta())
    names = " ".join(e.name for e in observation.elements)

    assert "Friday demo moved to 4pm" in names
    assert "about Q3" in names
    assert "Compose" in names


def test_the_same_person_gets_one_token_across_rows(funnel):
    """Otherwise the model reasons about one human as two."""
    observation, _, _ = funnel.run(
        [
            element(1, role="sender", name="Priya Nair", y=10),
            element(2, role="listitem", name="Priya Nair replied to your thread", y=50),
        ],
        meta(),
    )
    listed = " ".join(e.name for e in observation.elements)
    assert listed.count("C1") == 2


def test_a_url_never_reaches_the_wire(funnel):
    """On an email surface a URL IS an identifier — hence context_id, not url."""
    observation, _, _ = funnel.run(
        inbox_elements(), meta(context_ref="mail/u/0/#inbox/18f3a9c2b1", thread_ref="18f3a9c2b1")
    )
    assert observation.context_id.startswith("T")
    assert "18f3a9c2b1" not in observation.model_dump_json()
    assert observation.mail is not None
    assert observation.mail.thread_token is not None
    assert observation.mail.thread_token.startswith("T")


# ── counts are honest ───────────────────────────────────────────────────────


def test_offscreen_rows_are_reported_so_the_agent_scrolls(funnel):
    observation, _, report = funnel.run(inbox_elements(), meta())

    assert report.offscreen == 1
    assert report.hidden == 1
    assert observation.dropped_count >= 1, "the agent must know there is more below"


def test_hidden_elements_are_not_reported_as_reachable(funnel):
    """Reporting them would send the agent scrolling after content that does not exist."""
    _, _, report = funnel.run(
        [element(1, name="visible", interactive=True), element(2, name="gone", displayed=False)],
        meta(),
    )
    assert report.hidden == 1
    assert report.reachable_but_unlisted == 0


def test_budget_pressure_is_added_to_the_dropped_count(tokenizer):
    tight = ObservationFunnel(tokenizer, token_budget=20)
    observation, _, report = tight.run(inbox_elements(), meta())

    assert report.budget_dropped > 0
    assert observation.dropped_count == report.offscreen + report.budget_dropped


# ── geometry stays behind ───────────────────────────────────────────────────


def test_geometry_never_crosses_the_wire(funnel):
    observation, geometry, _ = funnel.run(inbox_elements(), meta())

    assert geometry, "the executor needs the map"
    serialized = observation.model_dump_json()
    for key in ("\"x\"", "\"y\"", "centerX", "backendNodeId"):
        assert key not in serialized


def test_only_listed_indices_are_dispatchable(tokenizer):
    """A hallucinated number must not land on a real element by coincidence."""
    tight = ObservationFunnel(tokenizer, token_budget=20)
    observation, geometry, _ = tight.run(inbox_elements(), meta())

    assert set(geometry) == {e.index for e in observation.elements}


# ── the contract holds ──────────────────────────────────────────────────────


def test_the_result_validates_as_the_wire_contract(funnel):
    from inbox_contracts import Observation

    observation, _, _ = funnel.run(inbox_elements(), meta())
    assert Observation.model_validate(observation.model_dump(by_alias=True)) == observation


def test_mail_semantics_are_carried(funnel):
    observation, _, _ = funnel.run(
        inbox_elements(), meta(view="compose", compose_open=True, unread_count=7)
    )
    assert observation.mail is not None
    assert observation.mail.view == "compose"
    assert observation.mail.compose_open is True
    assert observation.mail.unread_count == 7


def test_indices_are_contiguous_from_one(funnel):
    observation, _, _ = funnel.run(inbox_elements(), meta())
    assert [e.index for e in observation.elements] == list(range(1, len(observation.elements) + 1))


def test_an_empty_page_is_not_a_crash(funnel):
    observation, geometry, report = funnel.run([], meta())
    assert observation.elements == []
    assert geometry == {}
    assert report.shown == 0
