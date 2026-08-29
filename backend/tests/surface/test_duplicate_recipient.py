"""A recipient can be entered once. Entering them twice is made impossible, not discouraged.

**Why this is not a prompt problem.** Indices are rebuilt every observation by design, so
the index the agent typed a recipient into last turn points at something else this turn.
Reading the new list, the agent sees a button where it believes it put an address, concludes
its own action failed, and types it again — leaving a committed chip *and* loose text in the
To field. Observed live, twice.

The agent cannot settle this for itself: it is never shown field CONTENTS, so "is my
recipient already in there?" is unanswerable from its side of the wire. Only the executor
can answer it, so the check lives there — the same reasoning, and the same shape, as
`COMPOSE_ALREADY_OPEN`.

Driven against real Chrome over a Gmail-shaped DOM, because the guard reads the live page.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

DIALOG = """
<div role="dialog" style="width:500px;height:400px">
  <div class="header">
    <!-- The FROM row: the signed-in user's own address, in the same attributes a
         recipient chip uses. Must never count as "already a recipient", or emailing
         yourself becomes impossible. -->
    <span data-hovercard-id="me@corp.com" email="me@corp.com">me@corp.com</span>
  </div>
  <div class="recipients"><div class="wrap"><div class="inner">
    {chips}
    <input name="to" aria-label="To recipients" value="{typed}">
  </div></div></div>
  <input name="subjectbox" value="">
  <div g_editable="true" contenteditable="true"></div>
</div>
"""


def chip(address: str) -> str:
    return f'<div data-hovercard-id="{address}" email="{address}" class="afV">{address}</div>'


@pytest.fixture
async def page():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome")
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


async def _present(page, html: str) -> set[str]:
    """What the guard sees as already-addressed."""
    from app.surface.playwright_surface import _RECIPIENTS_JS

    await page.set_content(f"<body>{html}</body>")
    state = await page.evaluate(_RECIPIENTS_JS)
    found = {a for a in state["present"] if a}
    if state["typed"]:
        found.add(state["typed"])
    return found


async def test_a_committed_chip_is_seen_as_already_present(page):
    found = await _present(page, DIALOG.format(chips=chip("priya@corp.com"), typed=""))
    assert "priya@corp.com" in found


async def test_loose_typed_text_counts_too(page):
    """The exact state in the screenshot: typed but not yet committed to a chip. Typing it
    again is what produced a chip AND loose text side by side."""
    found = await _present(page, DIALOG.format(chips="", typed="priya@corp.com"))
    assert "priya@corp.com" in found


async def test_the_senders_OWN_address_is_not_a_recipient(page):
    """The From row must not block emailing yourself — the same scoping bug that made a
    fresh compose report its recipient as already entered."""
    found = await _present(page, DIALOG.format(chips="", typed=""))
    assert found == set(), f"the From row leaked in: {found}"


async def test_an_empty_compose_has_nobody(page):
    assert await _present(page, DIALOG.format(chips="", typed="")) == set()


async def test_a_different_person_is_not_already_present(page):
    """Adding someone new must still work — that is the case the guard must not break."""
    found = await _present(page, DIALOG.format(chips=chip("priya@corp.com"), typed=""))
    assert "alex@corp.com" not in found


async def test_several_chips_are_all_seen(page):
    """The multi-recipient case: both must be recognised, so neither is re-added."""
    two = chip("priya@corp.com") + chip("alex@corp.com")
    found = await _present(page, DIALOG.format(chips=two, typed=""))
    assert {"priya@corp.com", "alex@corp.com"} <= found


async def test_no_dialog_at_all_reports_nobody(page):
    """Failing open: a duplicate check that cannot read the page must never block a
    recipient the user actually asked for."""
    assert await _present(page, "<div>no compose here</div>") == set()


async def test_case_is_ignored(page):
    """Gmail echoes addresses back in whatever case the user typed; a case difference must
    not read as a different person."""
    found = await _present(page, DIALOG.format(chips=chip("Priya@Corp.com"), typed=""))
    assert "priya@corp.com" in found


# ── a token must arrive as an ADDRESS, in a real browser ────────────────────
#
# Everything above is about not typing the same person twice. This is about typing the
# right thing at all: the model sends `P1`, and what has to land in Gmail's To field is
# priya@corp.com — never the characters "P1".
#
# It went wrong when a human separated two recipients with a space. The value stayed
# `"P1 P3"` all the way down, the dispatcher's token check understood commas only, so
# nothing was resolved and the literal text was typed. Asserted here through the real
# `_do_type`, against a real browser, because that is the only place the whole chain is
# actually joined up.


def _surface(page, vault):
    from inbox_contracts import Element, MailContext, Observation, Viewport

    from app.surface.playwright_surface import PlaywrightEmailSurface

    instance = PlaywrightEmailSurface.__new__(PlaywrightEmailSurface)
    instance._page = page
    instance._vault = vault
    instance._last_observation = Observation(
        context_id="T",
        title="Compose",
        viewport=Viewport(width=1280, height=800),
        elements=[Element(index=50, role="textbox", name="To")],
        mail=MailContext(view="compose", composeOpen=True, toIndex=50),
    )
    return instance


async def _type_into_to(page, vault, **args) -> tuple[object, str]:
    """Run the real Type handler and read back what the To input actually holds."""
    from inbox_contracts import ActionCall

    from app.surface.dispatch import ActionValidator

    surface = _surface(page, vault)
    validator = ActionValidator(
        vault=vault,
        geometry={50: (10.0, 20.0)},
        bound_verbs={"Type"},
        observation=surface._last_observation,
    )
    action = validator.validate(ActionCall(name="Type", args={"index": 50, **args}))
    result = await surface._do_type(action)
    typed = await page.eval_on_selector('input[name="to"]', "el => el.value")
    return result, typed


@pytest.fixture
def vault():
    from app.security.vault import SessionPiiVault

    store = SessionPiiVault()
    store.trust("priya@corp.com")
    store.trust("alex@corp.com")
    return store


@pytest.mark.parametrize("text", ["P1", "P1, P2", "P1 P2", "P1;P2"])
async def test_a_token_is_typed_as_the_real_address(page, vault, text):
    """THE regression, end to end. Whatever separated them, addresses land — not tokens."""
    await page.set_content(f"<body>{DIALOG.format(chips='', typed='')}</body>")

    result, landed = await _type_into_to(page, vault, text=text)

    assert result.success is True
    assert "P1" not in landed and "P2" not in landed, f"a token was typed literally: {landed}"
    assert "priya@corp.com" in landed


async def test_two_tokens_land_comma_separated_so_gmail_chips_each_one(page, vault):
    """A comma is what commits a chip. Two addresses joined by a space are one unbroken
    string to Gmail, and the single trailing Enter makes them one malformed recipient."""
    await page.set_content(f"<body>{DIALOG.format(chips='', typed='')}</body>")

    _, landed = await _type_into_to(page, vault, text="P1 P2")

    assert landed == "priya@corp.com, alex@corp.com"


async def test_the_recipient_argument_lands_the_same_way(page, vault):
    await page.set_content(f"<body>{DIALOG.format(chips='', typed='')}</body>")

    _, landed = await _type_into_to(page, vault, recipient="P1 P2")

    assert landed == "priya@corp.com, alex@corp.com"


async def test_only_the_new_one_is_typed_when_the_other_is_already_a_chip(page, vault):
    """The duplicate guard and the separator fix have to hold at the same time."""
    await page.set_content(
        f"<body>{DIALOG.format(chips=chip('priya@corp.com'), typed='')}</body>"
    )

    result, landed = await _type_into_to(page, vault, text="P1 P2")

    assert result.success is True
    assert landed == "alex@corp.com", "the address already committed was typed again"


async def test_an_unresolvable_token_is_refused_and_nothing_is_typed(page, vault):
    """The last line of defence: if resolution somehow did not happen, the field is left
    untouched rather than filled with literal text."""
    from inbox_contracts import ActionCall

    from app.surface.dispatch import ResolvedAction

    await page.set_content(f"<body>{DIALOG.format(chips='', typed='')}</body>")
    surface = _surface(page, vault)

    result = await surface._do_type(
        ResolvedAction(call=ActionCall(name="Type", args={"index": 50, "text": "P1 P2"}))
    )
    landed = await page.eval_on_selector('input[name="to"]', "el => el.value")

    assert result.error_code == "UNRESOLVED_TOKEN"
    assert landed == "", "literal token text reached the To field"
