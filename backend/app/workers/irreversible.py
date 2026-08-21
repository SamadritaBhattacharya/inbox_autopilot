"""Which actions are irreversible — by what they DO, not by what they are called.

**The hole this closes.** Gating used to be a set of verb names: `Send`, `DeleteForever`,
`SendInvite`. But the compose worker is also bound to `Click`, and Gmail's Send button is an
ordinary element with an index. `Click(index=108)` on that button sends the email, is not in
the verb set, and therefore dispatched with no approval at all. The strongest guarantee in
the system — "nothing leaves your mailbox without you clicking approve" — was one plausible
tool call away from being false, and the model reaches for that call naturally: it had just
written "Then click Send."

So the question is asked of the TARGET, not the verb. A click lands on an element with an
accessible name, and a control named "Send (Ctrl-Enter)" does the same irreversible thing
whichever verb was used to press it.

Deliberately matched on the accessible name rather than a Gmail-specific selector: the name
is what the funnel already carries, it is what a human reads off the screen, and it survives
Gmail redesigns that a CSS path would not. False positives are cheap — an extra approval
card — and false negatives are the entire failure this module exists to prevent.
"""
from __future__ import annotations

import re

from inbox_contracts import ActionCall, Observation

#: Verbs whose name alone settles it.
GATED_VERBS = frozenset({"Send", "DeleteForever", "SendInvite"})

#: Verbs that press whatever is under them, and so inherit their target's consequences.
TARGETING_VERBS = frozenset({"Click", "PressKey"})

#: Controls that dispatch mail or destroy it. Anchored so "Sender", "Resend later" and
#: "Send feedback" do not trip it, while "Send", "Send (Ctrl-Enter)" and "Send & Archive" do.
IRREVERSIBLE_NAMES = re.compile(
    r"^\s*(send\b(?!\s*(feedback|later|to\s+yourself))"
    r"|delete\s+forever"
    r"|empty\s+(trash|spam)"
    r"|delete\s+all)",
    re.IGNORECASE,
)

#: Keystrokes that submit a compose window. Ctrl+Enter sends in Gmail, with no button
#: involved and therefore no element name to inspect.
SENDING_KEYS = re.compile(r"^(control|ctrl|meta|cmd)\+enter$", re.IGNORECASE)


def target_name(observation: Observation | None, index: object) -> str:
    """The accessible name of the element an action points at, if it can be found."""
    if observation is None or not isinstance(index, int) or isinstance(index, bool):
        return ""
    for element in observation.elements:
        if element.index == index:
            return element.name or ""
    return ""


def is_irreversible(call: ActionCall | None, observation: Observation | None = None) -> bool:
    """Would dispatching this be impossible to undo?

    `observation` is optional so callers that genuinely do not have one still get the
    verb-level answer. Passing it is what closes the click path, so every caller that can
    should.
    """
    if call is None:
        return False
    if call.name in GATED_VERBS:
        return True
    if call.name not in TARGETING_VERBS:
        return False

    if call.name == "PressKey" and SENDING_KEYS.match(str(call.args.get("key", ""))):
        return True

    name = target_name(observation, call.args.get("index"))
    return bool(name and IRREVERSIBLE_NAMES.match(name))
