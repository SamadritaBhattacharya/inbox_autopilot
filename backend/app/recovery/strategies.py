"""Remediation strategies — the curated skills the self-heal layer may reach for.

**This is the safe reading of "load skills".** A fixed, versioned, unit-tested set of moves.
The agent chooses among them; it does not write new ones, and it certainly does not edit its
own source while holding an open mailbox and a context full of attacker-controlled email.
See [ADR-009](../../../docs/ADR.md) for what the deferred version looks like and why it is
a separate, sandboxed, human-reviewed process.

Each strategy is one class with one job (SRP) and adding one is adding a class plus a
registry line (OCP). None of them can approve anything — a remedy that could send mail on
the user's behalf would make the approval gate decorative.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.llm.base import Message
from app.recovery.causes import Cause


@dataclass(frozen=True)
class Option:
    """One ranked choice, as the human sees it."""

    n: int
    label: str
    detail: str
    recommended: bool = False
    freeform: bool = False


@runtime_checkable
class RemediationStrategy(Protocol):
    name: str
    label: str
    detail: str

    def applies_to(self, cause: Cause) -> float:
        """0.0–1.0 confidence that this is the right move for `cause`."""
        ...

    def guidance(self) -> str:
        """What the agent is told to do next if the human picks this."""
        ...


@dataclass(frozen=True)
class _Strategy:
    """Shared shape. A strategy is a scored `Cause -> guidance` mapping and nothing more.

    Guidance is phrased as an *instruction to try*, not a promise it will work — the loop
    still observes, still reasons, and can still conclude the move was wrong. A strategy
    that bypassed the loop would be a scripted macro wearing an agent's clothes.
    """

    name: str
    label: str
    detail: str
    _fits: dict[Cause, float]
    _guidance: str

    def applies_to(self, cause: Cause) -> float:
        return self._fits.get(cause, 0.0)

    def guidance(self) -> str:
        return self._guidance


DISMISS_OVERLAY = _Strategy(
    name="dismiss_overlay",
    label="Close the dialog and try again",
    detail="Find the dialog that is in the way, close it, then retry the original action.",
    _fits={Cause.OVERLAY_BLOCKING: 0.95, Cause.TARGET_MOVED: 0.25},
    _guidance=(
        "A dialog is blocking you. Find its close/cancel/dismiss control in the CURRENT "
        "element list and click that first, then retry what you were doing."
    ),
)

SCROLL_AND_RETRY = _Strategy(
    name="scroll_and_retry",
    label="Scroll to find it, then retry",
    detail="Scroll toward the content that is off-screen and look again.",
    _fits={Cause.OFF_SCREEN: 0.9, Cause.TARGET_MOVED: 0.5},
    _guidance=(
        "What you need is off-screen. Scroll in the direction the observation's hint names, "
        "re-read the list, and act on the fresh indices — never on remembered ones."
    ),
)

WAIT_AND_RETRY = _Strategy(
    name="wait_and_retry",
    label="Wait for the page, then retry",
    detail="Give the page time to finish loading before acting again.",
    _fits={Cause.SLOW_RENDER: 0.9, Cause.NAVIGATED_AWAY: 0.5},
    _guidance=(
        "The page was still settling. Call WaitFor(2), look again, and only then act."
    ),
)

RE_OBSERVE = _Strategy(
    name="re_observe",
    label="Take a fresh look and pick again",
    detail="Discard the stale view and choose a target from the current list.",
    _fits={Cause.STALE_VIEW: 0.95, Cause.NAVIGATED_AWAY: 0.7, Cause.TARGET_MOVED: 0.6},
    _guidance=(
        "Your last target no longer exists. Read the CURRENT element list and pick a target "
        "from it. Indices are rebuilt every turn — an index you remember means nothing now."
    ),
)

SUMMARISE_AND_STOP = _Strategy(
    name="summarise_and_stop",
    label="Report what you found so far",
    detail="Stop exploring and return the partial result.",
    _fits={Cause.OSCILLATION: 0.9, Cause.BUDGET_SPENT: 0.95, Cause.OFF_SCREEN: 0.3},
    _guidance=(
        "You are not making progress by looking further. Use what you have already seen and "
        "call Complete(success=true) with those findings. A well-explained partial answer is "
        "far more useful than another lap."
    ),
)

NOTE_AND_CONTINUE = _Strategy(
    name="note_and_continue",
    label="Note what you've seen and move on",
    detail="Record findings to memory, then continue with the rest of the task.",
    _fits={Cause.OSCILLATION: 0.8, Cause.OFF_SCREEN: 0.6},
    _guidance=(
        "Use Remember to record what you have already established, then move to the next "
        "part of the task. You do not need everything in view at once."
    ),
)

SWITCH_PROVIDER = _Strategy(
    name="switch_provider",
    label="Wait for the model provider and retry",
    detail="The provider is rate-limiting; pause before trying again.",
    _fits={Cause.PROVIDER_EXHAUSTED: 0.95},
    _guidance="The model provider refused. Wait, then retry the same step.",
)

RESTART_SURFACE = _Strategy(
    name="restart_surface",
    label="Reconnect to the mailbox",
    detail="The browser is gone; a new session is needed.",
    _fits={Cause.SURFACE_GONE: 0.95},
    _guidance="The mailbox connection is gone. This run cannot continue; start a fresh one.",
)

SIGN_IN = _Strategy(
    name="sign_in",
    label="Sign into Gmail in that browser, then retry",
    detail="The browser is on Google's sign-in page, so there is no mailbox to read yet.",
    _fits={Cause.NOT_SIGNED_IN: 0.98, Cause.SURFACE_GONE: 0.2},
    # The agent must never attempt this itself: Google refuses its sign-in flow in a browser
    # under automation, and a password relayed through the model would land in the
    # trajectory, the logs, and the screencast frames. The remedy is the human's to perform.
    _guidance=(
        "Do NOT try to sign in yourself — it does not work, and a password must never pass "
        "through you. Call Complete(success=false) and say the browser needs signing into."
    ),
)

ASK_USER = _Strategy(
    name="ask_user",
    label="Ask me what to do",
    detail="Stop and put the question to you.",
    # Applies weakly to EVERYTHING on purpose: it is the floor, so there is always a third
    # real option even for a cause nothing else fits. An options card with two entries and a
    # blank is worse than one that admits it needs help.
    _fits=dict.fromkeys(Cause, 0.2) | {Cause.HUMAN_BLOCKED: 0.9, Cause.UNKNOWN: 0.6},
    _guidance=(
        "Use AskUser to ask the operator exactly what you should do differently, then follow "
        "their answer."
    ),
)

#: The registry, in a stable order so equal scores break deterministically. A run that
#: offers different options on identical evidence is one nobody can reason about.
ALL_STRATEGIES: tuple[_Strategy, ...] = (
    DISMISS_OVERLAY,
    RE_OBSERVE,
    SCROLL_AND_RETRY,
    WAIT_AND_RETRY,
    SUMMARISE_AND_STOP,
    NOTE_AND_CONTINUE,
    SWITCH_PROVIDER,
    RESTART_SURFACE,
    SIGN_IN,
    ASK_USER,
)

BY_NAME: dict[str, _Strategy] = {strategy.name: strategy for strategy in ALL_STRATEGIES}

#: Option 4 always exists. The user types what they want and it becomes loop guidance —
#: the escape hatch for every case the curated set does not cover, which is the honest
#: admission that a fixed registry cannot anticipate everything.
FREEFORM_OPTION = Option(
    n=4,
    label="Something else — tell me what to do",
    detail="Describe what you want instead and I'll follow it.",
    freeform=True,
)


def freeform_guidance(text: str) -> list[Message]:
    return [
        Message(
            role="user",
            content=f"Do this instead: {text}\nFollow it, then continue the original task.",
        )
    ]
