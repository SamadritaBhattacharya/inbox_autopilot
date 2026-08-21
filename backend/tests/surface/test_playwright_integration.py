"""The funnel and trusted input against a REAL browser, on a LOCAL fixture.

Marked `browser` and excluded from the default run — not because it is unimportant, but
because the fast suite has to stay fast enough that nobody skips it. Never points at a live
mailbox: the fixture is a static file, so the test is deterministic and cannot damage
anything.

    uv run --project backend pytest -m browser backend/tests/surface -v

What only a real browser can prove: that computed styles, hit-testing, layout boxes, and
trusted input all behave the way the funnel assumes. Every one of those assumptions is
invisible to a synthetic fixture.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from inbox_contracts import ActionCall

from app.security.patterns import TOKEN_RE, find_emails, find_phones
from app.surface.dispatch import approval_fingerprint
from app.surface.playwright_surface import launch_surface, resolve_chromium

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        resolve_chromium() is None,
        reason="no Chromium build found; run `playwright install chromium`",
    ),
]

FIXTURE = (Path(__file__).resolve().parents[1] / "fixtures" / "inbox.html").as_uri()


@pytest.fixture
async def surface():
    surface, close = await launch_surface(headless=True, start_url=FIXTURE)
    try:
        yield surface
    finally:
        await close()


def named(observation, fragment: str):
    return next((e for e in observation.elements if fragment.lower() in e.name.lower()), None)


# ── perception ──────────────────────────────────────────────────────────────


async def test_a_real_page_becomes_a_small_numbered_list(surface):
    observation = await surface.observe()

    assert observation.elements, "the funnel produced nothing from a real page"
    assert len(observation.elements) < 60, "a 4-row inbox must not produce a huge list"
    indices = [e.index for e in observation.elements]
    assert indices == sorted(indices)
    assert indices[0] == 1


async def test_no_raw_pii_survives_a_real_page(surface):
    """The product claim, proven against a browser rather than a synthetic fixture."""
    observation = await surface.observe()
    serialized = observation.model_dump_json()

    assert find_emails(serialized) == []
    assert find_phones(serialized) == []
    for raw in ("priya.nair@corp.com", "dev.kapoor@corp.com", "Priya Nair", "98765"):
        assert raw not in serialized, f"{raw!r} leaked from the live page"


async def test_the_subjects_survive_tokenization(surface):
    observation = await surface.observe()
    text = " ".join(e.name for e in observation.elements)

    assert "Friday demo moved to 4pm" in text
    assert "Q3 numbers" in text


async def test_a_display_none_row_is_never_listed(surface):
    observation = await surface.observe()
    assert named(observation, "display:none") is None
    assert named(observation, "Hidden Sender") is None


async def test_content_below_the_fold_is_reported_not_silently_dropped(surface):
    """The agent has to know there is more, or it concludes the mail does not exist."""
    observation = await surface.observe()

    assert named(observation, "far below the fold") is None
    assert observation.dropped_count >= 1


async def test_the_compose_button_is_actionable(surface):
    observation = await surface.observe()
    compose = named(observation, "compose")
    assert compose is not None
    assert compose.role == "button"


async def test_wrapper_chains_do_not_produce_duplicates(surface):
    """`div > div > div.row` must not appear as three separate targets."""
    observation = await surface.observe()
    subjects = [e for e in observation.elements if "Friday demo" in e.name]
    assert len(subjects) == 1


# ── acting ──────────────────────────────────────────────────────────────────


async def test_a_trusted_click_opens_the_dialog_and_the_next_observation_shows_it(surface):
    """No popup tracking anywhere: re-observe, and the dialog is simply what is there."""
    before = await surface.observe()
    compose = named(before, "compose")
    assert compose is not None

    result = await surface.act(ActionCall(name="Click", args={"index": compose.index}))
    assert result.success, result.reason

    after = await surface.observe()
    assert after.mail is not None
    assert after.mail.compose_open is True
    assert after.mail.view == "compose"
    assert named(after, "Subject") is not None


async def test_occlusion_hides_the_list_behind_the_dialog(surface):
    """The modal becomes the salient thing — the same perception a human has."""
    before = await surface.observe()
    await surface.act(ActionCall(name="Click", args={"index": named(before, "compose").index}))
    after = await surface.observe()

    assert named(after, "Friday demo") is None, "rows behind the dialog are unreachable"
    assert named(after, "Recipients") is not None


async def test_typing_lands_in_the_focused_field(surface):
    before = await surface.observe()
    await surface.act(ActionCall(name="Click", args={"index": named(before, "compose").index}))
    opened = await surface.observe()

    subject = named(opened, "Subject")
    result = await surface.act(
        ActionCall(name="Type", args={"index": subject.index, "text": "Friday demo"})
    )

    assert result.success
    assert await surface._page.input_value("#subject") == "Friday demo"


async def test_indices_are_rebuilt_every_turn(surface):
    """A stale index must not survive into the next turn."""
    before = await surface.observe()
    await surface.act(ActionCall(name="Click", args={"index": named(before, "compose").index}))
    after = await surface.observe()

    before_map = {e.index: e.name for e in before.elements}
    after_map = {e.index: e.name for e in after.elements}
    assert before_map != after_map


async def test_scrolling_reveals_what_was_below_the_fold(surface):
    await surface.observe()
    result = await surface.act(ActionCall(name="Scroll", args={"direction": "down", "amount": 3}))
    assert result.success

    after = await surface.observe()
    assert named(after, "far below the fold") is not None


# ── guardrails, against a real page ─────────────────────────────────────────


async def test_a_stale_index_is_refused_rather_than_misfiring(surface):
    await surface.observe()
    result = await surface.act(ActionCall(name="Click", args={"index": 9999}))

    assert result.success is False
    assert result.error_code == "STALE_INDEX"


async def test_send_is_refused_without_approval(surface):
    """The guarantee that makes this safe to point at a real mailbox."""
    before = await surface.observe()
    await surface.act(ActionCall(name="Click", args={"index": named(before, "compose").index}))
    opened = await surface.observe()

    send = named(opened, "Send")
    assert send is not None

    result = await surface.act(ActionCall(name="Send", args={"index": send.index}))
    assert result.success is False
    assert result.error_code == "APPROVAL_REQUIRED"


async def test_an_approval_authorizes_only_that_exact_payload(surface):
    before = await surface.observe()
    await surface.act(ActionCall(name="Click", args={"index": named(before, "compose").index}))
    opened = await surface.observe()
    send_index = named(opened, "Send").index

    surface.approve(approval_fingerprint(ActionCall(name="Send", args={"index": send_index})))

    # A different payload is still refused.
    other = await surface.act(ActionCall(name="Send", args={"index": send_index, "extra": 1}))
    assert other.error_code == "APPROVAL_REQUIRED"


async def test_a_literal_address_cannot_be_typed_as_a_recipient(surface):
    """The injected-recipient case, end to end on a real page."""
    await surface.observe()
    result = await surface.act(
        ActionCall(name="Type", args={"recipient": "attacker@evil.com"})
    )

    assert result.success is False
    assert result.error_code == "UNKNOWN_TOKEN"


async def test_a_token_resolves_to_a_real_address_only_at_the_keyboard(surface):
    """The one moment a real address exists outside the vault."""
    observation = await surface.observe()
    tokens = TOKEN_RE.findall(" ".join(e.name for e in observation.elements))
    assert tokens, "the funnel produced no person tokens to target"
    token = tokens[0]

    await surface.act(ActionCall(name="Click", args={"index": named(observation, "compose").index}))
    opened = await surface.observe()

    result = await surface.act(
        ActionCall(
            name="Type",
            args={"index": named(opened, "Recipients").index, "recipient": token},
        )
    )
    assert result.success
    # Whatever landed in the field is a REAL value, and it never appeared in an observation.
    typed = await surface._page.input_value("#to")
    assert typed and typed not in observation.model_dump_json()


# ── the live view ───────────────────────────────────────────────────────────


async def test_the_screencast_delivers_frames(surface):
    """The RHS of the cockpit is this stream, so "does it emit" is a product assertion.

    It is also easy to get wrong in a way that looks like nothing: Chrome sends one frame,
    waits for an acknowledgement, and goes silent forever if none arrives. A blank live view
    and a hung agent are indistinguishable from the outside.
    """
    frames: list[tuple[int, int]] = []

    async def on_frame(data: str, seq: int) -> None:
        frames.append((seq, len(data)))

    await surface.start_screencast(on_frame)
    await surface.observe()
    await asyncio.sleep(1.0)
    await surface.act(ActionCall(name="Scroll", args={"direction": "down"}))
    await asyncio.sleep(1.5)
    await surface.stop_screencast()

    assert frames, "the screencast produced no frames"
    assert all(size > 0 for _, size in frames)
    # Sequence numbers are what the cockpit uses to drop late frames.
    assert [seq for seq, _ in frames] == sorted(seq for seq, _ in frames)


async def test_a_failing_frame_consumer_does_not_kill_the_run(surface):
    """A broken cockpit must cost the live view, never the task."""

    async def explode(data: str, seq: int) -> None:
        raise RuntimeError("cockpit went away")

    await surface.start_screencast(explode)
    await surface.observe()
    await asyncio.sleep(0.5)

    observation = await surface.observe()
    assert observation.elements
    await surface.stop_screencast()


# ── secrets never leave the page ────────────────────────────────────────────

LOGIN = (Path(__file__).resolve().parents[1] / "fixtures" / "login.html").as_uri()

SECRETS = ("hunter2-correct-horse", "483920", "masked-by-css-only")


async def test_no_credential_value_survives_the_funnel():
    """The strongest guarantee in the system, and the cheapest to lose.

    `valueOf` used to return `el.value` for anything that had one — which on a login page
    is the plaintext password, headed for the model's context, the trajectory store, and
    the logs. The PII vault cannot help here: it tokenizes so values can be *resolved
    again*, which is the opposite of what a secret needs. So the redaction happens in the
    page, in the extractor, before a single byte crosses into Python.

    Asserted against the WHOLE serialized observation rather than field by field: a secret
    that reappears in an accessible name or a title is just as leaked as one in a value.
    """
    surface, close = await launch_surface(headless=True, start_url=LOGIN)
    try:
        observation = await surface.observe()
    finally:
        await close()

    serialized = observation.model_dump_json()
    for secret in SECRETS:
        assert secret not in serialized, f"{secret!r} reached the observation"


async def test_a_filled_secret_still_reports_that_it_is_filled():
    """Redaction must not blind the agent: "is this box already filled?" is a question it
    legitimately needs answered, and only the characters are off-limits."""
    surface, close = await launch_surface(headless=True, start_url=LOGIN)
    try:
        observation = await surface.observe()
    finally:
        await close()

    values = [element.value for element in observation.elements if element.value]
    assert any("•" in (value or "") for value in values), "no field reported as filled"


async def test_an_ordinary_field_is_still_readable():
    """The guard must be narrow. Redacting every value would make the funnel useless for
    the compose window, which is the one place values matter most."""
    surface, close = await launch_surface(headless=True, start_url=LOGIN)
    try:
        observation = await surface.observe()
    finally:
        await close()

    # The email field is ordinary input — tokenized as PII, but present and not masked.
    assert any(
        element.value and "•" not in element.value for element in observation.elements
    ), "every value was redacted; the guard is too broad"
