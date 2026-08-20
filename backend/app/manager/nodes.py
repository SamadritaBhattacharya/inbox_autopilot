"""PRE-phase nodes: intake, context_gate, router, planner.

Every node here is a **closure over injected ports** returning a **state delta**. None of
them contains an LLM, browser, or database call inline — they call a port and shape the
result. That is what lets the whole PRE phase be tested with a scripted `FakeLLMClient` and
no network.

Closures, not `functools.partial`: LangGraph inspects callables to decide whether to await
them, and a partial wrapping a coroutine can be mis-detected as synchronous. The node then
never runs and the graph stalls with no error worth reading.
"""
from __future__ import annotations

import json
import logging

from app.agent.state import AgentState
from app.llm.base import LLMClient, Message
from app.manager.intent import Action, Plan, Route, TaskIntent
from app.manager.slots import confidence, is_ready, missing_slots, question_for
from app.rules.store import RulesStore
from app.telemetry.records import ErrorCode, StepRecord

logger = logging.getLogger(__name__)

#: A gate that can ask forever can hang a run forever. After this many rounds the run ends
#: typed (`CONTEXT_INCOMPLETE`) rather than pinging a human who has clearly stopped reading.
MAX_ASKS = 3

_INTAKE_SYSTEM = """You convert an email request into structured JSON.

Return ONLY a JSON object:
{"action": "<action>", "slots": {...}, "confidence": <0-1>, "constraints": [...]}

action is one of: send_email, reply, triage, archive, label, snooze, search,
extract_event, apply_rules, unknown.

Slot names by action:
  send_email    recipient_identity, topic, body_intent, tone, cc, subject
  reply         thread_ref, stance, body_intent, tone
  triage        scope, aggressiveness
  archive       selector
  label         selector, target_label
  snooze        selector, until
  search        query
  extract_event thread_ref

Rules:
- Only fill a slot the user actually specified. NEVER invent a recipient, a subject, or a
  body. An invented slot is worse than a missing one: a missing slot gets asked about, an
  invented one gets acted on.
- Use "unknown" when the request is not about email.
- confidence is about the ACTION, not about how complete the slots are."""


def _parse_intent(raw: str) -> TaskIntent:
    """Best-effort parse of the classifier's JSON.

    A malformed response becomes `UNKNOWN`, not an exception: the gate then asks the human
    what they meant, which is a far better outcome than a crashed run — and it keeps a flaky
    model from being able to take the process down.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return TaskIntent()

    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        logger.warning("intake returned unparseable JSON")
        return TaskIntent()

    try:
        action = Action(str(payload.get("action", "unknown")).lower())
    except ValueError:
        action = Action.UNKNOWN

    slots = {
        str(k): str(v).strip()
        for k, v in (payload.get("slots") or {}).items()
        if v is not None and str(v).strip()
    }
    try:
        action_confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        action_confidence = 0.0

    return TaskIntent(
        action=action,
        slots=slots,
        action_confidence=max(0.0, min(1.0, action_confidence)),
        constraints=[str(c) for c in (payload.get("constraints") or [])],
    )


def build_intake_node(llm: LLMClient):
    """NL task -> typed `TaskIntent`. No side effects, no mailbox access."""

    async def intake(state: AgentState) -> dict:
        result = await llm.complete(
            role="classifier",
            messages=[
                Message(role="system", content=_INTAKE_SYSTEM, cacheable=True),
                Message(role="user", content=state.task),
            ],
        )
        intent = _parse_intent(result.text or result.reasoning)

        logger.info("intake: %s (confidence %.2f)", intent.action, intent.action_confidence)
        return {
            "intent": intent,
            "missing_slots": missing_slots(intent),
            "history": [
                StepRecord(
                    step=state.step,
                    node="intake",
                    provider=result.provider,
                    role="classifier",
                    usage=result.usage,
                    latency_ms=result.latency_ms,
                )
            ],
        }

    return intake


def build_context_gate_node(*, threshold: float = 0.85, max_asks: int = MAX_ASKS):
    """The 100%-context rule (R3).

    Emits a question and pauses when anything required is missing. **Nothing downstream can
    run before this clears** — not by convention, but because the graph has no edge that
    skips it.

    The pause is a LangGraph `interrupt`, so it survives a process restart and a cockpit
    reconnect. A blocking prompt would tie the run's life to one socket; a human who steps
    away for ten minutes would come back to a dead run.
    """

    async def context_gate(state: AgentState) -> dict:
        intent = state.intent or TaskIntent()

        # Fold any answer the human has given back into the intent before re-checking. The
        # answer arrives as free text, so it is attached to every slot still missing — the
        # next intake pass will refine it, and a slot filled with the wrong thing is caught
        # here rather than acted on.
        if state.answers and state.pending_question:
            latest = state.answers[-1]
            intent = intent.with_slots(**{name: latest for name in missing_slots(intent)})

        outstanding = missing_slots(intent)
        score = confidence(intent)

        if is_ready(intent, threshold=threshold):
            logger.info("context gate cleared (confidence %.2f)", score)
            return {
                "intent": intent,
                "missing_slots": [],
                "pending_question": None,
                "status": "running",
            }

        if state.ask_count >= max_asks:
            # Typed, not silent. A run that gives up without a code cannot be counted.
            return {
                "intent": intent,
                "missing_slots": outstanding,
                "status": "failed",
                "error_code": ErrorCode.CONTEXT_INCOMPLETE,
                "finished": True,
                "success": False,
                "reason": (
                    "I still don't have enough to start safely after "
                    f"{max_asks} attempts — missing: {', '.join(outstanding)}."
                ),
            }

        return {
            "intent": intent,
            "missing_slots": outstanding,
            "pending_question": question_for(intent),
            "status": "awaiting_human",
            "ask_count": state.ask_count + 1,
        }

    return context_gate


_ROUTER_SYSTEM = """Classify the execution topology of an email task.

Answer with ONE word:
  linear   - deterministic and mechanical; the same steps every time, no judgement per item
             ("archive all newsletters", "mark everything from X as read")
  decision - needs perception and judgement per item
             ("reply to the ones that need me", "book the meeting from this thread")

If unsure, answer decision: treating a judgement task as mechanical produces confident
wrong actions, whereas treating a mechanical task as judgement only costs tokens."""


def build_router_node(llm: LLMClient, rules: RulesStore):
    """Linear vs decision (R6).

    A deterministic rule match short-circuits the classifier entirely — the cheapest correct
    path is tried first, and a rule-matched task costs **zero** LLM calls to route.
    """

    async def router(state: AgentState) -> dict:
        intent = state.intent or TaskIntent()

        matched = rules.match(state.task, intent.action.value)
        if matched is not None:
            logger.info("router: rule %r matched — linear, no classifier call", matched.name)
            return {
                "route": Route(
                    topology="linear",
                    why=f"matched rule {matched.name!r}",
                    rule_matched=True,
                ),
                "status": "running",
            }

        result = await llm.complete(
            role="classifier",
            messages=[
                Message(role="system", content=_ROUTER_SYSTEM, cacheable=True),
                Message(role="user", content=state.task),
            ],
        )
        answer = (result.text or result.reasoning).strip().lower()
        topology = "linear" if answer.startswith("linear") else "decision"

        return {
            "route": Route(topology=topology, why=f"classifier said {answer[:40]!r}"),
            "status": "running",
            "history": [
                StepRecord(
                    step=state.step,
                    node="router",
                    provider=result.provider,
                    role="classifier",
                    usage=result.usage,
                    latency_ms=result.latency_ms,
                )
            ],
        }

    return router


_PLANNER_SYSTEM = """Given an email task, list the 3-6 steps you will take, one per line,
no numbering and no commentary. Each step is a concrete action in a mail UI."""


def build_planner_node(llm: LLMClient):
    """Post intent to the cockpit before acting (decision route only).

    A plan is not a script the loop must follow — it exists so a human sees what the agent
    intends *before* the first action, rather than reconstructing it from a transcript
    afterwards.
    """

    async def planner(state: AgentState) -> dict:
        result = await llm.complete(
            role="executor",
            messages=[
                Message(role="system", content=_PLANNER_SYSTEM, cacheable=True),
                Message(role="user", content=state.task),
            ],
        )
        steps = [
            line.strip(" -•\t")
            for line in (result.text or result.reasoning).splitlines()
            if line.strip()
        ][:6]

        return {
            "plan": Plan(steps=steps, rationale=result.explanation[:300]),
            "history": [
                StepRecord(
                    step=state.step,
                    node="planner",
                    provider=result.provider,
                    role="executor",
                    usage=result.usage,
                    latency_ms=result.latency_ms,
                )
            ],
        }

    return planner
