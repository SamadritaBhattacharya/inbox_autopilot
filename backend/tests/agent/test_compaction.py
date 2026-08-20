"""Context compaction — keeping a long run inside the window without corrupting it."""
from __future__ import annotations

import pytest

from app.agent.compaction import (
    KEEP_HEAD,
    KEEP_TAIL,
    compact,
    estimate_tokens,
)
from app.llm.base import Message, ToolCall


def turn(n: int, *, reasoning: str = "", verb: str = "Archive") -> list[Message]:
    """One agent turn: an assistant message with a tool call, and the tool's answer."""
    return [
        Message(
            role="assistant",
            content=reasoning or f"Thinking hard about item {n}. " * 40,
            tool_calls=[ToolCall(id=f"c{n}", name=verb, args={"index": n})],
        ),
        Message(role="tool", content=f"ok: {verb} [{n}] " + "detail " * 30, tool_call_id=f"c{n}"),
    ]


def history(turns: int) -> list[Message]:
    messages = [Message(role="user", content="clear the inbox")]
    for n in range(1, turns + 1):
        messages.extend(turn(n))
    return messages


def tool_calls_are_answered(messages: list[Message]) -> bool:
    """Every assistant tool call must be followed by a tool result.

    This is the invariant compaction exists to preserve: providers reject a conversation
    with an orphaned call, and the rejection arrives as an opaque 400 several turns later.
    """
    for index, message in enumerate(messages):
        if message.role == "assistant" and message.tool_calls:
            following = messages[index + 1 : index + 2]
            if not following or following[0].role != "tool":
                return False
    return True


# ── when nothing is needed ──────────────────────────────────────────────────


def test_a_short_history_is_left_completely_alone():
    messages = history(2)
    compacted, report = compact(messages, budget_tokens=100_000)

    assert compacted == messages
    assert report.changed is False


def test_recent_turns_alone_over_budget_are_not_touched():
    """Trimming these would remove the context the next decision actually needs.

    With nothing older than the head and tail, there is no middle to compact — and the
    right answer is to leave it alone rather than damage the only context that matters.
    """
    messages = history(KEEP_HEAD + KEEP_TAIL - 2)  # every group is head or tail
    _, report = compact(messages, budget_tokens=1)
    assert report.changed is False


# ── the layers, cheapest first ──────────────────────────────────────────────


def test_it_starts_by_trimming_old_results():
    messages = history(20)
    _, report = compact(messages, budget_tokens=estimate_tokens(messages) // 2)
    assert report.applied[0] == "trimmed old results"


def test_it_escalates_only_as_far_as_needed():
    messages = history(30)
    _, report = compact(messages, budget_tokens=200)

    assert "dropped old reasoning" in report.applied
    assert report.after_tokens < report.before_tokens


def test_the_last_resort_condenses_the_middle():
    messages = history(40)
    compacted, report = compact(messages, budget_tokens=120)

    assert "summarised the middle" in report.applied
    assert any("condensed" in m.content for m in compacted)
    assert report.dropped_messages > 0


# ── the invariant that shapes the whole module ──────────────────────────────


@pytest.mark.parametrize("budget", [10, 50, 120, 400, 2_000, 20_000])
def test_no_tool_call_is_ever_orphaned(budget):
    """A naive drop-the-oldest-N would corrupt the conversation about half the time."""
    compacted, _ = compact(history(25), budget_tokens=budget)
    assert tool_calls_are_answered(compacted)


def test_dropping_reasoning_keeps_the_action():
    """What the agent DID still matters; why it did it eight turns ago rarely does.

    Budget chosen so layer 1 is enough and layer 2 never runs — the point is what layer 1
    leaves behind, and summarising would delete the very message being inspected.
    """
    compacted, report = compact(history(30), budget_tokens=5_000)

    assert "dropped old reasoning" in report.applied
    assert "summarised the middle" not in report.applied

    stripped = [m for m in compacted if m.role == "assistant" and "(called" in m.content]
    assert stripped, "the calls themselves must survive, stripped of their prose"
    assert all(m.tool_calls for m in stripped), "a stripped message keeps its tool call"


def test_the_task_and_the_recent_turns_survive():
    compacted, _ = compact(history(40), budget_tokens=150)

    assert compacted[0].content == "clear the inbox"
    # The final turn is the one the next decision depends on.
    assert "40" in compacted[-1].content or "40" in compacted[-2].content


def test_the_summary_says_what_happened_so_work_is_not_repeated():
    compacted, _ = compact(history(40), budget_tokens=120)
    summary = next(m for m in compacted if "condensed" in m.content)

    assert "Archive" in summary.content
    assert "do not repeat" in summary.content


# ── it needs no model call ──────────────────────────────────────────────────


def test_compaction_is_deterministic_and_offline():
    """It fires when the window is nearly full — exactly when an extra call is most likely
    to fail. A summariser that needs the thing it is rescuing is no rescue."""
    first, _ = compact(history(30), budget_tokens=200)
    second, _ = compact(history(30), budget_tokens=200)
    assert [m.content for m in first] == [m.content for m in second]


def test_it_actually_gets_under_budget_when_it_can():
    messages = history(40)
    _, report = compact(messages, budget_tokens=400)
    assert report.after_tokens < report.before_tokens // 2
