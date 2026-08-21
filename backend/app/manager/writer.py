"""The writer — the node that decides what the email actually says.

**Why a separate node rather than letting the worker improvise.** The compose worker's job
is operating a screen: click the right box, type into it, notice when Gmail moves something.
Asking it to also invent prose mid-loop means the wording is produced one `Type` call at a
time, by a model whose context is dominated by a list of 140 DOM elements and whose prompt
is about indices and tokens. That is the worst possible place to write anything.

Drafting once, up front, from the intent alone gives the writing its own prompt, its own
short context, and — because it happens before the browser is touched — something the human
can see and object to before a compose window even opens.

**No-op for anything that is not writing.** The node sits on the main path rather than
behind a conditional edge: one unconditional edge that sometimes returns `{}` is easier to
follow, and impossible to route around by mistake, than a branch that has to be kept in sync
with the action table.
"""
from __future__ import annotations

import json
import logging
import re

from app.agent.state import AgentState
from app.events.emitter import EventEmitter
from app.llm.base import LLMClient, Message
from app.manager.draft import MAX_BODY_CHARS, MAX_SUBJECT_CHARS, Draft
from app.manager.intent import Action
from app.prompts import load_prompt
from app.telemetry.records import StepRecord

logger = logging.getLogger(__name__)

_WRITER_SYSTEM = load_prompt("writer")

#: Actions whose output is prose. `FORWARD` is included: a forward with a covering note is
#: the normal case, and an empty one is a worse default than a short one.
WRITING_ACTIONS = frozenset({Action.SEND_EMAIL, Action.REPLY, Action.FORWARD})



def _parse_draft(text: str) -> Draft | None:
    """Pull a draft out of the model's reply.

    Models fence JSON, prefix it with "Here you go:", or both. Returning `None` rather than
    raising matters: a writer that fails should cost the run its head start, not the run.
    """
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    body = str(data.get("body") or "").strip()
    if not body:
        return None  # a subject with no body is not a draft
    # Clip here rather than let validation reject: an over-long draft is a style problem,
    # and failing the run over one would be worse than the run's first paragraph being fine.
    return Draft(
        subject=str(data.get("subject") or "").strip()[:MAX_SUBJECT_CHARS],
        body=body[:MAX_BODY_CHARS],
        tone=str(data.get("tone") or "professional").strip() or "professional",
    )


def brief_for(state: AgentState) -> str:
    """What the writer is told. The intent slots, not the raw task — they are what intake
    and the context gate already agreed the request means."""
    lines = [f"Request: {state.task}"]
    intent = state.intent
    if intent is not None:
        filled = {n: v for n, v in intent.slots.items() if str(v).strip()}
        if filled:
            lines.append("")
            lines.extend(f"{name}: {value}" for name, value in filled.items())
    return "\n".join(lines)


def build_writer_node(llm: LLMClient, emitter: EventEmitter | None = None):
    """Draft the message before the browser is opened. No-op for non-writing actions."""

    async def writer(state: AgentState) -> dict:
        intent = state.intent
        if intent is None or intent.action not in WRITING_ACTIONS:
            return {}

        result = await llm.complete(
            role="executor",
            messages=[
                Message(role="system", content=_WRITER_SYSTEM, cacheable=True),
                Message(role="user", content=brief_for(state)),
            ],
        )
        draft = _parse_draft(result.text or result.reasoning)

        if draft is None:
            # The worker can still write inline; it is just worse at it. Losing the draft is
            # a downgrade, not a failure, and failing the run here would turn a bad sentence
            # into a dead run.
            logger.warning("writer produced no usable draft; worker will improvise")
        else:
            logger.info("drafted %r (tone=%s)", draft.subject, draft.tone)
            if emitter is not None:
                # Before the browser opens, so a wrong tone is caught at second zero rather
                # than at the approval card with a compose window already full.
                await emitter.draft(draft.subject, draft.body, draft.tone)

        return {
            "draft": draft,
            "history": [
                StepRecord(
                    step=state.step,
                    node="writer",
                    provider=result.provider,
                    role="executor",
                    usage=result.usage,
                    latency_ms=result.latency_ms,
                )
            ],
        }

    return writer
