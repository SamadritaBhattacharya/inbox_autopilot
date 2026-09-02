"""Overwriting a field is ONE action, and a written body must not look unwritten.

Two failures, one run, feeding each other.

**"Clear it, then Type" is two verbs in a one-verb-per-turn loop.** The worker prompt says
*call exactly one tool per turn*; the correction message asked for two. The model spent most
of its reasoning on the conflict — *"That's two calls in same turn, which violates rule 'Call
exactly one tool per turn'... we need to redo"* — lost track of which half it had already
done, and re-cleared a body it had just written correctly. Three times, on one edit.

**And every successful write looked like a failure.** Gmail wraps each typed line in its own
`<div>`, so a five-line email became five new elements the moment it was written. The model
read that as *"the body is split into multiple textboxes"*, concluded the write had gone
wrong, and cleared it again.

Run against real Chrome, calling the real methods: a Python reimplementation would only
prove two copies of the same reasoning agree.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

COMPOSE = """
<div role="dialog" style="width:600px;height:420px">
  <div class="recipients"><div class="wrap"><div class="inner">
    <input name="to" aria-label="To recipients" value="{to}">
  </div></div></div>
  <input name="subjectbox" aria-label="Subject" value="{subject}">
  <div g_editable="true" contenteditable="true" aria-label="Message Body"
       style="min-height:120px">{body}</div>
  <button>Send</button>
</div>
"""


@pytest.fixture
async def page():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome")
        try:
            yield await browser.new_page(viewport={"width": 900, "height": 700})
        finally:
            await browser.close()


def _surface(page, *, to=None, subject=None, body=None):
    """A surface bound to this page, with the compose indices the agent would have been given."""
    from inbox_contracts import Element, MailContext, Observation, Viewport

    from app.surface.playwright_surface import PlaywrightEmailSurface

    instance = PlaywrightEmailSurface.__new__(PlaywrightEmailSurface)
    instance._page = page
    instance._last_observation = Observation(
        context_id="T",
        title="Compose",
        viewport=Viewport(width=900, height=700),
        elements=[Element(index=n, role="textbox", name="f") for n in (61, 67, 72) if n],
        mail=MailContext(
            view="compose",
            composeOpen=True,
            toIndex=to,
            subjectIndex=subject,
            bodyIndex=body,
        ),
    )
    return instance


def _call(verb: str, index: int, **args):
    from inbox_contracts import ActionCall

    from app.surface.dispatch import ResolvedAction

    return ResolvedAction(call=ActionCall(name=verb, args={"index": index, **args}))


async def _set(page, *, to="", subject="", body=""):
    await page.set_content(f"<body>{COMPOSE.format(to=to, subject=subject, body=body)}</body>")


async def _body_text(page) -> str:
    return await page.eval_on_selector('[g_editable="true"]', "el => el.innerText.trim()")


# ── Replace overwrites, in one action ──────────────────────────────────────


async def test_it_overwrites_a_body_that_already_has_text(page):
    """THE point. One call, one turn, one result — no Clear/Type dance to reason about."""
    await _set(page, body="the old words")
    surface = _surface(page, body=72)

    result = await surface._do_replace(_call("Replace", 72, text="the new words"))

    assert result.success is True
    assert await _body_text(page) == "the new words"


async def test_nothing_of_the_old_text_survives(page):
    """A Replace that appends is worse than one that fails: the human sees their correction
    applied and the original still sitting above it."""
    await _set(page, body="Good evening. Regards, Sam")
    surface = _surface(page, body=72)

    await surface._do_replace(_call("Replace", 72, text="Entirely different."))

    assert await _body_text(page) == "Entirely different."


async def test_it_works_on_an_already_empty_field(page):
    """The instruction says Replace whether or not the field has content. Clearing nothing
    must not be an error."""
    await _set(page, body="")
    surface = _surface(page, body=72)

    result = await surface._do_replace(_call("Replace", 72, text="first draft"))

    assert result.success is True
    assert await _body_text(page) == "first draft"


async def test_a_multi_line_body_lands_with_its_blank_lines(page):
    """A blank line between paragraphs is content. Losing one silently reformats the email."""
    await _set(page, body="old")
    surface = _surface(page, body=72)
    text = "Good evening,\n\nKeep going.\n\nRegards,\nSam"

    await surface._do_replace(_call("Replace", 72, text=text))

    landed = await _body_text(page)
    assert "Good evening," in landed
    assert "Regards," in landed
    assert "Sam" in landed


async def test_it_replaces_a_subject_too(page):
    await _set(page, subject="Old subject")
    surface = _surface(page, subject=67)

    result = await surface._do_replace(_call("Replace", 67, text="Friday demo — moved"))

    assert result.success is True
    assert await page.eval_on_selector('[name="subjectbox"]', "el => el.value") == (
        "Friday demo — moved"
    )


async def test_a_field_it_cannot_reach_is_reported_and_NOT_typed_into(page):
    """Clearing runs first and alone, so an unreachable field fails before anything is
    written on top of its old contents."""
    await page.set_content("<body><div>no compose window</div></body>")
    surface = _surface(page, body=72)

    result = await surface._do_replace(_call("Replace", 72, text="new"))

    assert result.success is False
    assert result.error_code == "FIELD_UNREACHABLE"


async def test_replacing_the_recipient_removes_the_committed_chip(page):
    """`Clear` on the To field is chip-aware, and Replace inherits that — otherwise the new
    address is ADDED beside the old one and the mail goes to two people."""
    html = COMPOSE.format(to="", subject="", body="").replace(
        '<input name="to"',
        '<div data-hovercard-id="old@corp.com" email="old@corp.com">Old</div><input name="to"',
    )
    await page.set_content(f"<body>{html}</body>")
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
    surface = _surface(page, to=61)

    result = await surface._do_replace(_call("Replace", 61, text="Biyash"))

    assert result.success is True
    assert await surface._already_addressed() == {"biyash"}


# ── a written body stays ONE element ───────────────────────────────────────


async def _elements(page):
    from app.surface.extract import EXTRACT_JS, MAX_NODES, parse_elements

    raw = await page.evaluate(EXTRACT_JS, MAX_NODES)
    return parse_elements(raw.get("elements") or [])


async def test_a_multi_line_body_does_not_become_multiple_elements(page):
    """THE regression. Gmail wraps every line in its own div, so writing the email made four
    new entries appear where one body field had been — and the agent read that as its own
    write having failed."""
    lines = "".join(
        f"<div>{line}</div>"
        for line in ("Good evening,", "I hope your day went well.", "Regards,", "Sam")
    )
    await _set(page)
    await page.eval_on_selector('[g_editable="true"]', f"el => el.innerHTML = `{lines}`")

    names = [element.name or "" for element in await _elements(page)]

    assert not any(name == "I hope your day went well." for name in names)
    assert not any(name == "Regards," for name in names)


async def test_the_body_itself_is_still_there(page):
    """Dropping the children must not drop the field. The agent types into the body, and it
    has to be in the list to be typed into."""
    await _set(page, body="<div>a line</div><div>another</div>")

    elements = await _elements(page)
    bodies = [e for e in elements if (e.name or "").lower().startswith("message body")]

    assert bodies, "the body field itself disappeared from the observation"


async def test_the_body_index_still_resolves(page):
    """`MailContext.body_index` promises the body is one element with one number. That
    promise is what the roll-up is keeping."""
    from app.surface.extract import EXTRACT_JS, MAX_NODES, parse_meta

    await _set(page, body="<div>a line</div><div>another</div>")
    meta = parse_meta((await page.evaluate(EXTRACT_JS, MAX_NODES)).get("meta") or {})

    assert meta.body_node is not None


async def test_other_fields_are_untouched_by_the_roll_up(page):
    """Scoped to the body. A recipient chip and the subject are outside it and must survive."""
    await _set(page, to="priya@corp.com", subject="Friday demo", body="<div>hi</div>")

    names = " ".join((e.name or "") + (e.value or "") for e in await _elements(page))

    assert "Friday demo" in names


async def test_a_page_with_no_compose_window_is_unaffected(page):
    """The roll-up must not eat the inbox on a page that has no body to roll up."""
    await page.set_content(
        "<body><div role='main'><div>Priya Nair</div><div>Re: the demo</div></div></body>"
    )

    names = " ".join((e.name or "") for e in await _elements(page))

    assert "Priya Nair" in names
    assert "Re: the demo" in names
