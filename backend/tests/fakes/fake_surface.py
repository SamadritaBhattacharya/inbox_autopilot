"""A scripted `EmailSurface`. No browser, no page, no Chromium.

TEST DOUBLE ONLY — the composition root builds it solely when a test injects it. It
satisfies exactly the `EmailSurface` port, which is what lets the entire graph, every
worker, and the whole failure layer be tested in milliseconds.

The recording half matters as much as the scripting half: assertions are made against
`surface.calls` — what the agent *tried to do* — rather than against side effects. That is
how a test proves "no send was dispatched" instead of proving "no email arrived", which is
a much weaker statement.
"""
from __future__ import annotations

from collections.abc import Sequence

from inbox_contracts import ActionCall, ActionResult, MailContext, Observation, Viewport

from app.surface.base import SurfaceUnavailable


def observation(
    *elements,
    context_id: str = "T1",
    title: str = "Inbox",
    view: str = "inbox",
    dropped: int = 0,
    hint: str | None = None,
    changed: str | None = None,
    compose_open: bool = False,
) -> Observation:
    """Build a tokenized observation the way the funnel would emit one."""
    return Observation(
        context_id=context_id,
        title=title,
        viewport=Viewport(width=1280, height=800),
        elements=list(elements),
        mail=MailContext(view=view, composeOpen=compose_open),
        changed=changed,
        droppedCount=dropped,
        hint=hint,
    )


class FakeEmailSurface:
    """Serves scripted observations; records every action it was asked to perform."""

    def __init__(
        self,
        observations: Sequence[Observation] | None = None,
        results: Sequence[ActionResult] | None = None,
        *,
        unavailable: bool = False,
        preview: str = "",
    ) -> None:
        self._observations = list(observations or [])
        self._results = list(results or [])
        self._unavailable = unavailable
        self._preview = preview

        #: Every action attempted, in order. The assertion surface for guardrail tests.
        self.calls: list[ActionCall] = []
        #: Calls a preview was requested for — proves the human was shown it FIRST.
        self.previewed: list[ActionCall] = []
        #: Fingerprints a human authorized. The fake refuses gated verbs without one,
        #: exactly like the real surface — a rubber-stamping double would make every
        #: approval test pass while proving nothing.
        self.approved: set[str] = set()
        self.observe_count = 0

    # ── the port ────────────────────────────────────────────────────────────

    async def observe(self) -> Observation:
        if self._unavailable:
            raise SurfaceUnavailable("fake surface is configured as unreachable")
        self.observe_count += 1
        if not self._observations:
            # Re-observing a settled page is normal, so the LAST observation repeats rather
            # than the fake raising. A test that wants a change scripts a change.
            return observation()
        if len(self._observations) == 1:
            return self._observations[0]
        return self._observations.pop(0)

    async def act(self, call: ActionCall) -> ActionResult:
        if self._unavailable:
            raise SurfaceUnavailable("fake surface is configured as unreachable")
        from app.surface.dispatch import GATED_VERBS, approval_fingerprint

        if call.name in GATED_VERBS and approval_fingerprint(call) not in self.approved:
            return ActionResult(
                success=False,
                reason=f"{call.name} has no matching approval",
                error_code="APPROVAL_REQUIRED",
            )

        self.calls.append(call)
        if self._results:
            return self._results.pop(0)
        return ActionResult(success=True, reason=f"{call.name} ok")

    def approve(self, fingerprint: str) -> None:
        """Record an authorization, and remember it was granted.

        The fake ENFORCES this like the real surface does — a double that rubber-stamps
        every send would make the approval tests pass while proving nothing.
        """
        self.approved.add(fingerprint)

    async def preview(self, call: ActionCall) -> str:
        """A scripted resolved draft. Records that a preview was asked for.

        Tests assert on `previewed` to prove the human was shown something *before* the
        action ran — "it asked first" is the claim, and the order is the whole of it.
        """
        self.previewed.append(call)
        return self._preview or f"{call.name} with {call.args}"

    # ── assertions ──────────────────────────────────────────────────────────

    @property
    def verbs(self) -> list[str]:
        return [call.name for call in self.calls]

    def dispatched(self, verb: str) -> bool:
        return verb in self.verbs

    def never_dispatched(self, *verbs: str) -> bool:
        """The shape most guardrail tests want: prove an action was never ATTEMPTED."""
        return all(verb not in self.verbs for verb in verbs)
