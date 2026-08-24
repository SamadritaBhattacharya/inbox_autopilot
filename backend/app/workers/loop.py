"""The worker loop: observe -> reason -> act -> observe.

The reused engine, expressed as three thin nodes. Each is a closure over injected ports and
returns a state delta; none contains an LLM or browser call inline.

**Re-observe from scratch after every action.** Nothing tracks what changed. A dialog opens
and simply appears in the next list; occlusion hides what is behind it; a navigation
produces a new page. This is why the loop needs no special handling for popups, tabs, or
redirects — and why indices are never reused across turns.

The guards live in `agent/guards.py` as pure functions and are *applied* here, so what the
loop does about a stuck page is readable in one place while how "stuck" is measured is
testable on its own.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from inbox_contracts import ActionCall, ActionResult, Observation
from langgraph.types import interrupt
from pydantic import BaseModel

from app.agent.assessment import derive_outcome, outcome_note, split_assessment
from app.agent.compaction import compact
from app.agent.guards import (
    OSCILLATION_KILL_AT,
    REPEAT_KILL_AT,
    REPEAT_NUDGE_AT,
    STUCK_KILL_AT,
    STUCK_NUDGE_AT,
    action_signature,
    budget_reminder,
    clip_reasoning,
    is_oscillating,
    is_repetition_candidate,
    oscillation_nudge,
    page_signature,
    push_action,
    repetition_count,
    repetition_nudge,
    stuck_nudge,
)
from app.agent.state import AgentState
from app.events.emitter import EventEmitter
from app.feedback.models import Feedback, FeedbackKind
from app.feedback.store import FeedbackStore
from app.llm.base import LLMClient, Message
from app.prompts import load_prompt
from app.surface.base import EmailSurface, SurfaceUnavailable
from app.telemetry.records import ErrorCode, StepRecord
from app.workers.internal_verbs import handle_internal
from app.workers.rendering import observation_block, task_block
from app.workers.tools import INTERNAL_VERBS

logger = logging.getLogger(__name__)

#: Token budget for the conversation history. Conservative on purpose: the free-tier models
#: this runs on have small windows, and a run that fails at step 39 has wasted everything it
#: spent getting there.
DEFAULT_CONTEXT_BUDGET = 8_000

WORKER_SYSTEM = load_prompt("worker")


#: Two goes at signing in. One is unforgiving if they mistype; unbounded means a run that
#: never ends on a page nobody is going to fix.
MAX_SIGNIN_ASKS = 2


def build_observe_node(surface: EmailSurface, emitter: EventEmitter):
    """Fresh perception. Rebuilt every turn, never diffed in place."""

    async def look() -> Observation:
        await emitter.activity("looking", "reading the screen")
        observation = await surface.observe()
        # Where the browser is, for the human. Best-effort and cockpit-only: a surface with
        # no URL (an API-backed one) simply has nothing to report, and a failure to report
        # it must never cost the run a turn.
        if url := getattr(surface, "current_url", ""):
            await emitter.location(url, observation.title or "")
        return observation

    async def observe(state: AgentState) -> dict:
        # ── perceive, pausing for a sign-in if the browser needs one ──
        #
        # A login wall hands the browser back to the human rather than failing. The agent
        # must NOT attempt the sign-in itself, and that is not caution — it does not work:
        # Google refuses its flow in a browser running a debugging port. It is also the one
        # secret that must never enter this system, since a password relayed through the
        # model would land in the trajectory, the logs, and the screencast frames.
        #
        # **The loop is the point.** `interrupt()` raises the first time and RETURNS on
        # resume, so looping re-observes and verifies the human actually signed in rather
        # than trusting that they said so. An earlier version returned a delta here instead,
        # which left `observation` unset — and `reason` then ran against "No page loaded
        # yet", invented a screen it had never seen, and asked the user to "load the email
        # client". This node must never hand the loop a state with no observation.
        asks = 0
        while True:
            try:
                observation = await look()
            except SurfaceUnavailable as exc:
                await emitter.error(str(exc), ErrorCode.SURFACE_UNAVAILABLE.value)
                return {
                    "status": "failed",
                    "error_code": ErrorCode.SURFACE_UNAVAILABLE,
                    "finished": True,
                    "success": False,
                    "reason": f"the mailbox became unreachable: {exc}",
                }

            if not (observation.mail and observation.mail.view == "signed_out"):
                break

            if asks >= MAX_SIGNIN_ASKS:
                # Asked and still signed out. Terminating typed beats pausing forever on a
                # page the human has already declined to fix.
                reason = (
                    "That browser is still not signed into Gmail. Sign in there, then start "
                    "the run again."
                )
                await emitter.error(reason, ErrorCode.NOT_SIGNED_IN.value)
                return {
                    "observation": observation,
                    "status": "failed",
                    "error_code": ErrorCode.NOT_SIGNED_IN,
                    "finished": True,
                    "success": False,
                    "reason": reason,
                }

            interrupt(
                {
                    "signin": True,
                    "question": (
                        "That browser isn't signed into Gmail. Sign in on the right — it is "
                        "a real Chrome window and this is Google's own page — then continue. "
                        "Type your password there, never here."
                    ),
                    "missing": ["gmail_session"],
                    "task": state.task,
                }
            )
            asks += 1

        before = page_signature(state.observation)
        after = page_signature(observation)
        # Only count as stuck once an action has actually been attempted — the first two
        # observations of a static page are not evidence of anything.
        unchanged = bool(before) and before == after and state.last_action is not None

        await emitter.observation(
            context_id=observation.context_id,
            elements=len(observation.elements),
            dropped=observation.dropped_count,
            view=observation.mail.view if observation.mail else "unknown",
        )

        return {
            "observation": observation,
            "stuck_count": state.stuck_count + 1 if unchanged else 0,
        }

    return observe


ToolsFor = Callable[[AgentState], tuple[type[BaseModel], ...]]


def build_reason_node(
    llm: LLMClient,
    emitter: EventEmitter,
    *,
    tools: tuple[type[BaseModel], ...] | ToolsFor,
    max_steps: int,
    feedback: FeedbackStore | None = None,
    context_budget: int = DEFAULT_CONTEXT_BUDGET,
):
    """One model turn: history + observation + feedback + bound tools -> reasoning + action.

    Three feedback signals reach the model here, and they are deliberately different:

    - the **derived outcome** of the last action (measured — "it succeeded and nothing moved")
    - **human corrections** not yet shown (a person actively steering the run)
    - the model's own **assessment**, which it writes and we surface but never enforce
    """

    async def reason(state: AgentState) -> dict:
        # ── hard stops, before spending a call ──
        if state.stuck_count >= STUCK_KILL_AT:
            await emitter.error("stuck: repeated actions had no effect", ErrorCode.STUCK.value)
            return {
                "status": "failed",
                "error_code": ErrorCode.STUCK,
                "finished": True,
                "success": False,
                "reason": "Stuck — the same actions kept having no effect on the page.",
            }

        recent = state.recent_actions
        repeats = repetition_count(recent, recent[-1]) if recent else 0
        # Plain repetition only counts for COMMITTING verbs: scrolling five times while
        # reading a long list is the job, not a loop.
        committing = state.last_action is not None and is_repetition_candidate(state.last_action)
        if committing and repeats >= REPEAT_KILL_AT:
            await emitter.error("stuck: the same action repeated", ErrorCode.STUCK.value)
            return {
                "status": "failed",
                "error_code": ErrorCode.STUCK,
                "finished": True,
                "success": False,
                "reason": "Stuck — the same action was repeated without making progress.",
            }

        # Oscillation is the hole the exemption above leaves: a loop built entirely of
        # verbs that are individually allowed to repeat.
        if is_oscillating(recent, at_least=OSCILLATION_KILL_AT):
            await emitter.error("stuck: alternating between the same views", ErrorCode.STUCK.value)
            return {
                "status": "failed",
                "error_code": ErrorCode.STUCK,
                "finished": True,
                "success": False,
                "reason": "Stuck — alternating between the same two views without progressing.",
            }

        if state.step >= max_steps:
            return {
                "status": "failed",
                "error_code": ErrorCode.MAX_STEPS,
                "finished": True,
                "success": False,
                "reason": f"ran out of steps after {max_steps}",
            }

        # ── nudges and feedback, appended for this turn only ──
        nudges: list[Message] = []

        # The measured outcome of the last action. A success flag cannot express "it worked
        # and nothing moved", which is the most common way a run is wasted.
        outcome = derive_outcome(
            result=state.last_result,
            page_changed=state.stuck_count == 0,
            is_first_action=state.last_action is None,
        )
        if (note := outcome_note(outcome, state.last_action.name if state.last_action else None)):
            nudges.append(Message(role="user", content=note))

        # Human corrections the model has not yet seen. Applied FIRST among feedback, and
        # marked applied afterwards — unshown feedback is a broken promise to the user.
        applied_feedback: list[str] = []
        if feedback is not None:
            for pending in await feedback.pending(state.thread_id):
                nudges.append(
                    Message(role="user", content=f"Correction from the user: {pending.text}")
                )
                applied_feedback.append(pending.text)

        if committing and repeats >= REPEAT_NUDGE_AT:
            nudges.append(Message(role="user", content=repetition_nudge(repeats)))
        elif is_oscillating(recent):
            nudges.append(Message(role="user", content=oscillation_nudge()))
        if state.stuck_count >= STUCK_NUDGE_AT:
            nudges.append(Message(role="user", content=stuck_nudge(state.stuck_count)))
        if (reminder := budget_reminder(state.step, max_steps)) is not None:
            nudges.append(Message(role="user", content=reminder))

        # Compact the HISTORY only. The system prompt, the task, the fresh observation and
        # this turn's nudges are all things the next decision depends on — shrinking those
        # to save tokens would trade the run's correctness for its length.
        history, compaction = compact(list(state.messages), budget_tokens=context_budget)
        if compaction.changed:
            logger.info(
                "compacted history %d -> %d tokens (%s)",
                compaction.before_tokens,
                compaction.after_tokens,
                "; ".join(compaction.applied),
            )

        messages = [
            Message(role="system", content=WORKER_SYSTEM, cacheable=True),
            Message(role="user", content=task_block(state)),
            *history,
            Message(role="user", content=observation_block(state)),
            *nudges,
        ]

        # Which tools this turn may use. Resolved from state when the worker is chosen at
        # runtime — a `summarize` run and a `triage` run share this node but must never
        # share a capability set.
        bound = tools(state) if callable(tools) else tools

        # The model call is the long pause in every turn. Saying so beforehand is the
        # difference between an agent that looks like it is thinking and one that looks
        # like it has crashed.
        await emitter.activity("thinking", "deciding what to do next")
        result = await llm.complete(role="executor", messages=messages, tools=bound)

        # ── think-before-act ──
        if result.tool_calls and not result.has_reasoning:
            retry = await llm.complete(
                role="executor",
                messages=[
                    *messages,
                    Message(
                        role="user",
                        content="Explain your reasoning in plain text BEFORE calling a tool.",
                    ),
                ],
                tools=bound,
            )
            if retry.tool_calls and not retry.has_reasoning:
                await emitter.error("no reasoning after retry", ErrorCode.REASONING_MISSING.value)
                return {
                    "status": "failed",
                    "error_code": ErrorCode.REASONING_MISSING,
                    "finished": True,
                    "success": False,
                    "reason": "the model acted without explaining itself",
                }
            result = retry

        # The model's own verdict on its last action, surfaced as a distinct signal. "It
        # noticed the click did nothing" is different information from "it is about to
        # click", and merging them makes both harder to read.
        assessment, remainder = split_assessment(result.explanation)
        explanation = clip_reasoning(remainder)

        if assessment:
            await emitter.assessment(assessment, outcome.value)
        if explanation:
            await emitter.reasoning(explanation)
        await emitter.usage(result.provider, "executor", result.usage, result.latency_ms)

        if feedback is not None:
            if applied_feedback:
                await feedback.mark_applied(state.thread_id)
            if assessment:
                await feedback.record(
                    Feedback(
                        thread_id=state.thread_id,
                        kind=FeedbackKind.ASSESSMENT,
                        text=assessment,
                        step=state.step,
                        action=state.last_action.name if state.last_action else None,
                        outcome=outcome,
                        applied=True,
                    )
                )

        # ── no tool call: nudge once, then finalize typed ──
        if not result.tool_calls:
            if state.nudge_count < 1:
                return {
                    "messages": [
                        Message(role="assistant", content=explanation),
                        Message(
                            role="user",
                            content="You did not call a tool. Call one, or Complete().",
                        ),
                    ],
                    "nudge_count": state.nudge_count + 1,
                    "step": state.step + 1,
                }
            return {
                "status": "failed",
                "error_code": ErrorCode.NO_ACTION,
                "finished": True,
                "success": False,
                "reason": "the model stopped choosing actions",
            }

        # Re-id the call to its verb before it enters history.
        #
        # A tool result has to be pairable with the call it answers, and every result in
        # this loop echoes `tool_call_id=call.name`. The provider's own id ("call_abc123")
        # matches none of them, so the two halves of the conversation disagree the moment
        # the history is replayed to a DIFFERENT provider than produced it — which is
        # exactly what the fallback chain does on every 429.
        #
        # Gemini's OpenAI-compatible shim resolves a `function_response`'s name by looking
        # up its id among the preceding calls. No match means no name, and it rejects the
        # whole request: "function_response.name: Name cannot be empty". OpenAI-shaped
        # providers never complained, so the mismatch was invisible until the day Groq ran
        # out of quota.
        #
        # The verb is a sound id here because the loop takes exactly one call per turn, so
        # it is unique within the message that carries it.
        raw = result.tool_calls[0]
        call = raw.model_copy(update={"id": raw.name})
        await emitter.tool_call(call.name, call.args)

        return {
            "messages": [Message(role="assistant", content=explanation, tool_calls=[call])],
            "last_action": ActionCall(name=call.name, args=call.args),
            "step": state.step + 1,
            "history": [
                StepRecord(
                    step=state.step + 1,
                    node="reason",
                    worker=state.active_worker,
                    action=call.name,
                    provider=result.provider,
                    role="executor",
                    usage=result.usage,
                    latency_ms=result.latency_ms,
                )
            ],
        }

    return reason


def build_act_node(
    surface: EmailSurface,
    emitter: EventEmitter,
    *,
    tools: tuple[type[BaseModel], ...] | ToolsFor | None = None,
):
    """Perform the chosen action, or handle it internally."""

    async def act(state: AgentState) -> dict:
        call = state.last_action
        if call is None:
            return {"reason": "nothing to act on"}

        # Every action is recorded. The two guards READ this window differently — plain
        # repetition only counts committing verbs, oscillation counts everything — but a
        # window that omitted scrolls could not see a scroll-based loop at all.
        delta: dict = {"recent_actions": push_action(state.recent_actions, action_signature(call))}

        # Capability check against the WORKER's tool set, here rather than at the surface.
        # The surface knows which verbs it can physically perform; only the graph knows which
        # verbs this run is allowed to use. Letting the surface answer both questions is how a
        # read-only run ends up with a dispatchable Send.
        if tools is not None:
            bound = tools(state) if callable(tools) else tools
            allowed = {tool.__name__ for tool in bound}
            if call.name not in allowed:
                result = ActionResult(
                    success=False,
                    reason=f"{call.name} is not available to the {state.active_worker} worker",
                    error_code="VERB_NOT_BOUND",
                )
                await emitter.action_result(result.success, result.reason, result.error_code)
                return {
                    **delta,
                    "last_result": result,
                    "messages": [
                        Message(role="tool", content=result.reason, tool_call_id=call.name)
                    ],
                }

        if call.name in INTERNAL_VERBS:
            return {**delta, **await handle_internal(call, state, emitter)}

        await emitter.activity("acting", call.name)
        try:
            result = await surface.act(call)
        except SurfaceUnavailable as exc:
            await emitter.error(str(exc), ErrorCode.SURFACE_UNAVAILABLE.value)
            return {
                **delta,
                "status": "failed",
                "error_code": ErrorCode.SURFACE_UNAVAILABLE,
                "finished": True,
                "success": False,
                "reason": str(exc),
            }

        await emitter.action_result(result.success, result.reason, result.error_code)

        return {
            **delta,
            "last_result": result,
            "messages": [
                Message(
                    role="tool",
                    content=f"{'ok' if result.success else 'failed'}: {result.reason}",
                    tool_call_id=call.name,
                )
            ],
            "history": [
                StepRecord(
                    step=state.step,
                    node="act",
                    worker=state.active_worker,
                    action=call.name,
                    success=result.success,
                    error_code=result.error_code,
                    undo=result.undo,
                )
            ],
        }

    return act


