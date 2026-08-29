"""What KIND of correction a human typed at the approval card.

**The gap this closes.** Every instruction was fed to the reviser, whose entire prompt is
"change only what was asked and return everything else byte for byte". That is right for
"add regards" and wrong for three other things people actually type:

  - "scrap this, write about the Q3 numbers instead" — a rewrite, fought by a prompt built
    to preserve the existing words. What comes back is a hedged half-edit of an email they
    already rejected.
  - "why did you phrase it like that?" — a question, acted on as if it were a command.
  - "send it to P5 instead" — nothing to do with the words at all, but the reviser is asked
    to revise anyway, finds nothing, and reports success.

So the instruction gets classified before anything acts on it. One small call, and only when
a human has actually typed something — never in the loop, never on the common path where
they retype the draft themselves.

**It cannot change the recipient, by construction.** `kind` says what to do with the WORDS.
Who the mail goes to stays with the deterministic check in the gate, because a model that
can retarget an email on a judgement call is a model that can send your message to the wrong
person, and that is the one mistake here with no undo.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.llm.base import LLMClient, Message
from app.prompts import load_prompt

logger = logging.getLogger(__name__)

_SYSTEM = load_prompt("edit_scope")

#: Defensive ceiling on the brief. A runaway generation here would be copied straight into
#: the writer's prompt.
MAX_BRIEF_CHARS = 400

Kind = Literal["adjust", "rewrite", "question", "none"]
_KINDS: frozenset[str] = frozenset({"adjust", "rewrite", "question", "none"})


class EditScope(BaseModel):
    """What a human's instruction asks for, in terms the gate can route on."""

    model_config = ConfigDict(frozen=True)

    kind: Kind = "adjust"
    #: For `rewrite`: what the new message should be about. Empty otherwise.
    brief: str = Field(default="", max_length=MAX_BRIEF_CHARS)


#: `None` means "I could not tell" — a provider outage or an unreadable reply — and is
#: deliberately different from any `kind`. The CALLER decides what an unknown instruction
#: falls back to, because the fallback depends on what else it knows about the edit, and a
#: policy split between two modules is a policy nobody can check.
Classifier = Callable[[str], Awaitable[EditScope | None]]


def _parse_scope(text: str) -> EditScope | None:
    """Read a scope out of the model's reply, or `None` if there is not one in there."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    kind = str(data.get("kind") or "").strip().lower()
    if kind not in _KINDS:
        # A kind we do not have is not a reason to guess. Anything unrecognised is treated
        # as the safe default by the caller.
        return None

    brief = str(data.get("brief") or "").strip()[:MAX_BRIEF_CHARS]
    return EditScope(kind=kind, brief=brief if kind == "rewrite" else "")


def build_edit_classifier(llm: LLMClient) -> Classifier:
    """Classify one human instruction. Never raises; returns `None` when it cannot tell."""

    async def classify(instruction: str) -> EditScope | None:
        text = (instruction or "").strip()
        if not text:
            # Nothing to classify, and nothing to spend a call on.
            return EditScope(kind="none")

        try:
            result = await llm.complete(
                role="classifier",
                messages=[
                    Message(role="system", content=_SYSTEM, cacheable=True),
                    Message(role="user", content=text),
                ],
            )
        except Exception:
            logger.warning("edit-scope classification failed; the caller will fall back")
            return None

        scope = _parse_scope(result.text or result.reasoning)
        if scope is None:
            logger.info("edit-scope reply was unreadable; the caller will fall back")
            return None

        if scope.kind == "rewrite" and not scope.brief:
            # A rewrite with no brief would give the writer nothing to write from. The
            # instruction itself is the brief in that case — it is what the human said.
            scope = EditScope(kind="rewrite", brief=text[:MAX_BRIEF_CHARS])

        logger.info("instruction classified as %s", scope.kind)
        return scope

    return classify
