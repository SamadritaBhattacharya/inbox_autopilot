"""Two verbs that lied, and the runs they cost.

**Scroll always said it worked.** Inside an open compose dialog the wheel does nothing —
the dialog does not scroll — and `_do_scroll` returned "scrolled down" every time. The
agent was hunting an element that was never in the observation, was told six times that it
had successfully scrolled towards it, and had no reason to stop. Scroll is excluded from
the repetition guard by design (repeating it is legitimate), so nothing else was going to
catch it either. A scroll that does not move the page is the only evidence available that
"look further down" is not the answer, and it was being thrown away.

**Clear could not clear the To field.** It is `Ctrl+A, Delete` in the focused input, which
empties the loose text and leaves the committed chip attached — so the next address is
added ALONGSIDE and the mail goes to two people. The correction instruction worked around
that by telling the agent to click the × on the chip instead. A chip's × has no accessible
name and does not survive the funnel, so it was never in the element list: the agent
scrolled six times, ran Extract twice, and asked the human for an index number.

A missing primitive, not a law of nature. Gmail deletes the last chip on Backspace against
an empty input — a trusted keystroke, and exactly what a person does.

Run against real Chrome over a synthetic DOM, calling the real methods: a Python
reimplementation would only prove two copies of my own reasoning agree.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

CHIP = '<div data-hovercard-id="{email}" email="{email}" class="afV">{name}</div>'

COMPOSE = """
<div role="dialog" style="width:600px;height:400px">
  <div class="recipients"><div class="wrap"><div class="inner">
    {chips}
    <input name="to" aria-label="To recipients" value="{typed}">
  </div></div></div>
  <input name="subjectbox" aria-label="Subject" value="Friday demo">
  <div g_editable="true" contenteditable="true" aria-label="Message Body">body</div>
</div>
"""

#: A page taller than the viewport, so a wheel event has somewhere to go.
TALL = "<div style='height:5000px'>tall</div>"

#: A page that fits, so it does not.
SHORT = "<div style='height:50px'>short</div>"


@pytest.fixture
async def page():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome")
        try:
            yield await browser.new_page(viewport={"width": 800, "height": 600})
        finally:
            await browser.close()


def _surface(page):
    """A surface bound to nothing but this page — these verbs use only `_page`."""
    from app.surface.playwright_surface import PlaywrightEmailSurface

    instance = PlaywrightEmailSurface.__new__(PlaywrightEmailSurface)
    instance._page = page
    return instance


def _scroll(direction: str = "down", amount: float = 1):
    from inbox_contracts import ActionCall

    from app.surface.dispatch import ResolvedAction

    return ResolvedAction(
        call=ActionCall(name="Scroll", args={"direction": direction, "amount": amount})
    )


# ── Scroll tells the truth ──────────────────────────────────────────────────


async def test_a_scroll_that_moves_the_page_succeeds(page):
    await page.set_content(f"<body>{TALL}</body>")

    result = await _surface(page)._do_scroll(_scroll())

    assert result.success is True


async def test_a_scroll_with_nowhere_to_go_is_a_TYPED_failure(page):
    """THE regression. Six of these in a row is what twenty wasted turns looked like."""
    await page.set_content(f"<body>{SHORT}</body>")

    result = await _surface(page)._do_scroll(_scroll())

    assert result.success is False
    assert result.error_code == "SCROLL_NO_EFFECT"


async def test_the_failure_says_that_scrolling_again_will_not_help(page):
    """Without that, the model reads a failure as "try harder" and scrolls again."""
    await page.set_content(f"<body>{SHORT}</body>")

    result = await _surface(page)._do_scroll(_scroll())

    assert "will not help" in result.reason
    assert "not further down" in result.reason


async def test_scrolling_UP_at_the_top_is_also_a_failure(page):
    await page.set_content(f"<body>{TALL}</body>")

    result = await _surface(page)._do_scroll(_scroll("up"))

    assert result.success is False
    assert result.error_code == "SCROLL_NO_EFFECT"


async def test_an_inner_container_counts_as_movement(page):
    """`window.scrollY` alone would call this a no-op — Gmail scrolls inner containers, and
    reporting a real scroll as failed is the mirror-image bug."""
    await page.set_content(
        "<body><div id='pane' style='height:100px;overflow:auto'>"
        "<div style='height:3000px'>inner</div></div></body>"
    )
    surface = _surface(page)
    before = await surface._scroll_signature()
    await page.evaluate("document.getElementById('pane').scrollTop = 500")

    assert await surface._scroll_signature() != before


async def test_an_unreadable_page_is_never_reported_as_a_failed_scroll(page):
    """`None` means "could not tell". Inventing a failure here would be as bad as
    inventing a success."""
    await page.set_content(f"<body>{SHORT}</body>")
    surface = _surface(page)
    surface._scroll_signature = lambda: _none()

    result = await surface._do_scroll(_scroll())

    assert result.success is True


async def _none():
    return None


# ── Clear empties the To field, chips included ──────────────────────────────


def _compose(chips: str = "", typed: str = "") -> str:
    return COMPOSE.format(chips=chips, typed=typed)


def _chip(email: str, name: str) -> str:
    return CHIP.format(email=email, name=name)


async def _clear_to(page, html: str):
    """Calls the real `_clear_recipients`. Backspace against an empty input is what Gmail
    reacts to, so the fixture wires that up the way Gmail does."""
    await page.set_content(f"<body>{html}</body>")
    # Gmail's own behaviour: Backspace on an empty To input removes the last chip.
    await page.evaluate(
        """() => {
          const input = document.querySelector('input[name="to"]');
          input.addEventListener('keydown', (event) => {
            if (event.key !== 'Backspace' || input.value) return;
            const chips = document.querySelectorAll('[data-hovercard-id]');
            if (chips.length) chips[chips.length - 1].remove();
          });
        }"""
    )
    return await _surface(page)._clear_recipients()


async def _addressed(page) -> set[str]:
    return await _surface(page)._already_addressed()


async def test_it_removes_a_committed_chip(page):
    """THE regression: `Ctrl+A, Delete` left this chip attached, so the next address typed
    was ADDED and the mail went to two people."""
    result = await _clear_to(page, _compose(chips=_chip("priya@corp.com", "Priya")))

    assert result.success is True
    assert await _addressed(page) == set()


async def test_it_removes_SEVERAL_chips(page):
    chips = _chip("a@corp.com", "A") + _chip("b@corp.com", "B") + _chip("c@corp.com", "C")

    result = await _clear_to(page, _compose(chips=chips))

    assert result.success is True
    assert await _addressed(page) == set()
    assert "3 recipient(s) removed" in result.reason


async def test_it_removes_a_chip_AND_the_text_typed_beside_it(page):
    """Both halves, in one verb. Leaving either behind is how one recipient becomes two."""
    result = await _clear_to(
        page, _compose(chips=_chip("a@corp.com", "A"), typed="half-typed@")
    )

    assert result.success is True
    assert await _addressed(page) == set()


async def test_clearing_an_already_empty_field_is_not_a_failure(page):
    result = await _clear_to(page, _compose())

    assert result.success is True
    assert "0 recipient(s) removed" in result.reason


async def test_it_reports_failure_rather_than_claiming_a_field_it_could_not_empty(page):
    """Read the truth back instead of trusting the keystrokes. A recipient believed removed
    and still attached is exactly how one mail goes to two people."""
    await page.set_content(f"<body>{_compose(chips=_chip('a@corp.com', 'A'))}</body>")
    # No Backspace handler: the chip is unremovable, as a Gmail variant we do not know
    # would be.
    result = await _surface(page)._clear_recipients()

    assert result.success is False
    assert result.error_code == "RECIPIENTS_NOT_CLEARED"
    assert "do not type another address on top" in result.reason.lower()


async def test_an_unreachable_To_field_is_typed_rather_than_silent(page):
    await page.set_content("<body><div>no compose window here</div></body>")

    result = await _surface(page)._clear_recipients()

    assert result.success is False
    assert result.error_code == "FIELD_UNREACHABLE"
