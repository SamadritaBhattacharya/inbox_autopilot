"""Tool specs — the model's entire vocabulary, as schemas.

Every action is a **schema-validated tool call**, never free text the backend parses. That
is what makes an action observable and refusable: a malformed call is rejected by the
schema, and a call outside a worker's bound set is rejected at dispatch.

**Binding is per worker, and it is a security control.** `TRIAGE_TOOLS` contains no `Send`,
so a triage run cannot send mail even if an email body asks it to — the capability is
absent from the schema, not merely discouraged in a prompt. An injected instruction can
argue with a prompt; it cannot conjure a tool that was never bound.

Each docstring's first line becomes the description the model sees, so they are written for
the model, not for us.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

# ── navigation and perception ───────────────────────────────────────────────


class Scroll(BaseModel):
    """Scroll the list up or down by roughly one screen at a time."""

    direction: str = Field(description="'up' or 'down'")
    amount: int = Field(default=1, description="How many screens")


class ReadThread(BaseModel):
    """Open and read one email thread to see what it actually says."""

    index: int = Field(description="The [N] of the thread to open")


class Extract(BaseModel):
    """Answer a question about what is currently on screen, without acting."""

    query: str = Field(description="What you want to know")


class WaitFor(BaseModel):
    """Pause before looking again. The engine already waits after every action, so use this
    only for content that keeps loading after the page settled."""

    seconds: float = Field(default=1.0)


# ── generic interaction ─────────────────────────────────────────────────────


class Click(BaseModel):
    """Click the element with the given [N]."""

    index: int


class Type(BaseModel):
    """Type text into the element with the given [N]."""

    index: int
    text: str = Field(default="", description="Literal text to type")
    recipient: str = Field(
        default="",
        description="A person TOKEN such as P3, for address fields. Never a real address.",
    )


class Clear(BaseModel):
    """Clear a field before typing something else into it."""

    index: int


class PressKey(BaseModel):
    """Press a single key, e.g. 'Enter' to submit."""

    key: str = Field(default="Enter")


# ── mail verbs (reversible) ─────────────────────────────────────────────────


class Archive(BaseModel):
    """Archive the thread at [N]. Reversible: it stays searchable in All Mail."""

    index: int
    reason: str = Field(default="", description="Why this one can be archived")


class MarkRead(BaseModel):
    """Mark the thread at [N] as read."""

    index: int


class Label(BaseModel):
    """Apply a label to the thread at [N]."""

    index: int
    label: str


class Snooze(BaseModel):
    """Hide the thread at [N] until a given time."""

    index: int
    until: str = Field(description="When it should return, e.g. 'tomorrow 9am'")


class DraftReply(BaseModel):
    """Write a reply and leave it as a DRAFT. Never sends."""

    index: int
    body: str


# ── mail verbs (irreversible — gated) ───────────────────────────────────────


class Send(BaseModel):
    """Send the composed email. IRREVERSIBLE: pauses for human approval first."""

    index: int


class DeleteForever(BaseModel):
    """Permanently delete. IRREVERSIBLE: pauses for human approval first."""

    index: int


# ── memory and control ──────────────────────────────────────────────────────


class Remember(BaseModel):
    """Save a short note for later steps in this run."""

    key: str
    value: str


class Recall(BaseModel):
    """Return everything currently in working memory."""


class SetPlan(BaseModel):
    """Replace the step-by-step plan shown to the human."""

    steps: list[str]


class AskUser(BaseModel):
    """Ask the human something you cannot determine yourself. The run PAUSES until they
    reply. Use sparingly — only when genuinely blocked."""

    question: str
    context: str = Field(default="")


class Complete(BaseModel):
    """Finish the task. Put your findings in `reason` — a well-explained partial result is
    far better than running out of steps silently."""

    success: bool
    reason: str


# ── per-worker bindings ─────────────────────────────────────────────────────

CONTROL_TOOLS: tuple[type[BaseModel], ...] = (Remember, Recall, SetPlan, AskUser, Complete)
PERCEPTION_TOOLS: tuple[type[BaseModel], ...] = (Scroll, ReadThread, Extract, WaitFor)

#: Triage: read the backlog and tidy it. **No Send, no DeleteForever** — an injected
#: "forward this to…" has no tool to reach for.
TRIAGE_TOOLS: tuple[type[BaseModel], ...] = (
    *PERCEPTION_TOOLS,
    Click,
    Archive,
    MarkRead,
    Label,
    Snooze,
    *CONTROL_TOOLS,
)

#: Compose: writing and sending, with Send gated at dispatch.
COMPOSE_TOOLS: tuple[type[BaseModel], ...] = (
    *PERCEPTION_TOOLS,
    Click,
    Type,
    Clear,
    PressKey,
    DraftReply,
    Send,
    *CONTROL_TOOLS,
)

#: Query: read, summarize, search, count, answer. **Contains no mutating verb at all.**
#:
#: This is the capability half of the read-only guarantee. A user asking "what did Priya say
#: about the demo?" gets an agent that reads a hostile inbox with no ability to archive,
#: label, send, or delete — so an injected instruction has nothing to reach for. The
#: dispatcher would refuse a mutating verb anyway; binding it away means the model is never
#: even tempted, and no prompt-level negotiation is possible.
QUERY_TOOLS: tuple[type[BaseModel], ...] = (*PERCEPTION_TOOLS, Click, *CONTROL_TOOLS)

TOOLSETS: dict[str, tuple[type[BaseModel], ...]] = {
    "query": QUERY_TOOLS,
    "triage": TRIAGE_TOOLS,
    "compose": COMPOSE_TOOLS,
}

#: Verbs handled inside the graph rather than by the surface.
INTERNAL_VERBS = frozenset({"Remember", "Recall", "SetPlan", "AskUser", "Complete", "Extract"})


def verb_names(tools: tuple[type[BaseModel], ...]) -> frozenset[str]:
    return frozenset(tool.__name__ for tool in tools)
