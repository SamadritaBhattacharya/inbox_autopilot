"""How the mailbox is described to the model.

Separated from the loop because it is the one piece here with no control flow at all: a pure
function from an `Observation` to the text the model reads. It is also the piece most likely
to be tweaked — wording, ordering, what to include — and tweaking prose should not mean
opening the module that decides when to stop a run.
"""
from __future__ import annotations

from app.agent.state import AgentState


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
        if observation.mail.compose_open:
            detail += " · compose is open"
        lines.append(detail)
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
