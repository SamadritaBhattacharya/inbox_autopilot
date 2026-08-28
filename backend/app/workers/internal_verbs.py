"""Verbs the graph owns, rather than the surface.

Memory, the plan, and completion never touch the page. Routing them through the browser
would be a round trip to accomplish a dictionary write — and it would make the surface
responsible for concepts (a plan, a run's success) that belong to the graph.

The dividing line is worth stating plainly: if a verb changes what is ON SCREEN, the surface
performs it; if it changes what the RUN KNOWS, this module does.
"""
from __future__ import annotations

from inbox_contracts import ActionCall
from langgraph.types import interrupt

from app.agent.state import AgentState
from app.events.emitter import EventEmitter
from app.llm.base import Message


async def handle_internal(call: ActionCall, state: AgentState, emitter: EventEmitter) -> dict:
    """Verbs the graph owns: memory, plan, completion.

    Handled here rather than at the surface because none of them touch the page — routing
    them through the browser would be a round trip to accomplish a dictionary write.
    """
    args = call.args

    if call.name == "Complete":
        success = bool(args.get("success"))
        reason = str(args.get("reason") or "")
        # Deliberately does NOT emit `finalize`. The run driver owns the single terminal
        # announcement — emitting here too gave the cockpit two terminal cards for one run.
        return {
            "finished": True,
            "success": success,
            "status": "done" if success else "failed",
            "reason": reason,
            "messages": [Message(role="tool", content="completed", tool_call_id=call.name)],
        }

    if call.name == "Remember":
        key, value = str(args.get("key") or ""), str(args.get("value") or "")
        await emitter.memory(key, value)
        return {
            "agent_memory": {**state.agent_memory, key: value},
            "messages": [Message(role="tool", content=f"remembered {key}", tool_call_id=call.name)],
        }

    if call.name == "Recall":
        dump = "; ".join(f"{k}={v}" for k, v in state.agent_memory.items()) or "(empty)"
        return {"messages": [Message(role="tool", content=dump, tool_call_id=call.name)]}

    if call.name == "SetPlan":
        from app.manager.intent import Plan

        steps = [str(step) for step in (args.get("steps") or [])]
        await emitter.plan(steps)
        return {
            "plan": Plan(steps=steps),
            "messages": [Message(role="tool", content="plan updated", tool_call_id=call.name)],
        }

    if call.name == "ProposeEvent":
        # A PROPOSAL, not a booking. Nothing is created and nobody is invited: the value is
        # turning a thread into something a human can check, and a mis-drafted proposal costs
        # nothing while a mis-sent invite lands in other people's calendars and cannot be
        # recalled. Attendees stay as tokens — the model never held the real addresses.
        proposal = {
            "title": str(args.get("title") or "").strip(),
            "when": str(args.get("when") or "").strip(),
            "duration": str(args.get("duration") or "").strip(),
            "attendees": str(args.get("attendees") or "").strip(),
            "evidence": str(args.get("evidence") or "").strip(),
        }
        await emitter.event_proposed(proposal)
        return {
            "agent_memory": {**state.agent_memory, "proposed_event": str(proposal)},
            "messages": [
                Message(
                    role="tool",
                    content=(
                        "Proposed (not created, nobody invited). Tell the user what you "
                        "drafted, then Complete."
                    ),
                    tool_call_id=call.name,
                )
            ],
        }

    if call.name == "AskUser":
        # A real pause, not a message into the void.
        #
        # This verb was bound and then not handled, which is worse than not offering it: the
        # model asked, got "AskUser is not handled" back, reasoned at length about whether it
        # had called it wrongly, tried again, and burned its step budget on a tool that
        # looked available. A remediation strategy actively recommends this verb, so the gap
        # was reachable by design rather than by accident.
        #
        # `interrupt()` raises out of the act node; the runtime checkpoints and stops. The
        # transport turns the payload into a question card, and `Command(resume=...)`
        # re-enters this node with the answer. Nothing is parked in memory, so the run
        # survives a disconnect while it waits.
        question = str(args.get("question") or "").strip() or "What would you like me to do?"
        await emitter.activity("waiting", "asking you a question")
        answer = interrupt({"question": question, "missing": [], "task": state.task})
        return {
            "status": "running",
            "messages": [
                Message(
                    role="tool",
                    content=f"The operator answered: {answer}",
                    tool_call_id=call.name,
                )
            ],
        }

    if call.name == "Extract":
        # Answered from the observation already in context — a read verb that needs no page
        # round trip, and no LLM call of its own.
        #
        # The reply has to say what is NOT knowable, or this verb becomes a trap. It used to
        # answer "Answer from the element list above.", which is not an answer when the
        # question is about a field's CONTENTS — those are deliberately never in an
        # observation, because a To field holds an address and a body holds whatever the
        # sender wrote. Asked "what is in the To field?", the model got a non-answer with no
        # hint that the question was unanswerable, so it asked again. Four times, four LLM
        # calls, on a free tier that was already returning 429s.
        return {
            "messages": [
                Message(
                    role="tool",
                    content=(
                        "The element list above is everything visible. Field CONTENTS are "
                        "never included — they carry private data — so no repeat of this "
                        "question can answer it. For compose fields the view line already "
                        "states FILLED or empty for To, Subject and Body; trust that and "
                        "act. If you need something you genuinely cannot see, use AskUser."
                    ),
                    tool_call_id=call.name,
                )
            ]
        }

    return {
        "messages": [
            Message(role="tool", content=f"{call.name} is not handled", tool_call_id=call.name)
        ]
    }
