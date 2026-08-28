"""How the mailbox is described to the model.

Separated from the loop because it is the one piece here with no control flow at all: a pure
function from an `Observation` to the text the model reads. It is also the piece most likely
to be tweaked — wording, ordering, what to include — and tweaking prose should not mean
opening the module that decides when to stop a run.
"""
from __future__ import annotations

from app.agent.state import AgentState
from app.manager.intent import TaskIntent
from app.manager.slots import (
    CONDITIONAL_SLOTS,
    resolved_delivery_mode,
    split_recipients,
)


def _delivery_instruction(intent: TaskIntent | None) -> str | None:
    """How to handle more than one recipient, decided BEFORE the worker ever sees the task.

    This is the point of resolving `delivery_mode` in `context_gate` rather than leaving it
    for the ReAct loop: the worker is never asked to decide together-vs-separate, only to
    carry out a decision that already has an unambiguous, concrete shape. See
    `docs/IMPROVEMENT-PLAN.md` §B2.
    """
    if intent is None:
        return None
    if not any(c.slot == "delivery_mode" for c in CONDITIONAL_SLOTS.get(intent.action, ())):
        return None
    people = split_recipients(intent.slots.get("recipient_identity", ""))
    if len(people) <= 1:
        return None

    if resolved_delivery_mode(intent) == "separate":
        steps = "\n".join(f"  {i}. {person}" for i, person in enumerate(people, 1))
        return (
            f"This goes to {len(people)} people SEPARATELY — {len(people)} separate "
            "emails, never one email with more than one of them in it. Send them one at a "
            f"time, in this order:\n{steps}\n"
            "Compose, fill, and Send ONE of these completely — including its own approval "
            "— before opening the next. Reuse the same subject and body for each; only the "
            "recipient changes."
        )
    return (
        f"This goes to {len(people)} people TOGETHER — ONE email, all of them in the To "
        f"field at once: {', '.join(people)}. Do not open more than one compose window."
    )


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
        filled = {
            name: value
            for name, value in intent.slots.items()
            # `delivery_mode` is raw free text ("separately please") — the worker gets the
            # RESOLVED instruction below instead, not the human's unparsed answer to parse
            # a second time itself.
            if str(value).strip() and name != "delivery_mode"
        }
        if filled:
            lines.append("")
            lines.append("Already established (use these — they are resolved and valid):")
            lines.extend(f"- {name}: {value}" for name, value in filled.items())

        instruction = _delivery_instruction(intent)
        if instruction:
            lines.append("")
            lines.append(instruction)

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


def _field_state(label: str, filled: bool, index: int | None) -> str:
    """One compose field as `Subject: empty [61]`.

    The index is omitted rather than faked when the field did not survive the funnel —
    an index the model was never shown is refused at dispatch, so inventing one would send
    it at a number that cannot work and teach it the numbers are unreliable.
    """
    state = "FILLED" if filled else "empty"
    # "(not on screen)" was actively harmful: it told the agent to scroll for a field that
    # was on screen the whole time, just unmatched by our selector. It scrolled six times,
    # the page never changed, and the stuck guard killed the run. An unknown location is a
    # gap in OUR knowledge, not a fact about the page — so say that, and point at the one
    # place the answer definitely is.
    where = f" [{index}]" if index is not None else " (find it in the list below)"
    return f"{label}: {state}{where}"


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
            # What is already done, so it stops guessing. A committed recipient becomes a
            # chip and the input reads empty, so the agent typed the address a second time
            # on top of the first — visible in the compose window as a chip AND loose text.
            # State AND location, together. Reporting only the state left the agent to
            # re-find each field by name every turn, against a list that renumbers every
            # turn — an open autocomplete dropdown alone shifts every index. Observed live:
            # it read "Subject at [60]", acted, saw [60] had become something else,
            # concluded its own action had failed, and spent four turns hunting. The number
            # comes from this same observation, so it cannot be stale.
            mail = observation.mail
            done = [
                _field_state("To", mail.to_filled, mail.to_index),
                _field_state("Subject", mail.subject_filled, mail.subject_index),
                _field_state("Body", mail.body_filled, mail.body_index),
            ]
            lines.append("  " + " · ".join(done))
            lines.append(
                "  Type into the [N] given here — do not search the list for these fields. "
                "Fill only the empty ones; a FILLED field is done."
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
