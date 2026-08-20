"""Keeping a long run inside the context window.

`messages` grows by two entries a turn — an assistant turn carrying its reasoning and tool
call, then the tool's result. Reasoning is already clipped at ~3k characters, so a forty-step
triage run can still reach tens of thousands of tokens and fail on a small model *at the very
end*, having done all the work.

Three layers, cheapest first, applied only as far as needed:

  0. **Trim old tool results.** "archived [4]" needs no more than a line, and old results are
     the least useful tokens in the history.
  1. **Drop old reasoning text.** Keep the tool CALL, lose the prose explaining it. What the
     agent did still matters; why it did it eight turns ago rarely does.
  2. **Summarise the middle.** Replace a span with one line saying what happened in it.

**The trap that shapes all of this:** an assistant message carrying tool calls must be
followed by its matching tool result, or the provider rejects the whole conversation with an
opaque 400 several turns later. So compaction moves in PAIRS and never orphans a call — a
naive "drop the oldest N messages" would corrupt the conversation roughly half the time.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.llm.base import Message

#: Characters per token, near enough. Running a real tokenizer over the whole history every
#: turn would cost more than the tokens it saves.
CHARS_PER_TOKEN = 4

#: Turns kept verbatim at the start — the task and its first move, which anchor everything.
KEEP_HEAD = 2
#: Turns kept verbatim at the end — the recent context the next decision depends on.
KEEP_TAIL = 6

#: How much of an old tool result survives layer 0.
TOOL_RESULT_CHARS = 80

#: Compaction starts here, as a fraction of the budget. Waiting for 100% means the turn that
#: triggers it is the turn that fails.
TRIGGER_RATIO = 0.85


@dataclass(frozen=True)
class CompactionReport:
    """What was done, so the cockpit can say so rather than quietly losing history."""

    applied: list[str]
    before_tokens: int
    after_tokens: int
    dropped_messages: int

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def estimate_tokens(messages: list[Message]) -> int:
    return sum(len(m.content) for m in messages) // CHARS_PER_TOKEN


def _pairs(messages: list[Message]) -> list[list[Message]]:
    """Group into units that must survive or vanish together.

    An assistant turn with tool calls owns the tool results that answer it. Splitting that
    pair is what produces the opaque provider rejection this module exists to avoid.
    """
    groups: list[list[Message]] = []
    for message in messages:
        if message.role == "tool" and groups and groups[-1][0].role == "assistant":
            groups[-1].append(message)
        else:
            groups.append([message])
    return groups


def compact(
    messages: list[Message], *, budget_tokens: int
) -> tuple[list[Message], CompactionReport]:
    """Shrink `messages` to fit, doing the least damage that suffices."""
    before = estimate_tokens(messages)
    target = int(budget_tokens * TRIGGER_RATIO)
    if before <= target:
        return messages, CompactionReport([], before, before, 0)

    applied: list[str] = []
    groups = _pairs(messages)
    head, middle, tail = groups[:KEEP_HEAD], groups[KEEP_HEAD:-KEEP_TAIL], groups[-KEEP_TAIL:]
    if not middle:
        # Nothing old enough to touch: the recent turns alone exceed the budget, and
        # trimming those would remove the context the next decision needs.
        return messages, CompactionReport([], before, before, 0)

    # ── layer 0: trim old tool results ──
    middle = [[_trim_tool(m) for m in group] for group in middle]
    applied.append("trimmed old results")

    if _fits(head, middle, tail, target):
        return _assemble(head, middle, tail, applied, before, 0)

    # ── layer 1: drop old reasoning, keep the actions ──
    middle = [[_strip_reasoning(m) for m in group] for group in middle]
    applied.append("dropped old reasoning")

    if _fits(head, middle, tail, target):
        return _assemble(head, middle, tail, applied, before, 0)

    # ── layer 2: replace the middle with one line ──
    dropped = sum(len(group) for group in middle)
    summary = [[_summarise(middle)]]
    applied.append("summarised the middle")
    return _assemble(head, summary, tail, applied, before, dropped)


def _fits(head, middle, tail, target: int) -> bool:
    flat = [m for group in (*head, *middle, *tail) for m in group]
    return estimate_tokens(flat) <= target


def _assemble(
    head, middle, tail, applied, before, dropped
) -> tuple[list[Message], CompactionReport]:
    flat = [m for group in (*head, *middle, *tail) for m in group]
    return flat, CompactionReport(applied, before, estimate_tokens(flat), dropped)


def _trim_tool(message: Message) -> Message:
    if message.role != "tool" or len(message.content) <= TOOL_RESULT_CHARS:
        return message
    return message.model_copy(update={"content": message.content[:TOOL_RESULT_CHARS] + "…"})


def _strip_reasoning(message: Message) -> Message:
    """Keep the tool call, lose the prose.

    The call must stay: dropping it would orphan the tool result that answers it.
    """
    if message.role != "assistant" or not message.tool_calls:
        return message
    verbs = ", ".join(call.name for call in message.tool_calls)
    return message.model_copy(update={"content": f"(called {verbs})"})


def _summarise(middle: list[list[Message]]) -> Message:
    """One line standing in for a span of turns.

    Deterministic, not an LLM call. Compaction fires when the window is nearly full, which is
    exactly when an extra call is most likely to fail — and a summariser that needs the thing
    it is rescuing is no rescue at all.
    """
    verbs: list[str] = []
    for group in middle:
        for message in group:
            verbs.extend(call.name for call in message.tool_calls)

    counted: dict[str, int] = {}
    for verb in verbs:
        counted[verb] = counted.get(verb, 0) + 1
    detail = ", ".join(f"{verb} x{n}" if n > 1 else verb for verb, n in counted.items())

    return Message(
        role="user",
        content=(
            f"[earlier in this run, condensed: {len(middle)} turns — {detail or 'no actions'}. "
            "Those steps are done; do not repeat them.]"
        ),
    )
