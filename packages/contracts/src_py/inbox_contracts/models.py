"""Wire contracts — the ONLY types the backend and the executor share.

Authored once here as Pydantic v2; the Zod/TS side is generated from the emitted
JSON Schema. Never redefine any of these anywhere else.

Two things are load-bearing and easy to lose in a refactor:

1. **`extra="forbid"` is a security control, not tidiness.** It is what makes
   "no coordinates, no raw DOM, no URL" a *validation error* rather than a code-review
   convention — and it propagates into the generated Zod, so the executor and the
   cockpit inherit the same guarantee for free.

2. **`args` and `payload` are deliberately open.** Per-verb argument validation belongs
   to the dispatcher, which knows the verb; the schema does not and must not guess.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .version import PROTOCOL_VERSION

# camelCase on the wire, snake_case in Python, and nothing unexpected in either direction.
_WIRE = ConfigDict(populate_by_name=True, extra="forbid")

# Open-payload models: same casing rules, but callers may put arbitrary keys inside the
# designated dict field. `extra` still applies to the TOP level.
_WIRE_OPEN = ConfigDict(populate_by_name=True, extra="forbid")


class Viewport(BaseModel):
    """Where the user is looking. Scroll offsets let the agent reason about what is
    off-screen without ever receiving element coordinates."""

    model_config = _WIRE

    width: int
    height: int
    scroll_x: int = Field(default=0, alias="scrollX")
    scroll_y: int = Field(default=0, alias="scrollY")


class Element(BaseModel):
    """One interactable, addressed by its Set-of-Marks index.

    `name` and `value` are ALREADY TOKENIZED when this is constructed — the funnel's
    tokenizer stage runs before indexing, so nothing downstream ever holds raw PII.

    There is no geometry here, and `extra="forbid"` means none can be added by accident:
    the `index -> {x, y, backendNodeId}` map stays in the executor.
    """

    model_config = _WIRE

    index: int
    role: str
    name: str = ""
    value: str | None = None
    is_new: bool = Field(default=False, alias="isNew")


MailView = Literal[
    "inbox", "thread", "compose", "search", "sent", "drafts", "calendar", "signed_out"
]


class MailContext(BaseModel):
    """Email semantics layered over the generic page observation.

    This is what lets a worker reason about *mail* ("am I in a thread? is compose open?")
    instead of re-deriving it from element names every turn.
    """

    model_config = _WIRE

    view: MailView
    thread_token: str | None = Field(default=None, alias="threadToken")
    unread_count: int | None = Field(default=None, alias="unreadCount")
    compose_open: bool = Field(default=False, alias="composeOpen")

    #: Which compose fields already have content. Meaningless unless `compose_open`.
    #:
    #: Booleans, never the text: whether a field is filled is what the agent needs in order
    #: to decide what to do next, and the content itself is exactly what must not reach the
    #: model in the clear.
    #:
    #: These exist because guessing was costing whole runs. A committed recipient becomes a
    #: *chip* — a separate DOM node — so the To input reads as empty, and the agent typed the
    #: address a second time on top of the first. The same blindness sent it hunting for a
    #: subject field it had already filled.
    to_filled: bool = Field(default=False, alias="toFilled")
    subject_filled: bool = Field(default=False, alias="subjectFilled")
    body_filled: bool = Field(default=False, alias="bodyFilled")


class Observation(BaseModel):
    """What the model is allowed to see: a short, numbered, tokenized element list.

    Deliberately absent, each enforced by `extra="forbid"`:
      - coordinates  -> executor-side index map
      - raw DOM      -> never crosses the wire in any form
      - url          -> on an email surface the URL *is* an identifier; `context_id`
                        is an opaque stand-in
    """

    model_config = _WIRE

    protocol_version: str = Field(default=PROTOCOL_VERSION, alias="protocolVersion")
    context_id: str = Field(alias="contextId")
    title: str = ""
    viewport: Viewport
    elements: list[Element] = Field(default_factory=list)
    mail: MailContext | None = None
    screenshot_ref: str | None = Field(default=None, alias="screenshotRef")
    # Human-readable diff vs the previous turn ("compose panel opened"). The model gets a
    # diff AND a fresh list — never a blind full dump.
    changed: str | None = None
    # How many elements are reachable but not listed. Silent truncation makes an agent
    # confidently wrong, so this is reported even when it is inconvenient.
    dropped_count: int = Field(default=0, alias="droppedCount")
    # WHERE the unlisted content is, in words the model can act on:
    # "12 more items — 5 above, 7 below."
    #
    # The count alone turned out to be half a fix. An agent told only "12 more" scrolls one
    # way, sees the number stay the same, and scrolls the same way again — observed doing
    # exactly that, until the stuck guard killed the run. A direction converts a dead end
    # into a decision.
    hint: str | None = None


class ActionCall(BaseModel):
    """What the model is allowed to do. Targets by INDEX and TOKEN only.

    `args` is intentionally open: the dispatcher validates per verb (a literal email
    address in an argument is rejected there — that is the signature of an injected
    recipient). Encoding every verb's shape here would duplicate that logic in a place
    that cannot see the verb registry.
    """

    model_config = _WIRE_OPEN

    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ActionResult(BaseModel):
    model_config = _WIRE

    success: bool
    reason: str = ""
    error_code: str | None = Field(default=None, alias="errorCode")
    # Enough state to reverse a mutating verb (previous label set, previous folder).
    # Every mutating action logs this; without it "undo" is a promise we cannot keep.
    undo: dict[str, Any] | None = None


class Envelope(BaseModel):
    """The relay frame between the backend and the executor.

    `id` correlates a request with its response; unsolicited messages (frames,
    registration) omit it.
    """

    model_config = _WIRE_OPEN

    protocol_version: str = Field(default=PROTOCOL_VERSION, alias="protocolVersion")
    type: str
    id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


#: Every model that gets a generated JSON Schema + Zod counterpart.
WIRE_MODELS: list[type[BaseModel]] = [Observation, ActionCall, ActionResult, Envelope]
