"""Every tool result must be pairable with the call it answers.

**The bug.** The reason node stored the provider's own tool-call id ("call_abc123"), while
every tool result in the loop echoes `tool_call_id=<verb>`. The two halves of the
conversation therefore disagreed — invisibly, because OpenAI-shaped providers never check.

Gemini does. Its OpenAI-compatible shim resolves a `function_response`'s name by looking its
id up among the preceding calls; no match means no name, and it rejects the request with
"function_response.name: Name cannot be empty". So the mismatch only ever surfaced after
Groq hit its daily token cap and the chain fell through — i.e. mid-run, at the worst moment,
looking like a Gemini bug rather than a history bug.

The invariant is provider-independent and belongs in a test, not in an adapter workaround:
**a tool result's id must match its call's id.**
"""
from __future__ import annotations

from app.llm.base import Message, ToolCall
from app.llm.openai_compatible import message_to_openai


def paired(history: list[Message]) -> list[tuple[str, str]]:
    """(call id, result id) for each assistant/tool pair, as they go on the wire."""
    pairs: list[tuple[str, str]] = []
    pending: str | None = None
    for message in history:
        wire = message_to_openai(message)
        if wire.get("tool_calls"):
            pending = wire["tool_calls"][0]["id"]
        elif wire["role"] == "tool" and pending is not None:
            pairs.append((pending, wire["tool_call_id"]))
            pending = None
    return pairs


def reasoned(provider_id: str, verb: str, **args) -> Message:
    """The assistant message exactly as the reason node now builds it."""
    raw = ToolCall(id=provider_id, name=verb, args=args)
    call = raw.model_copy(update={"id": raw.name})
    return Message(role="assistant", content="doing it", tool_calls=[call])


def acted(verb: str) -> Message:
    """The tool result exactly as the act node builds it."""
    return Message(role="tool", content="ok: done", tool_call_id=verb)


def test_a_call_and_its_result_share_an_id():
    history = [reasoned("call_abc123", "Click", index=15), acted("Click")]

    assert paired(history) == [("Click", "Click")]


def test_the_pairing_survives_a_provider_id():
    """Groq and OpenRouter return real ids; Gemini returns none. History replays across all
    three, so the id cannot come from whichever provider happened to answer."""
    for provider_id in ("call_abc123", "", "toolu_01XYZ", "0"):
        history = [reasoned(provider_id, "Type", index=54), acted("Type")]

        call_id, result_id = paired(history)[0]
        assert call_id == result_id, f"{provider_id!r} broke the pairing"


def test_a_multi_turn_history_pairs_all_the_way_down():
    """The live failure named contents[2], [4] AND [6] — every tool result in the run, not
    just the first."""
    history = [
        Message(role="user", content="Task: email P1"),
        reasoned("call_1", "Click", index=15),
        acted("Click"),
        reasoned("call_2", "Type", index=54),
        acted("Type"),
        reasoned("call_3", "Type", index=72),
        acted("Type"),
    ]

    assert all(call == result for call, result in paired(history))


def test_no_wire_id_is_ever_empty():
    """An empty id is the other half of the same 400."""
    history = [reasoned("", "Click", index=15), acted("Click")]

    for call_id, result_id in paired(history):
        assert call_id and result_id


def test_a_tool_result_always_names_its_function():
    """Belt and braces: the shim prefers the id lookup, but the explicit name costs nothing
    and is what the OpenAI schema documents."""
    wire = message_to_openai(acted("Archive"))

    assert wire["name"] == "Archive"
