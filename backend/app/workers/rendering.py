"""How the mailbox is described to the model.

Separated from the loop because it is the one piece here with no control flow at all: a pure
function from an `Observation` to the text the model reads. It is also the piece most likely
to be tweaked — wording, ordering, what to include — and tweaking prose should not mean
opening the module that decides when to stop a run.
"""
from __future__ import annotations

from app.agent.state import AgentState


def task_block(state: AgentState) -> str:
    """The task, plus the slots intake already resolved.

    The slots are the point. Intake mints an addressable token for every address the
    operator typed (`_trust_addresses`), and without passing them on, that work is invisible
    to the worker: it reads "send email to <someone>", has no token in hand, and asks the
    human for one that was minted three nodes earlier. Naming them here closes that loop —
    "recipient_identity: P1" is the whole answer to "who am I sending this to".

    Only slots with a value are listed. An empty slot is noise the context gate has already
    dealt with, and printing `topic: ` invites the model to treat blank as an answer.
    """
    lines = [f"Task: {state.task}"]

    intent = state.intent
    if intent is not None:
        filled = {name: value for name, value in intent.slots.items() if str(value).strip()}
        if filled:
            lines.append("")
            lines.append("Already established (use these — they are resolved and valid):")
            lines.extend(f"- {name}: {value}" for name, value in filled.items())

    draft = state.draft
    if draft is not None:
        # Verbatim, and said so. These words were written by a node with a short context and
        # a prompt about writing; re-deciding them here — mid-loop, surrounded by 140 DOM
        # elements — reliably makes them worse, and the human may already have approved them.
        lines.append("")
        lines.append("The message is already written. Type it EXACTLY as it appears:")
        lines.append(f"Subject: {draft.subject}")
        lines.append("Body:")
        lines.append(draft.body)
    return "\n".join(lines)


def observation_block(state: AgentState) -> str:
    """Render the observation as the model sees it."""
    observation = state.observation
    if observation is None:
        return "No page loaded yet."

    lines = [f"## {observation.title or 'Mailbox'}"]
    if observation.mail is not None:
        detail = f"view: {observation.mail.view}"
        if observation.mail.unread_count is not None:
            detail += f" · unread: {observation.mail.unread_count}"
        lines.append(detail)
        if observation.mail.compose_open:
            # Its own line, not a clause tacked onto a detail string. Buried in
            # "view: inbox · unread: 12 · compose is open" it was reliably missed, and the
            # agent opened a second window and split one email across both.
            lines.append(
                "A COMPOSE WINDOW IS ALREADY OPEN. Write in it. Do not click Compose again."
            )
    if observation.changed:
        lines.append(f"changed: {observation.changed}")

    lines.append("")
    for element in observation.elements:
        marker = " (new)" if element.is_new else ""
        value = f" = {element.value}" if element.value else ""
        lines.append(f"[{element.index}] {element.role}: {element.name}{value}{marker}")

    if observation.dropped_count:
        # Never let the model believe it has seen everything: an agent that thinks the list
        # is complete concludes a message does not exist. The hint names the DIRECTION,
        # without which the count is not actionable.
        lines.append("")
        lines.append(
            f"({observation.hint or str(observation.dropped_count) + ' more items not shown'} "
            "Scroll to reach them.)"
        )
    return "\n".join(lines)
