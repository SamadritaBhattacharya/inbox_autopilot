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
import sys
from collections.abc import Awaitable, Callable
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING, Any

from inbox_contracts import ActionCall, ActionResult, Observation

from app.observation.funnel.pipeline import ObservationFunnel
from app.observation.funnel.reading_order import identity_set
from app.security.tokenizer import PiiTokenizer
from app.security.vault import SessionPiiVault
from app.surface.base import SurfaceUnavailable
from app.surface.dispatch import ActionValidator, DispatchRejected, ResolvedAction
from app.surface.extract import EXTRACT_JS, MAX_NODES, parse_elements, parse_meta

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
        try:
            resolved = self._validator().validate(call)
        except DispatchRejected as rejection:
            # A refusal is information, not a crash: the agent sees a typed failure and can
            # re-observe or choose differently.
            logger.info("dispatch rejected %s: %s", call.name, rejection.reason)
            return rejection.to_result()

        timeout = TIMEOUTS.get(call.name, DEFAULT_TIMEOUT)
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

    def _validator(self) -> ActionValidator:
        return ActionValidator(
            vault=self._vault,
            geometry=self._geometry,
            bound_verbs=self._bound_verbs,
            approved=self._approved,
            # So the approval check can ask what an index points at. Same turn as the
            # geometry it is validated against — a stale one would gate the wrong element.
            observation=self._last_observation,
        )

    async def preview(self, call: ActionCall) -> str:
        """What this action will actually do, with tokens resolved.

        Read from the LIVE compose fields rather than reconstructed from what the agent
        thinks it typed. Those are two different things whenever a field rejected input,
        autocompleted, or was edited by a take-over — and the whole value of the card is
        that the human sees what is really there.
        """
        if call.name not in ("Send", "SendInvite"):
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

    async def _do_type(self, action: ResolvedAction) -> ActionResult:
        text = self._text_for(action)
        if action.point is not None:
            x, y = action.point
            await self._page.mouse.click(x, y)
        await self._page.keyboard.type(text, delay=12)
        # NEVER log `text` — a recipient resolved from a token is raw PII by this point.
        return ActionResult(success=True, reason=f"typed {len(text)} characters")

    async def _do_clear(self, action: ResolvedAction) -> ActionResult:
        if action.point is not None:
            x, y = action.point
            await self._page.mouse.click(x, y)
        await self._page.keyboard.press("Control+A")
        await self._page.keyboard.press("Delete")
        return ActionResult(success=True, reason="cleared the field")

    async def _do_presskey(self, action: ResolvedAction) -> ActionResult:
        key = str(action.call.args.get("key") or "Enter")
        await self._page.keyboard.press(key)
        return ActionResult(success=True, reason=f"pressed {key}")

    async def _do_scroll(self, action: ResolvedAction) -> ActionResult:
        direction = str(action.call.args.get("direction") or "down")
        amount = float(action.call.args.get("amount") or 1)
        height = self._page.viewport_size["height"] if self._page.viewport_size else 800
        delta = height * amount * (1 if direction == "down" else -1)
        await self._page.mouse.wheel(0, delta)
        return ActionResult(success=True, reason=f"scrolled {direction}")

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

    def _text_for(self, action: ResolvedAction) -> str:
        """The literal text to type, with tokens swapped for real values.

        This is the ONLY moment a real address exists outside the vault, and it exists for
        exactly as long as it takes to reach the keyboard.
        """
        text = str(action.call.args.get("text") or "")
        recipient = str(action.call.args.get("recipient") or "")
        if recipient and action.resolved_args:
            for token, real in action.resolved_args.items():
                recipient = recipient.replace(token, real)
            return recipient
        return text

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
    page = await context.new_page()

    surface = PlaywrightEmailSurface(page, **surface_kwargs)
    if start_url:
        await page.goto(start_url, wait_until="domcontentloaded")
    # After navigating, never before: `navigator.userAgentData` is undefined on about:blank,
    # so checking too early silently reports nothing and the warning never fires.
    await _warn_if_google_will_reject(page)

    async def close() -> None:
        await surface.stop_screencast()
        # Our tab only. `browser.close()` on a CDP connection disconnects rather than
        # quitting their Chrome, but the tab is ours to clean up.
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
