"""Stage 7 — serialise survivors, within a hard token budget.

The last stage turns indexed elements into the `Element` contract objects that cross the
wire, and enforces the budget that keeps an observation at ~1–3k tokens instead of 100k.

**Nothing is ever truncated silently.** When the budget bites, the lowest-value elements go
first and the count comes back with the result so it can be reported to the model as "N
more items — scroll to see them". An agent that believes it has seen everything will
confidently conclude a message does not exist; an agent told there are 18 more will scroll.
That difference is the whole reason this stage returns a number instead of just a list.

Priority when cutting, worst first:
  1. non-interactive text that is far down the page  (read it after scrolling)
  2. non-interactive text near the top               (context, not action)
  3. interactive elements far down                   (actionable, but not yet)
  4. interactive elements near the top               (never cut — this is the task surface)
"""
from __future__ import annotations

from inbox_contracts import Element

from app.observation.raw import RawElement

#: ~1–3k tokens for the element list. The rest of the window belongs to instructions,
#: history, and the model's own reasoning.
DEFAULT_TOKEN_BUDGET = 2000

#: Long values (a whole email body in a textarea) are clipped rather than dropped: the
#: agent needs to know the field HAS content, not to re-read all of it every turn.
MAX_TEXT_LENGTH = 160


def estimate_tokens(text: str) -> int:
    """Chars-per-token approximation.

    Deliberately a cheap estimate, not a tokenizer call. The budget only needs to be
    approximately right, and running a real tokenizer over every element on every turn
    would cost more than the tokens it saves.
    """
    return max(1, len(text) // 4)


def _clip(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= MAX_TEXT_LENGTH:
        return text
    return text[: MAX_TEXT_LENGTH - 1].rstrip() + "…"


Box = tuple[float, float, float, float]


def _inside(element: RawElement, box: Box | None) -> bool:
    """Is this element's centre within the focused region?

    Centre rather than full containment: a wide field inside a narrow dialog still belongs
    to it, and requiring total overlap would exclude exactly the inputs that matter.
    """
    if box is None:
        return False
    x, y, width, height = box
    cx = element.x + element.width / 2
    cy = element.y + element.height / 2
    return x <= cx <= x + width and y <= cy <= y + height


def _priority(element: RawElement, fold: float, focus: Box | None = None) -> int:
    """Lower is cut first.

    **Anything inside an open dialog outranks everything else**, and that is the whole point
    of the focus box. When a compose window is open its fields are the only things the agent
    can act on — but there are half a dozen of them against two hundred inbox rows behind,
    and the rows won on volume. The subject field was trimmed away before the model ever saw
    it, and the agent spent five turns scrolling a page that does not move, looking for a
    field that was never in the list.
    """
    if _inside(element, focus):
        return 5 if element.interactive else 4
    above_fold = element.y < fold
    if element.interactive:
        return 3 if above_fold else 2
    return 1 if above_fold else 0


class ReadingOrderFormatter:
    """Serialises indexed elements to the wire contract, within budget."""

    def __init__(self, *, token_budget: int = DEFAULT_TOKEN_BUDGET) -> None:
        self._budget = token_budget

    def apply(
        self,
        elements: list[RawElement],
        *,
        viewport_height: int,
        previous_indices: set[str] | None = None,
        focus_box: Box | None = None,
    ) -> tuple[list[Element], int]:
        """Returns `(elements, budget_dropped_count)`.

        `previous_indices` holds last turn's element identities (role + name), used only to
        mark what is NEW. The model gets a diff *and* a fresh list — never a blind dump —
        and `isNew` is what makes "a dialog just appeared" legible in one glance.
        """
        previous = previous_indices or set()
        fold = viewport_height * 0.75

        # Cut candidates by value, but emit in reading order: the model reads the list as a
        # picture of the page, so the order it arrives in has to match the page.
        by_value = sorted(
            elements,
            key=lambda e: (_priority(e, fold, focus_box), -(e.index or 0)),
            reverse=True,
        )

        kept: list[RawElement] = []
        spent = 0
        dropped = 0

        for element in by_value:
            cost = estimate_tokens(f"[{element.index}] {element.role} {element.name}")
            if spent + cost > self._budget:
                dropped += 1
                continue
            kept.append(element)
            spent += cost

        kept.sort(key=lambda e: e.index or 0)

        return [
            Element(
                index=element.index or 0,
                role=element.role,
                name=_clip(element.name),
                value=_clip(element.value) if element.value else None,
                is_new=f"{element.role}:{element.name}" not in previous,
            )
            for element in kept
        ], dropped


def identity_set(elements: list[Element]) -> set[str]:
    """Identities for the next turn's `isNew` comparison.

    Keyed on role+name rather than index, because indices are rebuilt every turn — an
    index-based comparison would mark half the page as new every time anything moved.
    """
    return {f"{element.role}:{element.name}" for element in elements}
