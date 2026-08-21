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

import re

import json
import logging

from app.agent.state import AgentState
from app.events.emitter import EventEmitter
from app.llm.base import LLMClient, Message
from app.manager.intent import Action, Plan, Route, TaskIntent
from app.manager.slots import (
    apply_defaults,
    confidence,
    is_ready,
    missing_slots,
    question_for,
)
from app.prompts import load_prompt
from app.rules.store import RulesStore
from app.security.patterns import find_emails
from app.security.vault import SessionPiiVault
from app.telemetry.records import ErrorCode, StepRecord
from app.workers.registry import worker_for

logger = logging.getLogger(__name__)

#: A gate that can ask forever can hang a run forever. After this many rounds the run ends
#: typed (`CONTEXT_INCOMPLETE`) rather than pinging a human who has clearly stopped reading.
MAX_ASKS = 3

_INTAKE_SYSTEM = load_prompt("intake")


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


def build_intake_node(
    llm: LLMClient,
    emitter: EventEmitter | None = None,
    vault: SessionPiiVault | None = None,
):
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

        # An address the USER typed is trusted input: they chose it, so it is somewhere they
        # meant to write. Minting it here is what makes "send an email to alice@x.com" work
        # at all — the dispatcher only accepts vault tokens, and an address that never
        # appeared on the page would otherwise have none.
        #
        # This is the same distinction that refuses a recipient lifted out of a hostile email
        # body, seen from the other side: what matters is not the address, it is who put it
        # there.
        task = state.task
        if vault is not None:
            intent = _trust_user_addresses(intent, vault)
            # The TASK too, not just the slots. The worker is handed the task text verbatim,
            # so leaving a raw address in it does two bad things at once: it puts real PII in
            # front of the model — the one thing §13 exists to prevent — and it shows the
            # model an address it is then told is impossible and forbidden, which is a
            # deadlock it cannot reason its way out of. Both disappear if only the token is
            # ever visible.
            task = _trust_addresses(task, vault)

        if (implied := implied_body_intent(task, intent)) is not None:
            logger.info("intake: body intent taken from the task itself")
            intent = intent.with_slots(body_intent=implied)

        logger.info("intake: %s (confidence %.2f)", intent.action, intent.action_confidence)
        if emitter is not None:
            # The cockpit shows "I understood this as…" before anything happens, so a
            # misread is caught by the human at second zero rather than at step twelve.
            await emitter.intent(
                intent.action.value, intent.slots, intent.action_confidence
            )
        return {
            "task": task,
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


#: Actions whose content the operator can DESCRIBE rather than dictate. Only these can have
#: a body intent read out of the task; a triage or archive request has no body at all.
_DESCRIBABLE_ACTIONS = frozenset({Action.SEND_EMAIL, Action.REPLY, Action.FORWARD})

#: Words that carry no subject matter — the scaffolding of a request rather than its
#: content. Used only to decide whether a task said anything ABOUT the email.
_INSTRUCTION_WORDS = frozenset(
    """a an the to and or for me my please send email mail message write compose draft
    with about on of it that this then also can you could would should""".split()
)

_WORD_RE = re.compile(r"[a-zA-Z']+")
_TOKEN_RE = re.compile(r"^[PCT]\d+$", re.IGNORECASE)


def implied_body_intent(task: str, intent: TaskIntent) -> str | None:
    """The body intent the operator already gave, when the classifier failed to notice.

    "Write a good afternoon mail with short motivation and send it to P1" states perfectly
    clearly what the email should say. Asking "what should the email be about?" in reply is
    the single most irritating thing this agent can do, and it happens whenever one sampling
    of the classifier fills `recipient_identity` and forgets `body_intent`.

    This is NOT the "never invent a slot" rule being bent. Nothing is invented: the value is
    the operator's own sentence, handed to the writer, whose draft the human then sees and
    approves before anything sends. The rule exists to stop a *recipient* or a *body* being
    conjured from nowhere, and neither happens here.

    Returns `None` when the task genuinely says nothing about content — "send an email to
    P1" deserves the question, and getting that case wrong would mean writing an email out
    of thin air.
    """
    if intent.action not in _DESCRIBABLE_ACTIONS:
        return None
    if any(intent.slots.get(name, "").strip() for name in ("topic", "body_intent")):
        return None

    content = [
        word
        for word in _WORD_RE.findall(task)
        if word.lower() not in _INSTRUCTION_WORDS and not _TOKEN_RE.match(word)
    ]
    # Two content words is the line between "a good afternoon note, keep it short" and
    # "send an email to P1". Below it there is nothing to write from.
    return task if len(content) >= 2 else None


def _trust_addresses(text: str, vault: SessionPiiVault) -> str:
    """Swap every operator-supplied address in `text` for an addressable token."""
    for address in find_emails(text):
        text = text.replace(address, vault.trust(address))
    return text


def _trust_user_addresses(intent: TaskIntent, vault: SessionPiiVault) -> TaskIntent:
    """Replace operator-supplied addresses in the intent with addressable tokens."""
    updated: dict[str, str] = {}
    for slot, value in intent.slots.items():
        if not find_emails(value):
            continue
        updated[slot] = _trust_addresses(value, vault)
    return intent.with_slots(**updated) if updated else intent


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
            # Defaults are folded in permanently once the gate clears, so the worker sees
            # the same intent the gate approved rather than re-deriving it.
            intent = apply_defaults(intent)
            worker = worker_for(intent.action)
            logger.info(
                "context gate cleared (confidence %.2f) -> %s worker%s",
                score,
                worker.name,
                " [read-only]" if worker.read_only else "",
            )
            return {
                "intent": intent,
                "missing_slots": [],
                "pending_question": None,
                "status": "running",
                "active_worker": worker.name,
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


_ROUTER_SYSTEM = load_prompt("router")


def build_router_node(llm: LLMClient, rules: RulesStore, emitter: EventEmitter | None = None):
    """Linear vs decision (R6).

    A deterministic rule match short-circuits the classifier entirely — the cheapest correct
    path is tried first, and a rule-matched task costs **zero** LLM calls to route.
    """

    async def router(state: AgentState) -> dict:
        intent = state.intent or TaskIntent()

        matched = rules.match(state.task, intent.action.value)
        if matched is not None:
            logger.info("router: rule %r matched — linear, no classifier call", matched.name)
            if emitter is not None:
                await emitter.route("linear", f"matched rule {matched.name!r}", True)
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

        if emitter is not None:
            await emitter.route(topology, f"classifier said {answer[:40]!r}", False)
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


_PLANNER_SYSTEM = load_prompt("planner")


def build_planner_node(llm: LLMClient, emitter: EventEmitter | None = None):
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

        if emitter is not None:
            await emitter.plan(steps)
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
