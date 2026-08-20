"""The `EmailSurface` port — everything the graph knows about the mailbox.

Two methods. That narrowness is the point: `observe()` and `act()` are the entire
vocabulary, so the same graph drives a server-side Chromium in CI and a user's own Chrome
in production with **one line changed in the composition root**. If swapping the
implementation ever needs more than that, dependency inversion has quietly stopped holding
and the architecture claims in this repo are decoration.

Note what is deliberately *absent*: no `navigate()`, no `screenshot()`, no `tabs()`. Those
are capabilities of one implementation, not of the concept. A consumer that needs them
wants a different port, not a wider one.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from inbox_contracts import ActionCall, ActionResult, Observation


class SurfaceUnavailable(RuntimeError):
    """The mailbox cannot be reached at all.

    Distinct from an action that failed: a failed action is information the agent can
    reason about and retry, whereas an unreachable surface ends the run with
    `ErrorCode.SURFACE_UNAVAILABLE`. Collapsing the two makes a dead browser look like a
    misclick, and the agent will happily retry forever.
    """


@runtime_checkable
class EmailSurface(Protocol):
    async def observe(self) -> Observation:
        """A fresh, tokenized, numbered view of the mailbox.

        Rebuilt from scratch every call. Indices are valid only for the observation that
        produced them, and are never reused across turns.
        """
        ...

    async def act(self, call: ActionCall) -> ActionResult:
        """Perform one action, targeted by index and token.

        Resolution of index → geometry and token → real value happens HERE, on this side of
        the wire, and only at this moment.
        """
        ...
