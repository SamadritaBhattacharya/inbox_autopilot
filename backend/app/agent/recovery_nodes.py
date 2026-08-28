"""POST phase: verify → diagnose → options.

The difference between an agent that fails and one that recovers.

**`verify` exists because "the agent believes it succeeded" and "it succeeded" are different
claims.** A model that called `Complete(success=True)` is reporting its own opinion; the
contract check asks the page. Cheapest first: a deterministic check covers most cases and
costs nothing, and only genuinely ambiguous outcomes are worth a model call.

**`diagnose` turns a code into a sentence.** `STUCK` cannot be acted on; "a dialog is
covering the button" can.

**`options` asks.** Three ranked remedies plus a free-form fourth — and a hard stop, because
an agent that can always offer another remedy can loop on remediation forever.
"""
from __future__ import annotations

import logging

from langgraph.types import interrupt

from app.agent.state import AgentState
from app.events.emitter import EventEmitter
from app.llm.base import Message
from app.recovery.causes import Cause, Diagnosis, classify
from app.recovery.registry import CuratedSkillRegistry
from app.recovery.strategies import freeform_guidance
from app.telemetry.records import ErrorCode, StepRecord
from app.workers.irreversible import is_irreversible

logger = logging.getLogger(__name__)


def build_verify_node(emitter: EventEmitter):
    """Did the run actually achieve what it set out to?

    Deterministic only, and deliberately so. A rubric check costs a model call at exactly
    the moment the provider is most likely to be the thing that broke, so v1 verifies what
    can be verified for free and does not pretend to more.
    """

    async def verify(state: AgentState) -> dict:
        # A typed failure is already a verified failure — nothing to re-check.
        if state.error_code is not None or state.status == "failed":
            return {"success": False}

        if state.success is False:
            # The agent said so itself. Believe it, and let diagnose explain why.
            return {}

        # An irreversible action the EXECUTOR confirmed is not re-litigated here.
        #
        # `_do_send` reports success only after watching the compose dialog close — evidence
        # from the browser itself, at the moment it happened. This node reads
        # `state.observation`, which is the view from BEFORE the action, because a run that
        # finished at `act` never re-observed. So it saw `view: compose`, concluded the mail
        # had not gone, and reported a failure for a message that was already sent.
        #
        # Stale evidence must not overturn direct evidence. When the surface confirmed it,
        # believe it.
        if (
            state.last_result is not None
            and state.last_result.success
            and is_irreversible(state.last_action, state.observation)
        ):
            return {"success": True, "status": "done"}

        intent = state.intent
        if intent is None:
            return {"success": bool(state.success)}

        # Contract check: the mailbox should show the effect the intent asked for.
        expected_view = {"send_email": "sent", "reply": "sent"}.get(intent.action.value)
        observation = state.observation
        if expected_view and observation is not None and observation.mail is not None:
            # Being ON the sent view is weak evidence, but its ABSENCE after a send is the
            # signal worth catching: a click that silently did nothing looks identical to a
            # successful send from inside the loop.
            reached = observation.mail.view in (expected_view, "inbox")
            if not reached:
                # Typed, like every other failure. Without a code this reached the human
                # as "Send failed with no code" — the one thing CLAUDE.md §11 says a
                # terminal state may never be, because an untyped exit cannot be counted,
                # diagnosed, or turned into a ranked remedy.
                await emitter.error(
                    "the message does not appear to have been sent",
                    ErrorCode.SEND_UNVERIFIED.value,
                )
                return {
                    "success": False,
                    "status": "failed",
                    "error_code": ErrorCode.SEND_UNVERIFIED,
                    "reason": "I could not confirm the message was actually sent.",
                }

        return {"success": True, "status": "done"}

    return verify


def build_diagnose_node(
    emitter: EventEmitter, registry: CuratedSkillRegistry | None = None
):
    """Explain the failure in terms something can be done about."""
    registry = registry or CuratedSkillRegistry()

    async def diagnose(state: AgentState) -> dict:
        diagnosis: Diagnosis = classify(
            error_code=state.error_code,
            last_action=state.last_action,
            last_result=state.last_result,
            observation=state.observation,
            stuck_count=state.stuck_count,
        )

        await emitter.diagnosis(
            diagnosis.cause.value, diagnosis.plain, diagnosis.evidence
        )
        logger.info("diagnosed %s: %s", diagnosis.cause, diagnosis.evidence)

        # Terminate rather than remediate forever. A third occurrence of one cause means the
        # remedies are not working, and asking again is nagging rather than helping.
        if registry.exhausted(state.remedied_causes, diagnosis.cause):
            return {
                "diagnosis": diagnosis,
                "finished": True,
                "success": False,
                "status": "failed",
                "error_code": state.error_code or ErrorCode.STUCK,
                "reason": f"{diagnosis.plain} I tried to work around it and could not.",
            }

        if diagnosis.cause is Cause.SURFACE_GONE:
            # Nothing to offer: the mailbox is gone, so every remedy needs a new session.
            return {
                "diagnosis": diagnosis,
                "finished": True,
                "success": False,
                "status": "failed",
                "error_code": ErrorCode.SURFACE_UNAVAILABLE,
                "reason": diagnosis.plain,
            }

        # Clear the terminal flag. The run arrived here already marked finished — that is
        # how it got diagnosed at all — and leaving it set would send the very next router
        # straight to `finalize`, skipping the options it was just diagnosed FOR. The error
        # code is deliberately kept: it is still the evidence, and only a chosen remedy
        # earns the right to clear it.
        return {
            "diagnosis": diagnosis,
            "finished": False,
            "history": [
                StepRecord(step=state.step, node="diagnose", error_code=state.error_code)
            ],
        }

    return diagnose


def build_options_node(
    emitter: EventEmitter, registry: CuratedSkillRegistry | None = None
):
    """Four ranked options, and a human choosing between them."""
    registry = registry or CuratedSkillRegistry()

    async def options(state: AgentState) -> dict:
        diagnosis: Diagnosis | None = state.diagnosis  # type: ignore[assignment]
        if diagnosis is None:
            return {"finished": True, "success": False, "reason": "nothing to recover from"}

        # Exclude what has already been tried, so a second failure of the same cause offers
        # genuinely different moves rather than the one that just failed.
        tried = set(state.tried_strategies)
        ranked = registry.strategies_for(diagnosis.cause, exclude=tried)
        choices = registry.options_for(diagnosis.cause, exclude=tried)

        # Derived from the cause and how many remedies have been tried, so a resume
        # re-emits the SAME id rather than presenting one decision as several.
        request_id = f"op-{diagnosis.cause.value}-{len(state.tried_strategies)}"
        await emitter.options(
            request_id,
            [
                {
                    "n": choice.n,
                    "label": choice.label,
                    "detail": choice.detail,
                    "recommended": choice.recommended,
                    "freeform": choice.freeform,
                }
                for choice in choices
            ],
        )

        raw = interrupt(
            {
                "options": True,
                "requestId": request_id,
                "cause": diagnosis.cause.value,
                "plain": diagnosis.plain,
                "evidence": diagnosis.evidence,
                "choices": [
                    {
                        "n": c.n,
                        "label": c.label,
                        "detail": c.detail,
                        "recommended": c.recommended,
                        "freeform": c.freeform,
                    }
                    for c in choices
                ],
            }
        )

        chosen, text = _parse_choice(raw)

        # Free-form, or a number past the ranked slots: the human's own words become the
        # guidance. This is the escape hatch for everything a fixed registry cannot cover.
        if text or chosen > len(ranked):
            if not text:
                return _give_up(diagnosis, "no instruction was given")
            return {
                "messages": freeform_guidance(text),
                "remedied_causes": [diagnosis.cause.value],
                "status": "running",
                "finished": False,
                "success": None,
                "error_code": None,
                "stuck_count": 0,
                "recent_actions": [],
            }

        strategy = ranked[chosen - 1]
        logger.info("remedy chosen: %s for %s", strategy.name, diagnosis.cause)

        return {
            "messages": [Message(role="user", content=strategy.guidance())],
            "remedied_causes": [diagnosis.cause.value],
            "tried_strategies": [strategy.name],
            "status": "running",
            # Clear the terminal state AND the guard counters. Resuming with a stuck count
            # of 8 would kill the run on its first turn back, which would make every remedy
            # look like it failed instantly.
            "finished": False,
            "success": None,
            "error_code": None,
            "stuck_count": 0,
            "recent_actions": [],
        }

    return options


def _give_up(diagnosis: Diagnosis, why: str) -> dict:
    return {
        "finished": True,
        "success": False,
        "status": "failed",
        "reason": f"{diagnosis.plain} ({why})",
    }


def _parse_choice(raw: object) -> tuple[int, str]:
    """`(option_number, free_text)` from whatever came back.

    Unparseable input becomes option 1 — the *recommended* remedy — rather than a failure.
    Unlike an approval, where ambiguity must fail closed, every option here is a safe
    read-only move, so the cautious default is to try the best one rather than abandon a
    run over a malformed frame.
    """
    if isinstance(raw, dict):
        text = str(raw.get("text") or "").strip()
        try:
            return int(raw.get("option", 1)), text
        except (TypeError, ValueError):
            return 1, text
    if isinstance(raw, int):
        return raw, ""
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.isdigit():
            return int(stripped), ""
        return 4, stripped  # bare text is a free-form instruction
    return 1, ""
