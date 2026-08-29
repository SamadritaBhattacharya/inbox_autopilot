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


# ── reset: a new run starts from a known page ───────────────────────────────
#
# The browser outlives the run. A run that was stopped, or whose approval timed out, leaves
# a compose window open — and the next task begins inside it, where COMPOSE_ALREADY_OPEN
# (correct within a run) steers the agent into writing in somebody's abandoned draft.
#
# Save & close, never Discard: those are the human's words, and they stay in Drafts.


COMPOSE_WITH_CLOSE = """
<div role="dialog" style="width:600px;height:400px">
  <img aria-label="Save &amp; close" style="width:20px;height:20px" id="closer">
  <input name="to" aria-label="To recipients" value="priya@corp.com">
  <input name="subjectbox" aria-label="Subject" value="half written">
  <div g_editable="true" contenteditable="true" aria-label="Message Body">draft</div>
</div>
"""

#: Gmail uses role="dialog" for settings panes and confirmations too. Closing one of those
#: because it looked like a draft would be a mystery to debug.
NOT_A_COMPOSE = """
<div role="dialog" style="width:300px;height:200px">
  <img aria-label="Save &amp; close" style="width:20px;height:20px">
  <p>Are you sure?</p>
</div>
"""


async def _wire_close(page):
    """Make the close control behave like Gmail's: it removes the compose window."""
    await page.evaluate(
        """() => {
          const closer = document.getElementById('closer');
          if (!closer) return;
          closer.addEventListener('click', () => closer.closest('[role=dialog]').remove());
        }"""
    )


async def test_it_closes_a_compose_window_left_open_by_an_earlier_run(page):
    await page.set_content(f"<body>{COMPOSE_WITH_CLOSE}</body>")
    await _wire_close(page)

    report = await _surface(page).reset()

    assert "compose window" in report
    assert await page.query_selector('[role="dialog"]') is None


async def test_it_says_the_draft_was_kept(page):
    """A window closing on its own is alarming unless somebody says the words survived."""
    await page.set_content(f"<body>{COMPOSE_WITH_CLOSE}</body>")
    await _wire_close(page)

    assert "Drafts" in await _surface(page).reset()


async def test_a_clean_page_reports_nothing_and_touches_nothing(page):
    await page.set_content("<body><div>just the inbox</div></body>")

    assert await _surface(page).reset() == ""


async def test_a_dialog_that_is_not_a_compose_window_is_left_alone(page):
    """Scoped to dialogs that actually hold compose fields."""
    await page.set_content(f"<body>{NOT_A_COMPOSE}</body>")

    report = await _surface(page).reset()

    assert report == ""
    assert await page.query_selector('[role="dialog"]') is not None


async def test_it_never_clicks_Discard(page):
    """Irreversible, and nothing irreversible happens outside the approval gate — least of
    all as a side effect of starting an unrelated task."""
    html = COMPOSE_WITH_CLOSE.replace(
        '<img aria-label="Save &amp; close" style="width:20px;height:20px" id="closer">',
        '<img aria-label="Discard draft" id="discard" style="width:20px;height:20px">'
        '<img aria-label="Save &amp; close" style="width:20px;height:20px" id="closer">',
    )
    await page.set_content(f"<body>{html}</body>")
    await page.evaluate(
        """() => {
          window.__discarded = false;
          document.getElementById('discard')
            .addEventListener('click', () => { window.__discarded = true; });
          const closer = document.getElementById('closer');
          closer.addEventListener('click', () => closer.closest('[role=dialog]').remove());
        }"""
    )

    await _surface(page).reset()

    assert await page.evaluate("() => window.__discarded") is False


async def test_it_drops_the_index_map_so_no_referent_survives_the_run(page):
    """Indices belong to the page they were built from. A new run inheriting them could
    act on a number that means something else now."""
    await page.set_content(f"<body>{COMPOSE_WITH_CLOSE}</body>")
    await _wire_close(page)
    surface = _surface(page)
    surface._geometry = {9: (1.0, 2.0)}
    surface._previous_identities = {"stale"}
    surface._approved = {"a-fingerprint"}
    surface._last_observation = object()

    await surface.reset()

    assert surface._geometry == {}
    assert surface._previous_identities == set()
    assert surface._approved == set(), "an approval must never outlive its run"
    assert surface._last_observation is None


async def test_a_window_that_will_not_close_is_reported_as_such(page):
    """**The bug this catches was mine.** The first version of `reset` appended "closed a
    compose window" unconditionally — it never re-checked after its Escape fallback. A
    window that refused to close was reported as closed, the cockpit said "Starting fresh",
    and the agent began inside the stale draft anyway. Exactly the class of lying verb this
    file exists to remove."""
    await page.set_content(f"<body>{COMPOSE_WITH_CLOSE}</body>")
    # No handler wired: the close control does nothing, as an unknown Gmail build would.

    report = await _surface(page).reset()

    assert "could not close" in report
    assert "closed a compose window" not in report
    assert await page.query_selector('[role="dialog"]') is not None


async def test_escape_is_tried_when_the_close_control_does_nothing(page):
    """The fallback has to actually run — and be believed only after it is verified."""
    await page.set_content(f"<body>{COMPOSE_WITH_CLOSE}</body>")
    await page.evaluate(
        """() => {
          document.addEventListener('keydown', (event) => {
            if (event.key !== 'Escape') return;
            document.querySelector('[role=dialog]').remove();
          });
        }"""
    )

    report = await _surface(page).reset()

    assert "closed a compose window" in report
    assert await page.query_selector('[role="dialog"]') is None


async def test_the_page_referents_are_dropped_even_when_closing_failed(page):
    """A run that starts on a page we could not tidy must still not inherit indices or an
    approval from the run before it."""
    await page.set_content(f"<body>{COMPOSE_WITH_CLOSE}</body>")
    surface = _surface(page)
    surface._geometry = {9: (1.0, 2.0)}
    surface._previous_identities = {"stale"}
    surface._approved = {"a-fingerprint"}
    surface._last_observation = object()

    await surface.reset()

    assert surface._geometry == {}
    assert surface._approved == set(), "an approval outlived its run"
    assert surface._last_observation is None
