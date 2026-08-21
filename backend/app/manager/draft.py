"""The `Draft` model — the words of an email, before a browser is involved.

**Its own module because of who needs to import it.** `AgentState` must declare the field as
`Draft | None` and not `object`: state is revalidated from the checkpoint on every resume,
and an untyped field comes back as a plain `dict`. The failure is invisible until the first
approval interrupt — the one path that always round-trips — where the worker then reads
`draft.subject` off a dict and dies.

So state imports this. The writer node imports state. Putting `Draft` in the writer would
close that loop, which is why it lives here, importing nothing from either.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

#: Defensive ceilings, not style rules — the style rule lives in `prompts/writer.txt`. These
#: exist so a runaway generation cannot crowd the observation out of the worker's context.
MAX_BODY_CHARS = 4000
MAX_SUBJECT_CHARS = 200


class Draft(BaseModel):
    """Finished words, ready to type.

    Deliberately one object rather than three loose slots: a draft is reviewed and revised as
    a unit, and splitting it across slots invites half-applied edits — a new body sent under
    the old subject.
    """

    subject: str = Field(default="", max_length=MAX_SUBJECT_CHARS)
    body: str = Field(max_length=MAX_BODY_CHARS)
    tone: str = "professional"
