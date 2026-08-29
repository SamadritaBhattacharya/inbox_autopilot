"""`PlaywrightEmailSurface` — a real browser, driven by trusted input.

The default surface: dev, CI, and the demo. Its production counterpart drives the user's
own Chrome, and the graph cannot tell the two apart — that indistinguishability is the SOLID
payoff this project is betting on.

**Trusted input, not `element.click()`.** Actions are dispatched through CDP `Input.*`, so
events arrive with `isTrusted: true`. A JS-synthesised click is observably different: it
skips hover state, misses handlers bound to pointer events, and is exactly what automation
detection looks for. This matters more on a mailbox than on a toy page — a click that
silently does nothing sends the agent into a repetition loop, and the guard fires several
wasted turns later.

**Settle, then re-observe from scratch.** After every action the page is given time to go
quiet and the funnel runs again. Nothing tracks "what changed": a dialog simply appears in
the next fresh list, occlusion hides what is behind it, and the modal becomes the salient
thing. That is why the loop needs no special handling for popups, navigation, or new tabs.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING, Any

from inbox_contracts import ActionCall, ActionResult, Observation

from app.observation.funnel.pipeline import ObservationFunnel
from app.observation.funnel.reading_order import identity_set
from app.security.patterns import TOKEN_RE
from app.security.tokenizer import PiiTokenizer
from app.security.vault import SessionPiiVault
from app.surface.base import SurfaceUnavailable
from app.surface.dispatch import (
    ActionValidator,
    DispatchRejected,
    ResolvedAction,
    _is_all_tokens,
)
from app.surface.extract import EXTRACT_JS, MAX_NODES, parse_elements, parse_meta
from app.workers.irreversible import is_irreversible

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

#: Per-verb timeout walls. A breach is `ACTION_TIMEOUT` — a typed failure the recovery layer
#: can classify, never an indefinite hang.
TIMEOUTS: dict[str, float] = {
    "Navigate": 30.0,
    "Click": 10.0,
    "ReadThread": 10.0,
    "Type": 10.0,
    "Clear": 5.0,
    "PressKey": 5.0,
    "Scroll": 5.0,
    "Archive": 10.0,
    "Send": 20.0,
    "WaitFor": 30.0,
}
DEFAULT_TIMEOUT = 10.0

#: Above this many characters, type in ONE bulk insert instead of key by key.
#:
#: Key-by-key is the honest simulation and it is what makes Gmail's recipient autocomplete
#: produce a chip, so short fields keep it. But every keystroke is a separate CDP round trip
#: (keyDown, char, keyUp), and against a real browser that is ~50ms each — so a 190-character
#: body took ~9.5s and breached its 10s wall. An email body has no per-keystroke handler
#: worth simulating; a recipient field does. The threshold sits between the two.
TYPE_KEYSTROKE_LIMIT = 60

#: Budget per character for the timeout wall, with headroom over the ~50ms observed for a
#: keystroke round trip. A fixed wall on an action whose duration is LINEAR in its payload
#: is not a tuning mistake, it is the wrong model: it fails on exactly the long bodies the
#: writer is there to produce.
TYPE_SECONDS_PER_CHAR = 0.08

#: Ceiling on a scaled wall. The budget above is sized for the KEYSTROKE fallback; the
#: normal path is a single insert and finishes in well under a second. Without a cap a
#: pathological 20k-character body would buy itself a half-hour hang, and the whole point of
#: a wall is that a stuck action cannot eat the run.
TYPE_TIMEOUT_MAX = 60.0


def timeout_for(call: ActionCall) -> float:
    """This call's wall, scaled by what it actually has to do."""
    base = TIMEOUTS.get(call.name, DEFAULT_TIMEOUT)
    if call.name != "Type":
        return base
    text = str(call.args.get("text") or call.args.get("recipient") or "")
    if len(text) <= TYPE_KEYSTROKE_LIMIT:
        return base
    # Bulk insert is one round trip, so the extra budget is mostly for the page reacting to
    # a large input event. Generous, because the cost of being wrong is a dead run.
    return min(base + len(text) * TYPE_SECONDS_PER_CHAR, TYPE_TIMEOUT_MAX)

#: Settle bounds. The floor keeps fast pages snappy; the ceiling stops an animation-heavy
#: page from stalling a turn forever. An adaptive per-host bound is a fast-follow.
SETTLE_MIN = 0.25
SETTLE_MAX = 3.0

#: Reads the live compose fields for the approval preview. Selectors cover Gmail's real
#: markup and the ordinary semantics a normal mail form uses.
_COMPOSE_FIELDS_JS = """
() => {
  const pick = (selectors) => {
    for (const selector of selectors) {
      const el = document.querySelector(selector);
      if (!el) continue;
      const value = ('value' in el ? el.value : el.textContent) || '';
      if (value.trim()) return value.trim();
    }
    return '';
  };
  return {
    to: pick(['[name="to"]', 'textarea[name="to"]', '[aria-label*="To" i]', '#to']),
    subject: pick([
      '[name="subjectbox"]', '[name="subject"]', '[aria-label*="Subject" i]', '#subject',
    ]),
    body: pick([
      '[g_editable="true"]', '[role="textbox"][aria-label*="Body" i]', 'textarea#body', '#body',
    ]),
  };
}
"""


#: Finds a navigation entry in the sidebar by its visible name.
#:
#: Restricted to things that navigate — links and elements with a navigational role.
#: Matching any element whose text says "Sent" would happily return a word inside a
#: message, and clicking that does nothing while looking exactly like it worked.
_SIDEBAR_JS = """
(wanted) => {
  const target = wanted.trim().toLowerCase();
  const candidates = document.querySelectorAll(
    'a[href], [role="link"], [role="menuitem"], [role="treeitem"], [role="tab"]'
  );
  for (const el of candidates) {
    const box = el.getBoundingClientRect();
    if (box.width <= 0 || box.height <= 0) continue;
    const name = (
      el.getAttribute('aria-label') || el.textContent || ''
    ).trim().toLowerCase();
    // Gmail appends unread counts: "Spam 4", "Inbox 1,234". Strip a trailing count
    // so a folder does not become unfindable the moment it has unread mail in it.
    const bare = name.replace(/[\\s,0-9]+$/, '');
    if (name === target || bare === target) {
      return { x: box.left + box.width / 2, y: box.top + box.height / 2 };
    }
  }
  return null;
}
"""


#: Gmail's own locations for the folders it ships with.
#:
#: An ALLOWLIST, and that is the whole safety argument for having navigation at all. The
#: `Navigate(url=...)` verb exists on this surface and is bound to no worker on purpose: an
#: agent that reads attacker-controlled email must never be able to load an address that
#: email chose, because that is a credential-harvest page one injected sentence away.
#:
#: Here the model supplies a NAME and this table supplies the location. Every value is a
#: fragment of Gmail's own interface — nothing the model writes can become a destination,
#: so the useful half of navigation survives and the dangerous half never exists.
GMAIL_FOLDERS: dict[str, str] = {
    "inbox": "#inbox",
    "sent": "#sent",
    "sent mail": "#sent",
    "drafts": "#drafts",
    "draft": "#drafts",
    "spam": "#spam",
    "junk": "#spam",
    "trash": "#trash",
    "bin": "#trash",
    "deleted": "#trash",
    "starred": "#starred",
    "important": "#imp",
    "snoozed": "#snoozed",
    "scheduled": "#scheduled",
    "all mail": "#all",
    "all": "#all",
    # "Archive" is not a folder in Gmail — archived mail simply leaves the inbox and stays
    # in All Mail. Sending someone to All Mail is what they actually meant; refusing on a
    # technicality would be pedantry with no upside.
    "archive": "#all",
    "archived": "#all",
}


#: Finds a mail row's action button once hovering has revealed the row toolbar.
#:
#: Coordinates cannot address these directly: the buttons do not exist until the pointer is
#: over the row, so they are absent from the observation the agent chose an index from. The
#: row's y IS known, though, so the button is identified by tooltip and then matched to the
#: row it belongs to — which is what stops "Archive" archiving the row above.
_ROW_ACTION_JS = """
(args) => {
  const selector = args.tooltips
    .map((t) => `[data-tooltip="${t}"], [aria-label="${t}"]`)
    .join(', ');

  let best = null;
  let bestDistance = Infinity;
  for (const el of document.querySelectorAll(selector)) {
    const box = el.getBoundingClientRect();
    if (box.width <= 0 || box.height <= 0) continue;
    const distance = Math.abs(box.top + box.height / 2 - args.y);
    if (distance < bestDistance) {
      bestDistance = distance;
      best = box;
    }
  }
  // Must belong to THIS row. Without the band check the nearest match could be a toolbar
  // button at the top of the page, and the action would land on the wrong thread.
  if (!best || bestDistance > args.band) return null;
  return { x: best.left + best.width / 2, y: best.top + best.height / 2 };
}
"""

#: Half a mail row, near enough. Wide enough to catch a button whose centre sits a little
#: off the row's own centre; narrow enough that the row above never wins.
ROW_BAND_PX = 26


#: How to reach each compose field WITHOUT coordinates.
#:
#: These are the same selectors the extractor uses to identify the fields, kept in step on
#: purpose: the observation tells the agent "Subject is [80]", and this is how [80] is then
#: actually reached. Matching them is what makes the promise true.
#: Tried IN ORDER, one at a time — deliberately a tuple per field, never a comma-joined
#: string.
#:
#: A comma-joined selector hands the choice to DOCUMENT ORDER: `page.focus('a, b')` focuses
#: whichever matches first on the page, not whichever selector was listed first. Gmail lays
#: the subject out above the body, so a body selector that also matched the subject sent the
#: message text into the subject line — and a "To - Select contacts" LINK sits above the real
#: recipient field, so a loose To fallback matched the link instead of the input.
#:
#: Both bugs were the same mistake: writing an ordered list and then throwing the order away.
#: These MUST stay in step with `TO_SELECTORS`/`SUBJECT_SELECTORS`/`BODY_SELECTORS` in
#: `surface/extract.py` — that file decides which index a field is reported at, this one
#: decides where the typing goes, and a disagreement puts text in the wrong box.
_FIELD_SELECTORS: dict[str, tuple[str, ...]] = {
    "to": (
        '[name="to"]',
        'textarea[name="to"]',
        '[role="combobox"][aria-label*="To" i]',
        '[aria-label*="To recipients" i]',
    ),
    "subject": (
        '[name="subjectbox"]',
        '[name="subject"]',
        '[aria-label*="Subject" i]',
        '[placeholder*="Subject" i]',
    ),
    "body": (
        '[g_editable="true"]',
        '[aria-label*="Message Body" i]',
        '[role="textbox"][aria-label*="Body" i]',
        '[contenteditable="true"]:not([role="combobox"])',
    ),
}


#: The addresses currently in the To field — committed chips AND loose typed text.
#:
#: Scoped to the recipients row, never the whole dialog: the FROM row carries the signed-in
#: user's own address in the same attributes, and treating that as "already a recipient"
#: would make it impossible to email yourself.
_RECIPIENTS_JS = """
() => {
  const dialog = document.querySelector('[role="dialog"]');
  if (!dialog) return { present: [], typed: '' };

  const input = dialog.querySelector(
    '[name="to"], textarea[name="to"], input[aria-label*="To" i]'
  );
  if (!input) return { present: [], typed: '' };

  let area = input;
  for (let i = 0; i < 4 && area.parentElement && area.parentElement !== dialog; i++) {
    area = area.parentElement;
  }

  const present = [];
  for (const el of area.querySelectorAll('[data-hovercard-id], [email]')) {
    const value = el.getAttribute('data-hovercard-id') || el.getAttribute('email') || '';
    if (value.includes('@')) present.push(value.trim().toLowerCase());
  }
  return { present, typed: ((input.value || '') + '').trim().toLowerCase() };
}
"""


class PlaywrightEmailSurface:
    """`EmailSurface` over a Playwright page."""

    def __init__(
        self,
        page: Page,
        *,
        vault: SessionPiiVault | None = None,
        tokenizer: PiiTokenizer | None = None,
        funnel: ObservationFunnel | None = None,
        bound_verbs: frozenset[str] | set[str] | None = None,
    ) -> None:
        self._page = page
        self._vault = vault or SessionPiiVault()
        self._tokenizer = tokenizer or PiiTokenizer(self._vault)

        self._funnel = funnel or ObservationFunnel(self._tokenizer)

        #: Rebuilt on every `observe()`. Never carried across turns.
        self._geometry: dict[int, tuple[float, float]] = {}
        self._last_observation: Observation | None = None
        self._previous_identities: set[str] = set()
        self._bound_verbs = frozenset(bound_verbs or TIMEOUTS.keys())
        self._approved: set[str] = set()
        self._cdp = None

    # ── screencast ──────────────────────────────────────────────────────────

    @property
    def vault(self) -> SessionPiiVault:
        """The session vault. Read by the composition root so the graph mints tokens the
        dispatcher can resolve, and by the approval card so a human sees a real address."""
        return self._vault

    async def start_screencast(
        self,
        on_frame: Callable[[str, int], Awaitable[None]],
        *,
        quality: int = 55,
        max_width: int = 1000,
    ) -> None:
        """Stream the live page to the cockpit as JPEG frames.

        **Every frame must be acknowledged or Chrome stops sending.** It emits one, waits for
        the ack, and goes quiet forever if it never comes — which looks exactly like a broken
        agent rather than a broken transport, and is the single easiest way to get this
        wrong.

        Quality and width are turned down deliberately. This is a *progress* view: the user
        is checking that the right field is being filled, not reading the page. A pristine
        1280px frame several times a second is bandwidth spent on something nobody looks at
        that closely.
        """
        session = await self._page.context.new_cdp_session(self._page)
        self._cdp = session
        loop = asyncio.get_running_loop()
        counter = count(1)

        def handle(params: dict) -> None:
            # Playwright dispatches CDP events synchronously; the ack and the emit are both
            # async, so they are scheduled rather than awaited here.
            loop.create_task(self._ack_frame(session, params, next(counter), on_frame))

        session.on("Page.screencastFrame", handle)
        await session.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": quality,
                "maxWidth": max_width,
                "maxHeight": max_width,
                # Every frame: a mail UI is mostly still, so this is cheap, and skipping
                # frames makes typing look like it happens in jumps.
                "everyNthFrame": 1,
            },
        )
        logger.info("screencast started")

    async def _ack_frame(self, session, params: dict, seq: int, on_frame) -> None:
        session_id = params.get("sessionId")
        try:
            if session_id is not None:
                await session.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            return  # page navigated or closed mid-frame; the next one will re-sync
        data = params.get("data")
        if not data:
            return
        try:
            await on_frame(data, seq)
        except Exception:
            # Never let a broken consumer kill the stream — the browser keeps working and
            # the run keeps going. But do not swallow it either: a silently failing emitter
            # produces a blank live view with no diagnostic, which is indistinguishable from
            # a hung agent and is exactly how this went unnoticed once already.
            logger.warning("screencast frame %d not delivered", seq, exc_info=True)

    @property
    def current_url(self) -> str:
        """Where this page is, raw.

        A CAPABILITY, not part of `EmailSurface` — the same treatment `start_screencast`
        gets. Neither belongs on the port: a URL is meaningless to an API-backed surface and
        a screencast is impossible for one, and widening the port to fit one implementation
        is how ports stop being swappable. Consumers ask with `getattr`.

        Never goes into an `Observation`. It reaches the authenticated cockpit and nothing
        else; see `EventEmitter.location`.
        """
        try:
            return self._page.url
        except Exception:
            return ""

    async def stop_screencast(self) -> None:
        if self._cdp is None:
            return
        with contextlib.suppress(Exception):
            await self._cdp.send("Page.stopScreencast")
        self._cdp = None

    # ── approvals ───────────────────────────────────────────────────────────

    def approve(self, fingerprint: str) -> None:
        """Record a human decision for one exact payload.

        Called by the approval gate. Nothing else may call it — a remediation strategy that
        could approve on the user's behalf would make the gate decorative.
        """
        self._approved.add(fingerprint)

    # ── the port ────────────────────────────────────────────────────────────

    async def observe(self) -> Observation:
        try:
            raw = await self._page.evaluate(EXTRACT_JS, MAX_NODES)
        except Exception as exc:  # page closed, crashed, navigating
            raise SurfaceUnavailable(f"could not read the page: {exc}") from exc

        elements = parse_elements(raw.get("elements") or [])
        meta = parse_meta(raw.get("meta") or {})

        observation, geometry, report = self._funnel.run(
            elements, meta, previous_identities=self._previous_identities
        )

        # Indices belong to THIS observation only. Replacing the map wholesale is what makes
        # a stale index from last turn a rejection rather than a misfire.
        self._geometry = geometry
        # Kept for exactly one reader: the approval check, which must be able to ask what an
        # index POINTS AT. Rebuilt every turn alongside the geometry it belongs to — a stale
        # observation here would gate the wrong element, which is worse than not gating.
        self._last_observation = observation
        self._previous_identities = identity_set(observation.elements)

        logger.debug("observed %d/%d elements", report.shown, report.extracted)
        return observation

    async def act(self, call: ActionCall) -> ActionResult:
        # Re-read the live draft before validating anything irreversible. The human approved
        # WORDS, not a button, so consent has to be checked against what the fields say at
        # the moment of dispatch — not against what they said when the card was rendered.
        # Any edit in between invalidates the approval, and the gate asks again.
        preview = ""
        if is_irreversible(call, self._last_observation):
            with contextlib.suppress(Exception):
                preview = await self.preview(call)
        try:
            resolved = self._validator(preview).validate(call)
        except DispatchRejected as rejection:
            # A refusal is information, not a crash: the agent sees a typed failure and can
            # re-observe or choose differently.
            logger.info("dispatch rejected %s: %s", call.name, rejection.reason)
            return rejection.to_result()

        timeout = timeout_for(call)
        try:
            result = await asyncio.wait_for(self._perform(resolved), timeout=timeout)
        except TimeoutError:
            return ActionResult(
                success=False,
                reason=f"{call.name} exceeded its {timeout}s wall",
                error_code="ACTION_TIMEOUT",
            )
        except SurfaceUnavailable:
            raise
        except Exception as exc:
            return ActionResult(success=False, reason=f"{call.name} failed: {exc}")

        await self._settle()
        return result

    def _validator(self, preview: str = "") -> ActionValidator:
        return ActionValidator(
            vault=self._vault,
            geometry=self._geometry,
            bound_verbs=self._bound_verbs,
            approved=self._approved,
            # So the approval check can ask what an index points at. Same turn as the
            # geometry it is validated against — a stale one would gate the wrong element.
            observation=self._last_observation,
            preview=preview,
        )

    async def preview(self, call: ActionCall) -> str:
        """What this action will actually do, with tokens resolved.

        Read from the LIVE compose fields rather than reconstructed from what the agent
        thinks it typed. Those are two different things whenever a field rejected input,
        autocompleted, or was edited by a take-over — and the whole value of the card is
        that the human sees what is really there.
        """
        # By CONSEQUENCE, not by verb name — the same rule the GATE uses.
        #
        # Gating became consequence-based when `Click` on Gmail's Send button turned out to
        # send mail just as surely as the `Send` verb. The preview was left behind on the
        # old verb-name test, so exactly that action produced a card reading
        # `Click(index=83)` — an approval prompt for the most irreversible thing this
        # product does, showing the human a number instead of the email. Approving what you
        # cannot read is not approval.
        sends_mail = call.name in ("Send", "SendInvite") or (
            is_irreversible(call, self._last_observation)
            and getattr(getattr(self._last_observation, "mail", None), "compose_open", False)
        )
        if not sends_mail:
            return f"{call.name}({', '.join(f'{k}={v!r}' for k, v in call.args.items())})"

        try:
            fields = await self._page.evaluate(_COMPOSE_FIELDS_JS)
        except Exception:  # page closed or navigating
            return "(could not read the draft — check the browser view before approving)"

        lines = [
            f"To:      {fields.get('to') or '(empty)'}",
            f"Subject: {fields.get('subject') or '(empty)'}",
            "",
            (fields.get("body") or "(empty body)").strip(),
        ]
        return "\n".join(lines)

    # ── verbs ───────────────────────────────────────────────────────────────

    async def _perform(self, action: ResolvedAction) -> ActionResult:
        handler = getattr(self, f"_do_{action.verb.lower()}", None)
        if handler is None:
            return ActionResult(
                success=False,
                reason=f"{action.verb} has no handler",
                error_code="VERB_NOT_BOUND",
            )
        return await handler(action)

    async def _do_readthread(self, action: ResolvedAction) -> ActionResult:
        """Open a thread — which, on a mail surface, is a click on its row.

        Bound as its own verb because "read this thread" is what the model means, and making
        it say `Click` for that loses the intent from the trajectory. Without a handler here
        the tool was bindable but not performable, which the model discovered the hard way.
        """
        if action.point is None:
            return ActionResult(success=False, reason="ReadThread needs an index")
        x, y = action.point
        await self._page.mouse.move(x, y)
        await self._page.mouse.click(x, y)
        return ActionResult(
            success=True, reason=f"opened thread [{action.call.args.get('index')}]"
        )

    async def _do_click(self, action: ResolvedAction) -> ActionResult:
        if action.point is None:
            return ActionResult(success=False, reason="Click needs an index")
        x, y = action.point
        # Move first: hover state is real, and handlers bound to pointerenter will not fire
        # for a click that teleports.
        await self._page.mouse.move(x, y)
        await self._page.mouse.click(x, y)
        return ActionResult(success=True, reason=f"clicked [{action.call.args.get('index')}]")

    async def _already_addressed(self) -> set[str]:
        """Addresses already in the To field, lowercased. Empty when it cannot be read.

        Failing open is deliberate: this is a duplicate check, and a diagnostic query that
        breaks must not block a recipient the user actually asked for.
        """
        try:
            state = await self._page.evaluate(_RECIPIENTS_JS)
        except Exception:  # page closed, navigating, no dialog
            return set()
        present = {a for a in state.get("present", []) if a}
        typed = (state.get("typed") or "").strip()
        if typed:
            present.add(typed)
        return present

    def _compose_field_for(self, index: object) -> str | None:
        """Which compose field this index was reported as, if any.

        Read from the SAME observation the agent acted on, so the answer is exactly what it
        was told — the index and the field agree by construction rather than by hope.
        """
        mail = getattr(self._last_observation, "mail", None)
        if mail is None or not isinstance(index, int) or isinstance(index, bool):
            return None
        for name, reported in (
            ("to", mail.to_index),
            ("subject", mail.subject_index),
            ("body", mail.body_index),
        ):
            if reported is not None and reported == index:
                return name
        return None

    async def _focus_field(self, field: str) -> bool:
        """Put the caret in a compose field by SELECTOR, never by coordinates.

        **This is the fix for text landing in the wrong field**, and coordinates are the
        whole reason it happened. Observed live: the agent was correctly told the subject
        was at [80], we clicked [80]'s captured position — and between the observation and
        the click, committing the recipient closed Gmail's autocomplete dropdown and the
        dialog reflowed (the element count fell from 80 to 74). The subject had moved; the
        body had slid up into those coordinates. "Good Evening" went into the body, the
        subject stayed empty, and every confused turn afterwards followed from that.

        No settle delay fixes this reliably — the page may reflow again at any moment, and a
        coordinate is a bet that it will not. A selector names the field itself, so it is
        correct however the layout moves. `focus()` also scrolls the field into view, which
        removes a second class of failure: typing into something technically present but
        off-screen.

        Falls back to the caller's coordinate click when the selector finds nothing, so a
        Gmail variant this does not recognise degrades to the old behaviour rather than
        refusing to type at all.
        """
        for selector in _FIELD_SELECTORS[field]:
            try:
                await self._page.focus(selector, timeout=1500)
                return True
            except Exception:
                continue
        logger.debug("could not focus the %s field by any known selector", field)
        return False

    async def _do_type(self, action: ResolvedAction) -> ActionResult:
        text = self._text_for(action)
        recipient_field = _is_recipient_arg(action)
        field = self._compose_field_for(action.call.args.get("index"))

        if field == "to" and TOKEN_RE.search(text):
            # ── a token reached the To field as literal characters ──
            #
            # `text` is post-resolution, so a `P17` still in it means the dispatcher did NOT
            # recognise this value as tokens and passed it through verbatim. Typing it puts
            # the characters "P1 P3" into Gmail as if they were an address — which is what
            # happened when a human separated two recipients with a space.
            #
            # A literal ADDRESS in this field is already refused (`UNKNOWN_TOKEN`); a
            # literal token is the same class of mistake and gets the same answer. Refusing
            # is what makes it visible: typed, in the trajectory, and fixable next turn,
            # rather than a garbage recipient shown to a human as a real draft.
            return ActionResult(
                success=False,
                reason=(
                    "that is a vault token, not an address — it would be typed into Gmail "
                    "as literal text. Pass recipients as the `recipient` argument, or as "
                    "comma-separated tokens (P1, P3), so they can be resolved."
                ),
                error_code="UNRESOLVED_TOKEN",
            )

        if recipient_field:
            # ── the duplicate-recipient guard ──
            #
            # Observed live, twice, and it is not a reasoning failure the prompt can fix.
            # Indices are rebuilt every turn by design, so the index the agent typed into
            # last turn now points at something else entirely. Re-reading the new list it
            # sees a "button" where it believes it put a recipient, concludes its own action
            # failed, and types the address a second time — leaving a committed chip AND
            # loose text, which is what the user sees in the compose window.
            #
            # The agent cannot verify this for itself: it is never shown field CONTENTS, so
            # "is my recipient already there?" is genuinely unanswerable from its side. Only
            # this side can answer it, so this is where the answer belongs — the same
            # reasoning as `COMPOSE_ALREADY_OPEN`, and the same shape of fix.
            #
            # Refusing outright would break "also add Bob", so already-present addresses are
            # DROPPED and only genuinely new ones are typed. Adding a recipient later works;
            # adding the same one twice becomes impossible.
            present = await self._already_addressed()
            fresh = new_recipients(text, present)
            if present and not fresh:
                token = action.call.args.get("recipient") or action.call.args.get("text")
                return ActionResult(
                    success=False,
                    reason=(
                        f"{token} is already in the To field — nothing to add. The "
                        "recipient is done; move on to the subject or the body."
                    ),
                    error_code="RECIPIENT_ALREADY_PRESENT",
                )
            # Re-joined with COMMAS, always — not only when something was already there.
            #
            # Resolution substitutes token for address in place, so it inherits whatever
            # separated them: `"P1 P2"` becomes `"priya@corp.com alex@corp.com"`. Gmail's To
            # field commits a chip on a comma; two addresses separated by a space are one
            # unbroken string to it, and a single trailing Enter turns them into one
            # malformed recipient. This used to run only inside the duplicate guard, so an
            # EMPTY To field — the common case — skipped the normalization entirely.
            if fresh:
                text = ", ".join(fresh)

        # A known compose field is reached by selector; anything else still goes by the
        # index the agent was given.
        if field:
            if not await self._focus_field(field):
                # Refuse rather than fall back to a coordinate click.
                #
                # The fallback is what put the message body into the subject line: unable to
                # reach the body by selector, it clicked the body's coordinates, the caret
                # was still in the subject, and the text went there — then the verification
                # could not find the body either, reported "did not land", and the agent
                # retyped into the subject again. A field we have NAMED but cannot reach is
                # a gap in our own knowledge; guessing with coordinates turns that into
                # corrupted text in a field the human never sees us touch.
                return ActionResult(
                    success=False,
                    reason=(
                        f"could not reach the {field} field — nothing was typed. "
                        "Re-observe and try the field from the element list."
                    ),
                    error_code="FIELD_UNREACHABLE",
                )
        elif action.point is not None:
            x, y = action.point
            await self._page.mouse.click(x, y)

        if len(text) > TYPE_KEYSTROKE_LIMIT:
            # One CDP round trip instead of three per character. `Input.insertText` is a
            # real CDP input event — the page sees a trusted `beforeinput`/`input`, which is
            # what a contenteditable body actually listens for. It does NOT synthesize
            # individual key events, which is precisely why short fields are excluded: an
            # address box builds its recipient chip from keystrokes.
            await self._insert_text(text)
        else:
            await self._page.keyboard.type(text, delay=12)
        if recipient_field:
            # Commit the address into a chip.
            #
            # Gmail's To field is an autocomplete: typing leaves loose text with a suggestion
            # dropdown hanging open over the compose window, and NOTHING is committed until
            # Enter. Two things go wrong if we skip it — the address may never be attached to
            # the draft at all, and the open dropdown covers the subject and body, so the very
            # next observation cannot see the fields the agent needs next. It then hunts for a
            # subject line that is sitting underneath a suggestion list.
            #
            # Enter also accepts the highlighted suggestion, which is what a human does.
            await self._page.keyboard.press("Enter")

        # Did it land where it was aimed?
        #
        # The failure this catches is silent by nature: text goes into the wrong field, the
        # action reports success, and the agent proceeds on a false belief — which is how
        # "Good Evening" ended up in the body while the subject stayed empty, and why the
        # next six turns made no sense. Checking costs one DOM read and converts an
        # invisible corruption into a typed failure the agent can actually respond to.
        #
        # Only for the fields whose state is knowable; a click into arbitrary page content
        # has no postcondition to check.
        if field and text:
            landed = await self._field_has_content(field)
            # `None` means unfindable, not empty — see `_field_has_content`. Only a
            # confident False is a failure; anything else lets the write stand.
            if landed is False:
                return ActionResult(
                    success=False,
                    reason=(
                        f"the text did not land in the {field} field — it is still empty. "
                        "Re-observe: the compose window may have moved."
                    ),
                    error_code="TYPE_DID_NOT_LAND",
                )

        # NEVER log `text` — a recipient resolved from a token is raw PII by this point.
        return ActionResult(success=True, reason=f"typed {len(text)} characters")

    async def _field_has_content(self, field: str) -> bool | None:
        """Whether a compose field now holds anything. `None` when it cannot be read.

        **Checks EVERY match, not the first**, and that is the whole correctness of it. The
        write and the check resolve the selector differently: `page.focus()` takes the first
        ACTIONABLE match — the field you can actually see — while `eval_on_selector` takes
        the first match in DOM order, visible or not. Gmail keeps hidden legacy inputs
        beside its live fields, so the text went into the visible combobox and this read an
        empty hidden input, then failed an action that had just succeeded.

        Any match holding text means the field is written; that mirrors `filledWithin` in
        the extractor, which decides FILLED the same way.

        `None`, not `False`, when nothing can be read: an unreadable field is not an empty
        one, and reporting a good write as a failure sends the agent retyping over text that
        is already correct — the exact duplication this file works to prevent.
        """
        if field == "to":
            # ── a committed recipient is a CHIP, not text in the input ──
            #
            # **This is why the agent typed an address, watched it vanish, and typed it
            # again.** Typing a recipient ends with Enter (see `_do_type`), which is what
            # commits the address — and committing it moves the address OUT of the input
            # into a chip node and leaves `input.value` empty. Read through the To
            # selectors, a write that had just succeeded looked like an empty field, so
            # this returned False and the action reported `TYPE_DID_NOT_LAND`. Told its own
            # correct action had failed, the agent retyped.
            #
            # The knowledge to answer properly was already in this file: `_RECIPIENTS_JS`
            # reads chips. So the same address was "already present" to the duplicate guard
            # and "never landed" to this check, in the same turn — two halves of one file
            # disagreeing about one fact. They share the chip-aware read now.
            #
            # Never a confident False: that query returns an empty set both for "no
            # recipient" and for "no dialog to read", and this file's standing rule is that
            # only a confident False is a failure. A recipient that genuinely did not land
            # is caught by the next observation, where `toFilled` says so.
            return True if await self._already_addressed() else None

        seen = False
        for selector in _FIELD_SELECTORS[field]:
            try:
                matches = await self._page.eval_on_selector_all(
                    selector,
                    "els => els.map(el => "
                    "((el.value !== undefined ? el.value : el.innerText) || '').trim())",
                )
            except Exception:
                continue
            if not matches:
                continue
            seen = True
            if any(matches):
                return True
        # Nothing matched ANY selector: the field is not "empty", it is unfindable, and the
        # difference matters. Returning False here reported a write as failed whenever the
        # selectors did not know this Gmail's markup — so the agent retyped, and the text it
        # had already put somewhere was appended a second time. "Not found" is unknown.
        return False if seen else None

    async def _insert_text(self, text: str) -> None:
        """Bulk-insert into the focused element, falling back to keystrokes.

        The fallback matters: `Input.insertText` needs a CDP session, and the surface can be
        driven in configurations where one is not available. Degrading to slow-but-correct
        beats failing.
        """
        try:
            session = self._cdp or await self._page.context.new_cdp_session(self._page)
            await session.send("Input.insertText", {"text": text})
        except Exception as exc:
            logger.info("insertText unavailable (%s); falling back to keystrokes", exc)
            await self._page.keyboard.type(text, delay=4)

    async def _do_send(self, action: ResolvedAction) -> ActionResult:
        """Send the open compose window. Only ever reached AFTER the approval gate.

        **This handler did not exist**, and its absence is why no `Send` had ever worked:
        `_perform` looks for `_do_<verb>`, found nothing, and returned `VERB_NOT_BOUND` —
        "Send has no handler" — at the exact moment a human had just approved sending. The
        agent's only way through was to fall back to clicking the Send button, which the
        worker prompt actively discourages. So the recommended path was the broken one.

        Ctrl+Enter first, the button second. The shortcut is Gmail's own and needs no
        element to be found, which makes it immune to the reflow that moves buttons around
        mid-turn; the button click is the fallback for a Gmail where the shortcut is off.
        """
        # The dialog closing is how Gmail says "sent". Captured first so the check after is
        # a comparison rather than a guess.
        await self._page.keyboard.press("Control+Enter")
        if await self._compose_closed():
            return ActionResult(
                success=True, reason="sent", undo={"kind": "send", "reversible": False}
            )

        # The shortcut can be disabled in Gmail settings. Fall back to the button.
        for selector in (
            'div[role="button"][data-tooltip^="Send"]',
            'div[role="button"][aria-label^="Send"]',
            '[data-tooltip^="Send"]',
        ):
            try:
                await self._page.click(selector, timeout=1500)
            except Exception:
                continue
            if await self._compose_closed():
                return ActionResult(
                    success=True, reason="sent", undo={"kind": "send", "reversible": False}
                )

        # Never report a send that cannot be confirmed. A false success here is the worst
        # outcome available: the agent believes the mail has gone, completes the run, and
        # the draft is still sitting open — or it tries again and sends twice.
        return ActionResult(
            success=False,
            reason="the compose window is still open — the message does not appear to have sent",
            error_code="SEND_NOT_CONFIRMED",
        )

    async def _compose_closed(self) -> bool:
        """Did the compose dialog go away? Gmail's own signal that the mail left."""
        try:
            await self._page.wait_for_selector(
                '[role="dialog"] [name="subjectbox"], [role="dialog"] [g_editable="true"]',
                state="detached",
                timeout=5000,
            )
            return True
        except Exception:
            return False

    async def _row_action(
        self,
        action: ResolvedAction,
        *,
        tooltips: tuple[str, ...],
        did: str,
    ) -> ActionResult:
        """Hover a mail row, then click one of its action buttons.

        The shared body of Archive, MarkRead and DeleteForever — all the same interaction,
        differing only in which button. Written once because three copies of a hover-then-
        find-the-button dance would drift, and the one that drifted would act on the wrong
        row silently.

        **Deliberately not keyboard shortcuts.** `e` archives and `#` deletes in Gmail, and
        both would be shorter than this — but shortcuts are OFF by default and the failure
        when they are off is silent: the keystroke goes nowhere, the page does not change,
        and the agent burns its budget re-pressing a key that will never work.
        """
        if action.point is None:
            return ActionResult(
                success=False,
                reason=f"{action.verb} needs the [N] of a mail row",
                error_code="STALE_INDEX",
            )

        x, y = action.point
        # The toolbar only exists while hovering, so this move is the action, not politeness.
        await self._page.mouse.move(x, y)

        try:
            spot = await self._page.evaluate(
                _ROW_ACTION_JS, {"y": y, "tooltips": list(tooltips), "band": ROW_BAND_PX}
            )
        except Exception as exc:  # page closed or navigating
            return ActionResult(success=False, reason=f"{action.verb} failed: {exc}")

        if spot is None:
            # Say which button was looked for. "Archive failed" sends the agent guessing;
            # naming the control lets it try the row's own menu instead.
            wanted = " or ".join(repr(t) for t in tooltips)
            return ActionResult(
                success=False,
                reason=(
                    f"no {wanted} control appeared for that row — it may not be available "
                    "in this view. Open the thread and act from inside it."
                ),
                error_code="ROW_ACTION_UNAVAILABLE",
            )

        await self._page.mouse.click(spot["x"], spot["y"])
        index = action.call.args.get("index")
        return ActionResult(
            success=True,
            reason=f"{did} [{index}]",
            undo={"kind": action.verb.lower(), "index": index},
        )

    async def _do_archive(self, action: ResolvedAction) -> ActionResult:
        """Archive one thread. Reversible — it stays searchable in All Mail."""
        return await self._row_action(
            action, tooltips=("Archive",), did="archived"
        )

    async def _do_markread(self, action: ResolvedAction) -> ActionResult:
        return await self._row_action(
            action, tooltips=("Mark as read",), did="marked read"
        )

    async def _do_deleteforever(self, action: ResolvedAction) -> ActionResult:
        """Permanently delete. Gated, and matched narrowly on purpose.

        **Only "Delete forever" counts.** Gmail's ordinary "Delete" moves a thread to Trash,
        which is reversible and available on every row; "Delete forever" exists only inside
        Trash and Spam. Accepting the former would report a permanent deletion that had not
        happened — and this verb is gated precisely because the human is approving something
        that cannot be undone. Better to refuse than to overstate what was done.
        """
        return await self._row_action(
            action, tooltips=("Delete forever",), did="permanently deleted"
        )

    async def _do_label(self, action: ResolvedAction) -> ActionResult:
        """Apply a label. Opens Gmail's label menu, then picks the entry if it is there.

        Two steps, and the second is allowed to fail into the loop rather than be forced.
        The menu's contents are a mailbox's own labels — unknowable from here — so if the
        named one is not found, the menu is left OPEN and the agent is told to choose from
        the list. The next observation contains those entries, which is exactly the
        observe-then-act cycle this loop is built around.
        """
        opened = await self._row_action(
            action, tooltips=("Labels", "Label as", "Move to"), did="opened the label menu"
        )
        if not opened.success:
            return opened

        wanted = str(action.call.args.get("label") or "").strip()
        if not wanted:
            return ActionResult(
                success=True,
                reason="the label menu is open — pick a label from the list",
            )
        return await self._pick_menu_entry(wanted, did=f"labelled {wanted!r}")

    async def _do_snooze(self, action: ResolvedAction) -> ActionResult:
        """Snooze a thread. Same shape as Label: open the menu, then pick if we can."""
        opened = await self._row_action(action, tooltips=("Snooze",), did="opened snooze")
        if not opened.success:
            return opened

        wanted = str(action.call.args.get("until") or "").strip()
        if not wanted:
            return ActionResult(
                success=True,
                reason="the snooze menu is open — pick a time from the list",
            )
        return await self._pick_menu_entry(wanted, did=f"snoozed until {wanted!r}")

    async def _pick_menu_entry(self, wanted: str, *, did: str) -> ActionResult:
        """Click a menu entry by its visible text, or leave the menu open and say so.

        Matching is case-insensitive and substring-based because a human writes "tomorrow"
        where Gmail writes "Tomorrow morning, 8:00 AM". A miss is NOT a failure: the menu is
        on screen, so the next observation lists every entry and the agent can click one by
        index. Forcing an exact match would turn a solvable turn into a dead end.
        """
        try:
            entry = self._page.get_by_role("menuitem", name=wanted, exact=False).first
            await entry.click(timeout=2000)
        except Exception:
            return ActionResult(
                success=True,
                reason=(
                    f"the menu is open but nothing matched {wanted!r} — choose the closest "
                    "entry from the list"
                ),
            )
        return ActionResult(success=True, reason=did)

    async def _do_openfolder(self, action: ResolvedAction) -> ActionResult:
        """Go to a folder or label by NAME.

        Two routes, and the order matters. **The sidebar link first**, because clicking what
        a human would click works for a user's own labels as well as the built-in folders,
        and needs no knowledge of how Gmail spells anything. **The allowlisted location
        second**, for when the sidebar is collapsed or the label is hidden behind "More".

        A name that matches neither is refused, and refused by NAME so the agent can try
        another. It is never turned into a URL guess: the moment a folder name can become an
        arbitrary address, an injected "check yourdomain.example/login" becomes navigable,
        and that is the entire reason `Navigate` is bound to nobody.
        """
        wanted = str(action.call.args.get("folder") or "").strip()
        if not wanted:
            return ActionResult(
                success=False,
                reason="OpenFolder needs a folder name",
                error_code="FOLDER_UNKNOWN",
            )

        if await self._click_sidebar(wanted):
            await self._settle()
            return ActionResult(success=True, reason=f"opened {wanted}")

        destination = GMAIL_FOLDERS.get(wanted.lower())
        if destination is not None:
            # Same page, Gmail's own fragment — not a navigation to anywhere new.
            await self._page.evaluate("hash => { location.hash = hash; }", destination)
            await self._settle()
            return ActionResult(success=True, reason=f"opened {wanted}")

        known = ", ".join(sorted({v for v in GMAIL_FOLDERS}))
        return ActionResult(
            success=False,
            reason=(
                f"no folder or label called {wanted!r} is visible. Known folders: {known}. "
                "For your own labels, click the name in the sidebar list."
            ),
            error_code="FOLDER_UNKNOWN",
        )

    async def _click_sidebar(self, wanted: str) -> bool:
        """Click the sidebar entry whose name matches. False if there is no such entry.

        Case-insensitive and exact-after-trim: "sent" must not match "Sent Items Archive" if
        a user happens to have a label by that name. A near-miss here navigates somewhere
        the human did not ask for, and the agent then reads the wrong mailbox with complete
        confidence.
        """
        try:
            found = await self._page.evaluate(_SIDEBAR_JS, wanted)
        except Exception:
            return False
        if not found:
            return False
        await self._page.mouse.click(found["x"], found["y"])
        return True

    #: How many chips one Clear will remove before giving up.
    #:
    #: A bound, not a guess at how many recipients exist: without one, a Backspace that stops
    #: deleting (focus lost, a field that is not a recipient list) becomes an infinite loop
    #: inside a single action, and the timeout wall is the only thing that would end it.
    MAX_CHIPS = 25

    async def _do_clear(self, action: ResolvedAction) -> ActionResult:
        field = self._compose_field_for(action.call.args.get("index"))

        if field == "to":
            return await self._clear_recipients()

        if action.point is not None:
            x, y = action.point
            await self._page.mouse.click(x, y)
        await self._page.keyboard.press("Control+A")
        await self._page.keyboard.press("Delete")
        return ActionResult(success=True, reason="cleared the field")

    async def _clear_recipients(self) -> ActionResult:
        """Empty the To field — committed chips included.

        **This is the primitive whose absence sent the agent hunting for a "×" that is not
        in the observation.** A committed recipient is a chip, a separate node with no
        accessible name, so it never survives the funnel. Told to click it, the agent
        scrolled six times, ran Extract twice, and finally asked the human for an index
        number — a value that is internal, rebuilt every turn, and that no person has ever
        seen. It had no legal move, because we had asked for one that does not exist.

        `Ctrl+A, Delete` is why the old instruction FORBADE clearing this field: it empties
        the loose text beside the chip and leaves the recipient attached, so the next typed
        address is added alongside rather than instead. That is a missing capability, not a
        law of nature — Gmail deletes the last chip on Backspace against an empty input,
        which is exactly what a person does, and it is a trusted keystroke.

        So the executor does the whole job in one verb, and the model gets back the
        instruction it can actually follow: clear, then type.
        """
        if not await self._focus_field("to"):
            return ActionResult(
                success=False,
                reason="could not reach the To field — nothing was cleared.",
                error_code="FIELD_UNREACHABLE",
            )

        # Any half-typed text first, so the Backspaces below land on chips rather than on
        # the characters sitting beside them.
        await self._page.keyboard.press("Control+A")
        await self._page.keyboard.press("Delete")

        removed = 0
        for _ in range(self.MAX_CHIPS):
            if not await self._already_addressed():
                break
            await self._page.keyboard.press("Backspace")
            removed += 1

        # Read the truth back rather than trusting the keystrokes. A recipient believed
        # removed and still attached is how one mail goes to two people.
        if remaining := await self._already_addressed():
            return ActionResult(
                success=False,
                reason=(
                    f"the To field still holds {len(remaining)} recipient(s) after "
                    "clearing. Do not type another address on top — re-observe first."
                ),
                error_code="RECIPIENTS_NOT_CLEARED",
            )
        return ActionResult(
            success=True,
            reason=f"cleared the To field ({removed} recipient(s) removed)",
            undo={"verb": "Clear", "field": "to", "removed": removed},
        )

    async def _do_presskey(self, action: ResolvedAction) -> ActionResult:
        key = str(action.call.args.get("key") or "Enter")
        await self._page.keyboard.press(key)
        return ActionResult(success=True, reason=f"pressed {key}")

    async def _do_scroll(self, action: ResolvedAction) -> ActionResult:
        """Scroll, and say honestly whether anything moved.

        **An unconditional success here is what let one run burn twenty turns.** Inside an
        open compose dialog the wheel does nothing — the dialog does not scroll — and this
        reported "scrolled down" every time. The agent was looking for an element that was
        never in the observation, was told six times that it had successfully scrolled
        towards it, and had no reason to stop. Scroll is excluded from the repetition guard
        by design (it is a legitimate thing to repeat), so nothing else was going to catch
        it either.

        A scroll that does not move the page is a FAILURE, and a typed one: it is the only
        evidence available that "look further down" is not the answer.
        """
        direction = str(action.call.args.get("direction") or "down")
        amount = float(action.call.args.get("amount") or 1)
        height = self._page.viewport_size["height"] if self._page.viewport_size else 800
        delta = height * amount * (1 if direction == "down" else -1)

        before = await self._scroll_signature()
        await self._page.mouse.wheel(0, delta)
        # Scrolling is animated; reading the position back immediately reads the old one.
        await asyncio.sleep(SETTLE_MIN)
        after = await self._scroll_signature()

        if before is not None and after is not None and before == after:
            return ActionResult(
                success=False,
                reason=(
                    f"nothing moved — the page is already at the {direction} limit, or "
                    "what you are looking at (an open dialog) does not scroll. Scrolling "
                    "again will not help. If an element is not in the list, it did not "
                    "survive the observation and is not further down."
                ),
                error_code="SCROLL_NO_EFFECT",
            )
        return ActionResult(success=True, reason=f"scrolled {direction}")

    async def _scroll_signature(self) -> str | None:
        """Where everything on the page is scrolled to, as one comparable value.

        `window.scrollY` alone is not enough: Gmail scrolls inner containers, so the window
        never moves and a real scroll would look like a no-op. Summing every scrolled
        element covers both, and a sum is enough because the question is only "did anything
        change", never "what changed".

        `None` when the page cannot be read — an unreadable page is not a page that failed
        to scroll, and this must never invent a failure.
        """
        try:
            return await self._page.evaluate(
                "() => {"
                "  let total = 0;"
                "  for (const el of document.querySelectorAll('*')) {"
                "    total += el.scrollTop + el.scrollLeft;"
                "  }"
                "  return `${window.scrollY}|${window.scrollX}|${total}`;"
                "}"
            )
        except Exception:
            return None

    async def _do_waitfor(self, action: ResolvedAction) -> ActionResult:
        seconds = min(float(action.call.args.get("seconds") or 1.0), TIMEOUTS["WaitFor"])
        await asyncio.sleep(seconds)
        return ActionResult(success=True, reason=f"waited {seconds}s")

    async def _do_navigate(self, action: ResolvedAction) -> ActionResult:
        url = str(action.call.args.get("url") or "")
        if not url:
            return ActionResult(success=False, reason="Navigate needs a url")
        await self._page.goto(url, wait_until="domcontentloaded")
        return ActionResult(success=True, reason="navigated")

    @staticmethod
    def _is_recipient_arg(action: ResolvedAction) -> bool:
        return _is_recipient_arg(action)

    def _text_for(self, action: ResolvedAction) -> str:
        """The literal text to type, with tokens swapped for real values.

        This is the ONLY moment a real address exists outside the vault, and it exists for
        exactly as long as it takes to reach the keyboard.
        """
        resolved = action.resolved_args or {}
        for arg in ("recipient", "cc", "bcc", "text"):
            raw = str(action.call.args.get(arg) or "")
            if not raw:
                continue
            # Prose goes in verbatim. `text` carries email bodies, and business writing says
            # "the P2 bug" and "Q1 targets" constantly — substituting inside a sentence would
            # rewrite one of those into somebody's address, which is a worse failure than the
            # one this method exists to fix and one nobody would think to look for.
            if arg == "text" and not _is_all_tokens(raw):
                return raw
            for token, real in resolved.items():
                raw = raw.replace(token, real)
            return raw
        return ""

    # ── settling ────────────────────────────────────────────────────────────

    async def _settle(self) -> None:
        """Wait for the page to go quiet before the next observation.

        Reading a half-rendered page produces an element list that is wrong in the most
        expensive way: plausible. The agent acts on it, the action lands somewhere
        unintended, and the failure surfaces turns later with no obvious cause.
        """
        try:
            await self._page.wait_for_load_state("networkidle", timeout=SETTLE_MAX * 1000)
        except Exception:
            # Networkidle never arriving is normal on a live mailbox — long-polling keeps a
            # connection open forever. Fall back to the floor rather than failing the action.
            await asyncio.sleep(SETTLE_MIN)


def _browser_root() -> Path:
    """Where Playwright keeps downloaded browsers."""
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def resolve_chromium(*, headless: bool = True) -> str | None:
    """Find an installed Chromium, preferring the newest build present.

    Playwright pins an exact build per release and refuses to launch without it. That is
    reasonable for a CI image and unhelpful on a constrained machine: the download is
    ~150MB, and `playwright install` **exits 0 even when it fails**, so a missing browser
    surfaces much later as "executable doesn't exist" from an unrelated test.

    A slightly older build runs this funnel identically — nothing here depends on a
    bleeding-edge Chrome — so falling back to one that is already on disk is strictly
    better than refusing to run. `PLAYWRIGHT_CHROMIUM_PATH` overrides everything.
    """
    explicit = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH")
    if explicit and Path(explicit).exists():
        return explicit

    root = _browser_root()
    if not root.is_dir():
        return None

    prefix = "chromium_headless_shell-" if headless else "chromium-"
    binary = "chrome-headless-shell.exe" if sys.platform == "win32" else "chrome-headless-shell"
    if not headless:
        binary = "chrome.exe" if sys.platform == "win32" else "chrome"

    def build_number(path: Path) -> int:
        tail = path.name.rsplit("-", 1)[-1]
        return int(tail) if tail.isdigit() else 0

    candidates = sorted(
        (p for p in root.iterdir() if p.is_dir() and p.name.startswith(prefix)),
        key=build_number,
        reverse=True,
    )
    for candidate in candidates:
        found = next(candidate.rglob(binary), None)
        if found is not None:
            return str(found)
    return None


async def connect_surface(
    *,
    endpoint: str,
    start_url: str | None = None,
    auto_launch: bool = True,
    profile_dir: str | None = None,
    **surface_kwargs: Any,
) -> tuple[PlaywrightEmailSurface, Any]:
    """Attach to a browser that is already running — and already signed in.

    Google refuses its sign-in flow inside an automation-controlled browser, and no launch
    flag reliably changes that. Rather than fight the check, skip the situation it is
    checking for: the human signs in normally, in their own browser, and the agent joins a
    session that is already authenticated.

    When nothing is listening and `auto_launch` is set, we start that browser ourselves on
    a profile that has already been signed into — see `_ensure_browser`. The human runs one
    command, once, ever.

    We open our **own tab** in the existing profile rather than seizing one of theirs, and
    on shutdown we close only that tab and disconnect. Closing a user's browser out from
    under them because a run finished would be its own kind of bug.
    """

    await _ensure_browser(endpoint, auto_launch=auto_launch, profile_dir=profile_dir)

    manager = await _start_playwright()
    try:
        browser = await manager.chromium.connect_over_cdp(endpoint)
    except Exception as exc:
        await manager.stop()
        raise SurfaceUnavailable(
            f"could not attach to a browser at {endpoint} ({exc}). Something is listening "
            "on that port but it is not a Chrome debugging endpoint; free the port or "
            "point CDP_ENDPOINT elsewhere. See docs/RUNNING.md."
        ) from exc

    # Reuse the signed-in profile's context. A fresh context would have no cookies, which
    # is the entire thing we came here for.
    context = browser.contexts[0] if browser.contexts else await browser.new_context()

    # Reuse a Gmail tab the user already has open, rather than always opening our own.
    #
    # Opening a fresh tab meant cold-booting Gmail on **every run** — measured at 37 seconds
    # before the agent could take a single action, during which the cockpit showed nothing
    # and the run looked hung. It also meant the agent worked in a tab the human was not
    # looking at, which is its own quiet problem.
    page = _existing_mail_tab(context, start_url)
    ours = page is None
    if page is None:
        page = await context.new_page()
        if start_url:
            await page.goto(start_url, wait_until="domcontentloaded")

    surface = PlaywrightEmailSurface(page, **surface_kwargs)

    # After navigating, never before: `navigator.userAgentData` is undefined on about:blank,
    # so checking too early silently reports nothing and the warning never fires.
    await _warn_if_google_will_reject(page)

    # `domcontentloaded` fires long before Gmail has rendered anything — the first
    # observation came back with FOUR elements on a real inbox, and the agent then reasoned
    # about a mailbox that had not drawn yet.
    await _wait_until_ready(page)

    async def close() -> None:
        await surface.stop_screencast()
        # Only a tab we opened. Closing the human's own Gmail tab because a run finished
        # would be its own kind of bug — and on the reuse path that is exactly what this
        # would do.
        if ours:
            with contextlib.suppress(Exception):
                await page.close()
        with contextlib.suppress(Exception):
            await browser.close()
        await manager.stop()

    return surface, close


async def _ensure_browser(
    endpoint: str,
    *,
    auto_launch: bool,
    profile_dir: str | None,
) -> None:
    """Make sure something is listening on the debugging port before Playwright dials it.

    Every one of these paths otherwise arrives as the same `ECONNREFUSED`, and the right
    action differs completely between them: start the browser, sign in, or close the copy
    of Chrome that is squatting the profile. Telling them apart is the whole job here.
    """
    from app.surface import chrome_launcher as launcher

    host, port = launcher.endpoint_host_port(endpoint)
    if await launcher.port_is_open(host, port):
        return

    if not auto_launch:
        raise SurfaceUnavailable(
            f"nothing is listening at {endpoint}, and CDP_AUTO_LAUNCH is off. Run "
            "`python scripts/chrome.py serve --isolated`, or set CDP_AUTO_LAUNCH=true."
        )
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SurfaceUnavailable(
            f"nothing is listening at {endpoint}. It is not a local address, so this "
            "process cannot start it — bring that browser up yourself."
        )

    data_dir = Path(profile_dir) if profile_dir else launcher.ISOLATED_PROFILE

    # First run: nothing here has ever browsed, so there are no Gmail cookies to reuse and
    # attaching would only land on the login wall. Open an ordinary window instead — no
    # debugging port, which is the one configuration Google's sign-in accepts — and stop.
    profile = launcher.signed_in_profile(data_dir)
    if profile is None:
        try:
            launcher.spawn(port=None, data_dir=data_dir)
        except launcher.ChromeNotFound as exc:
            raise SurfaceUnavailable(str(exc)) from exc
        raise SurfaceUnavailable(
            "this is the first run, so I opened a normal Chrome window on the agent's own "
            f"profile ({data_dir}). Sign into Gmail there, close that window, then send "
            "your message again — after this the agent starts the browser itself. The "
            "window has no debugging port on purpose: that is the only way Google accepts "
            "a sign-in."
        )

    # `profile`, not `Default`. Signing in through the account picker can put the session in
    # a numbered profile and leave `Default` pristine, and attaching to that empty default
    # strands the agent on a login page inside a browser that is, in fact, signed in.
    logger.info("nothing at %s; starting Chrome on %s / %s", endpoint, data_dir, profile)
    try:
        launcher.spawn(port=port, data_dir=data_dir, profile_directory=profile)
    except launcher.ChromeNotFound as exc:
        raise SurfaceUnavailable(str(exc)) from exc

    if not await launcher.wait_for_port(host, port):
        raise SurfaceUnavailable(
            f"started Chrome but nothing ever listened on port {port}. Chrome silently "
            "drops --remote-debugging-port when another instance already owns the profile "
            f"at {data_dir}; close any window using it (check the system tray) and try "
            "again."
        )


#: Fields whose value is an ADDRESS and which need committing rather than merely filling.
def new_recipients(text: str, present: set[str]) -> list[str]:
    """The addresses in `text` that are not already in the field, in order.

    Split out so the decision is testable without a browser: reading the live page needs
    Chrome, but "which of these are new?" is arithmetic and deserves exhaustive cases.

    Case-insensitive, because Gmail echoes an address back in whatever case it was typed
    and a capital letter is not a different person.

    Whitespace separates as surely as a comma does — an address cannot contain a space, so
    there is nothing to lose by splitting on both. Comma-only was the fourth copy of one
    assumption, and it fails the same way as the others: `"a@x.com b@y.com"` counted as a
    single unfamiliar address, so the duplicate guard could not see that one of the two was
    already in the field.
    """
    wanted = [part for part in re.split(r"[,;\s]+", text.strip()) if part]
    return [address for address in wanted if address.lower() not in present]


RECIPIENT_ARGS = ("recipient", "cc", "bcc")


def _is_recipient_arg(action: ResolvedAction) -> bool:
    """Does this `Type` need an Enter after it to take effect?

    True for the address arguments, and equally for a `text` value that is nothing but a
    token — because that is a person being entered into a field, whichever field it is. In a
    To box Enter builds the chip; in the search box Enter runs the search. Both are what a
    human does next, and neither is what happens if we stop at typing.

    Decided from the ARGUMENT the model used, never the element's name: a name is page text
    and therefore untrusted, and "To" is localised.
    """
    if any(str(action.call.args.get(arg) or "").strip() for arg in RECIPIENT_ARGS):
        return True
    text = str(action.call.args.get("text") or "")
    return bool(text.strip()) and _is_all_tokens(text)


#: How long to wait for a mail UI to actually draw. Generous, because a cold Gmail on a
#: slow connection genuinely takes this long — and returning early is worse than waiting:
#: the agent burns a turn reasoning about a blank page.
READY_TIMEOUT_SECONDS = 25.0

#: What "rendered" looks like. Any one of these means the app has drawn something the agent
#: could act on. Deliberately several: Gmail's markup changes, and a selector that silently
#: stops matching would reintroduce the empty-page bug with no signal.
READY_SELECTORS = (
    "div[gh='cm']",  # Compose
    "table[role='grid'] tr",  # a mail row
    "div[role='main']",
    "input[name='identifier']",  # the sign-in wall counts as ready: the loop names it
)


async def _wait_until_ready(page: Any) -> None:
    """Wait until the mail app has drawn, or give up and let the funnel report what it sees.

    Giving up is not a failure: the loop handles a thin observation perfectly well, and
    raising here would turn a slow network into a dead run.
    """
    import asyncio as _asyncio

    deadline = _asyncio.get_running_loop().time() + READY_TIMEOUT_SECONDS
    while _asyncio.get_running_loop().time() < deadline:
        with contextlib.suppress(Exception):
            for selector in READY_SELECTORS:
                if await page.query_selector(selector) is not None:
                    logger.info("mail UI ready (%s)", selector)
                    return
        await _asyncio.sleep(0.25)
    logger.warning(
        "the mail UI did not finish rendering within %.0fs; observing whatever is there",
        READY_TIMEOUT_SECONDS,
    )


def _existing_mail_tab(context: Any, start_url: str | None) -> Any | None:
    """A tab already showing the mail app, if the user has one open.

    Matched on host rather than the full URL: the user is on `#inbox`, `#sent`, or a thread,
    and demanding an exact match would reject every tab that is genuinely already there.
    """
    if not start_url:
        return None
    from urllib.parse import urlparse

    host = urlparse(start_url).hostname or ""
    if not host:
        return None
    for page in getattr(context, "pages", []):
        with contextlib.suppress(Exception):
            if page.is_closed():
                continue
            if urlparse(page.url).hostname == host:
                logger.info("reusing the Gmail tab already open at %s", host)
                return page
    return None


async def _warn_if_google_will_reject(page: Any) -> None:
    """Say up front when this browser is one Google refuses to sign in.

    Chrome for Testing — the build Playwright ships — has no Google API keys, so the
    sign-in flow ends at "Couldn't sign you in / This browser or app may not be secure."
    Finding that out at the login wall, after setting everything else up, is a bad way to
    spend an evening; the browser announces its own brand, so we can just ask.
    """
    with contextlib.suppress(Exception):
        brands = await page.evaluate(
            "() => (navigator.userAgentData?.brands || []).map(b => b.brand)"
        )
        if brands and not any("Google Chrome" in brand for brand in brands):
            logger.warning(
                "attached to a browser identifying as %s. Google blocks its sign-in flow "
                "on non-Google Chrome builds, so Gmail will refuse to log in here. Use "
                "your installed Chrome: `python scripts/chrome.py signin`.",
                ", ".join(brands),
            )


async def _start_playwright() -> Any:
    """Start the Playwright driver, explaining the Windows loop trap if it bites."""
    from playwright.async_api import async_playwright

    try:
        return await async_playwright().start()
    except NotImplementedError as exc:
        raise SurfaceUnavailable(
            "this event loop cannot start a browser process. On Windows, uvicorn's "
            "--reload uses SelectorEventLoop, which cannot spawn subprocesses. Start the "
            "server with `python -m app.api.dev` (or add "
            "`--loop app.api.loop:loop_factory`) to get a ProactorEventLoop."
        ) from exc


async def launch_surface(
    *,
    headless: bool = True,
    start_url: str | None = None,
    **surface_kwargs: Any,
) -> tuple[PlaywrightEmailSurface, Any]:
    """Convenience for scripts and integration tests. Returns `(surface, closer)`."""

    executable = resolve_chromium(headless=headless)

    manager = await _start_playwright()
    try:
        browser = await manager.chromium.launch(headless=headless, executable_path=executable)
    except Exception as exc:
        await manager.stop()
        raise SurfaceUnavailable(
            f"could not launch Chromium ({exc}). Run `playwright install chromium`, or set "
            "PLAYWRIGHT_CHROMIUM_PATH to an existing build."
        ) from exc

    page = await browser.new_page(viewport={"width": 1280, "height": 800})
    surface = PlaywrightEmailSurface(page, **surface_kwargs)
    if start_url:
        await page.goto(start_url, wait_until="domcontentloaded")

    async def close() -> None:
        await surface.stop_screencast()
        await browser.close()
        await manager.stop()

    return surface, close
