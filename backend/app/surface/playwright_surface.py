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
import logging
import os
import sys
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
        self._previous_identities: set[str] = set()
        self._bound_verbs = frozenset(bound_verbs or TIMEOUTS.keys())
        self._approved: set[str] = set()

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
        )

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


async def launch_surface(
    *,
    headless: bool = True,
    start_url: str | None = None,
    **surface_kwargs: Any,
) -> tuple[PlaywrightEmailSurface, Any]:
    """Convenience for scripts and integration tests. Returns `(surface, closer)`."""
    from playwright.async_api import async_playwright

    executable = resolve_chromium(headless=headless)

    manager = await async_playwright().start()
    try:
        browser = await manager.chromium.launch(headless=headless, executable_path=executable)
    except Exception as exc:
        await manager.stop()
        raise SurfaceUnavailable(
            f"could not launch Chromium ({exc}). Run `playwright install chromium`, or set "
            "PLAYWRIGHT_CHROMIUM_PATH to an existing build."
        ) from exc

    page = await browser.new_page(viewport={"width": 1280, "height": 800})
    if start_url:
        await page.goto(start_url, wait_until="domcontentloaded")

    async def close() -> None:
        await browser.close()
        await manager.stop()

    return PlaywrightEmailSurface(page, **surface_kwargs), close
