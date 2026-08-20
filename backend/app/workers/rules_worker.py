"""`RulesWorker` — deterministic execution, and the reason the linear route exists.

**Zero LLM calls.** Not "fewer" — none. On a free tier where the binding constraint is
requests per day, that is the difference between one triage run and unlimited ones, and it
is the whole payoff of classifying topology in PRE.

The loop is observe → match → act → **re-observe**, one item at a time. Batching would be
faster and wrong: archiving row 3 renumbers everything below it, so a list of indices
collected up front is stale after the first action. Re-observing costs nothing here (no
model call) and makes the stale-index class of bug impossible rather than merely unlikely.

A progress guard is mandatory, not defensive — and it must measure the PAGE, not the
action's own success flag. An action that reports success while changing nothing is the most
common way a browser agent wastes a run, and a guard that trusts the flag will happily
"archive" the same row two hundred times. A free-of-charge infinite loop is still an
infinite loop.
"""
from __future__ import annotations

import logging
import re

from inbox_contracts import ActionCall, Element, Observation

from app.agent.guards import page_signature
from app.agent.state import AgentState
from app.events.emitter import EventEmitter
from app.rules.store import Rule, RulesStore
from app.surface.base import EmailSurface, SurfaceUnavailable
from app.telemetry.records import ErrorCode, StepRecord

logger = logging.getLogger(__name__)

#: Ceiling on items handled in one run. A mailbox is unbounded; a run is not.
MAX_ITEMS = 200

#: Consecutive turns allowed to pass without the item count falling. Above this the actions
#: are not landing, and continuing would archive nothing forever.
MAX_STALLS = 3

#: Verbs a rule may run. Deliberately excludes every gated verb: a deterministic rule that
#: could send mail would be the one path around the approval gate, and it does not exist.
ALLOWED_RULE_VERBS = frozenset({"Archive", "MarkRead", "Label", "Snooze"})


def matching_elements(observation: Observation, rule: Rule) -> list[Element]:
    """Rows whose text matches the rule, in reading order."""
    patterns = [re.compile(pattern, re.IGNORECASE) for pattern in rule.patterns]
    return [
        element
        for element in observation.elements
        if element.role in ("listitem", "row", "generic")
        and element.name.strip()
        and any(pattern.search(element.name) for pattern in patterns)
    ]


def build_linear_node(surface: EmailSurface, emitter: EventEmitter, rules: RulesStore):
    """Run a matched rule to completion, deterministically."""

    async def linear(state: AgentState) -> dict:
        rule = rules.match(state.task, state.intent.action.value if state.intent else None)
        if rule is None:
            # The router said linear, but nothing matches now. Fail typed rather than
            # silently doing nothing and reporting success.
            return {
                "finished": True,
                "success": False,
                "status": "failed",
                "error_code": ErrorCode.NO_ACTION,
                "reason": "no rule matched this task, so there was nothing deterministic to run",
            }

        verbs = [verb for verb in rule.actions if verb in ALLOWED_RULE_VERBS]
        if not verbs:
            return {
                "finished": True,
                "success": False,
                "status": "failed",
                "error_code": ErrorCode.NO_ACTION,
                "reason": f"rule {rule.name!r} has no permitted action",
            }

        await emitter.status("running", f"applying rule {rule.name!r} — no model calls")

        handled = 0
        stalls = 0
        history: list[StepRecord] = []
        previous_signature = ""

        while handled < MAX_ITEMS:
            try:
                observation = await surface.observe()
            except SurfaceUnavailable as exc:
                await emitter.error(str(exc), ErrorCode.SURFACE_UNAVAILABLE.value)
                return {
                    "finished": True,
                    "success": False,
                    "status": "failed",
                    "error_code": ErrorCode.SURFACE_UNAVAILABLE,
                    "reason": str(exc),
                }

            # Measured, not claimed: if the page is byte-identical to last turn, nothing
            # landed, whatever the action result said.
            signature = page_signature(observation)
            page_moved = signature != previous_signature
            previous_signature = signature

            matches = matching_elements(observation, rule)
            if not matches:
                break

            if handled and not page_moved:
                stalls += 1
                if stalls >= MAX_STALLS:
                    return _stalled(rule, handled, history)
                continue

            # One at a time, then look again: acting on row 3 renumbers everything below it.
            target = matches[0]
            progressed = False
            for verb in verbs:
                call = ActionCall(name=verb, args=_args_for(verb, target, rule))
                await emitter.tool_call(call.name, call.args)
                result = await surface.act(call)
                await emitter.action_result(result.success, result.reason, result.error_code)
                history.append(
                    StepRecord(
                        step=state.step + len(history) + 1,
                        node="linear",
                        worker="rules",
                        action=verb,
                        success=result.success,
                        undo=result.undo,
                    )
                )
                progressed = progressed or result.success

            if progressed:
                handled += 1
                stalls = 0
            else:
                stalls += 1
                if stalls >= MAX_STALLS:
                    return _stalled(rule, handled, history)

        summary = (
            f"Applied rule {rule.name!r} to {handled} item{'' if handled == 1 else 's'} "
            "with no model calls."
        )
        logger.info(summary)
        return {
            "finished": True,
            "success": True,
            "status": "done",
            "reason": summary,
            "active_worker": "rules",
            "history": history,
        }

    return linear


def _args_for(verb: str, target: Element, rule: Rule) -> dict:
    args: dict[str, object] = {"index": target.index}
    if verb == "Label":
        # A label rule names its label; without one there is nothing to apply.
        args["label"] = rule.name
    if verb == "Snooze":
        args["until"] = "tomorrow 9am"
    return args


def _stalled(rule: Rule, handled: int, history: list[StepRecord]) -> dict:
    """Give up, typed, rather than grinding on a page that never changes."""
    return {
        "finished": True,
        "success": False,
        "status": "failed",
        "error_code": ErrorCode.STUCK,
        "reason": (
            f"rule {rule.name!r} stopped having any effect after {handled} item(s) — "
            "the actions reported success but the page never changed."
        ),
        "history": history,
    }
