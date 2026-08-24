"""The funnel — seven stages from raw DOM to a tokenized, numbered `Observation`.

    extract -> visibility -> occlusion -> wrapper_collapse -> pii_tokenize -> som -> reading_order
                                                              ^^^^^^^^^^^^^
**Stage 5's position is a security control, not a preference.** The tokenizer runs before
indexing and formatting, so no later stage — and therefore nothing that could be
serialized, logged, checkpointed, or transmitted — ever holds a raw address. Moving it
later would leave a window in which real PII exists downstream, and `STAGE_ORDER` plus its
test exist to make that reordering fail loudly rather than pass review.

The pipeline returns the `Observation` (which crosses the wire) alongside two things that
**never** do: the `index -> geometry` map, and the report of what was dropped.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from inbox_contracts import MailContext, Observation, Viewport

from app.observation.funnel.occlusion import OcclusionCuller
from app.observation.funnel.reading_order import ReadingOrderFormatter
from app.observation.funnel.som import SoMIndexer
from app.observation.funnel.visibility import VisibilityFilter
from app.observation.funnel.wrapper_collapse import WrapperCollapser
from app.observation.raw import FunnelReport, PageMeta, RawElement
from app.security.tokenizer import PiiTokenizer

logger = logging.getLogger(__name__)


def _scroll_hint(report: FunnelReport) -> str | None:
    """Where the unlisted content is, in words the model can act on.

    "12 more items" alone is not actionable — an agent scrolls one way, sees the number
    unchanged, and scrolls the same way again. Naming the direction turns a dead end into
    a decision.
    """
    total = report.reachable_but_unlisted
    if total <= 0:
        return None

    parts: list[str] = []
    if report.offscreen_above:
        parts.append(f"{report.offscreen_above} above")
    if report.offscreen_below:
        parts.append(f"{report.offscreen_below} below")
    if report.budget_dropped:
        parts.append(f"{report.budget_dropped} trimmed to fit")

    where = ", ".join(parts) if parts else "elsewhere on the page"
    return f"{total} more item{'' if total == 1 else 's'} not shown: {where}."

#: The canonical order. Asserted by test; changing it changes the security properties.
STAGE_ORDER: tuple[str, ...] = (
    "extract",
    "visibility",
    "occlusion",
    "wrapper_collapse",
    "pii_tokenize",
    "som",
    "reading_order",
)

#: Stages that must run before anything can serialize an element's text.
_TOKENIZE_BEFORE = ("som", "reading_order")

#: Roles where a name is STRUCTURED rather than prose that happens to mention one. Only
#: these teach the tokenizer a person; see `security/tokenizer.py` for why guessing at
#: names in free text is the wrong trade.
_PERSON_ROLES = frozenset({"sender", "recipient", "contact", "chip"})


def _assert_tokenizer_precedes_serialization() -> None:
    """Fails at import if the pipeline order stops protecting PII."""
    position = STAGE_ORDER.index("pii_tokenize")
    for later in _TOKENIZE_BEFORE:
        if STAGE_ORDER.index(later) < position:
            raise RuntimeError(
                f"Funnel order is unsafe: '{later}' runs before 'pii_tokenize'. "
                "Tokenization must precede any stage that serializes element text."
            )


_assert_tokenizer_precedes_serialization()


class ObservationFunnel:
    """Composes the stages. Add a capability by adding a stage, not by editing this."""

    def __init__(
        self,
        tokenizer: PiiTokenizer,
        *,
        token_budget: int | None = None,
    ) -> None:
        self._tokenizer = tokenizer
        self._visibility = VisibilityFilter()
        self._occlusion = OcclusionCuller()
        self._collapser = WrapperCollapser()
        self._indexer = SoMIndexer()
        self._formatter = (
            ReadingOrderFormatter(token_budget=token_budget)
            if token_budget is not None
            else ReadingOrderFormatter()
        )

    def run(
        self,
        elements: list[RawElement],
        meta: PageMeta,
        *,
        screenshot_ref: str | None = None,
        previous_identities: set[str] | None = None,
        changed: str | None = None,
    ) -> tuple[Observation, dict[int, tuple[float, float]], FunnelReport]:
        report = FunnelReport(extracted=len(elements), stages=list(STAGE_ORDER))

        # Learn people from the RAW element set, before any stage can prune it.
        #
        # This ran inside the tokenize stage once, and it was a latent leak: a later stage
        # (wrapper collapse folding a sender chip into its row) removed the only structured
        # occurrence of a name, so the name was never registered and every mention of it
        # downstream stayed in the clear. Registration must not depend on what survives —
        # pruning decides what the model SEES, never what the vault KNOWS.
        self._register_people(elements)

        visible, report.hidden, report.offscreen_above, report.offscreen_below = (
            self._visibility.apply(elements, meta)
        )

        viewport_area = float(meta.viewport_width * meta.viewport_height)
        unoccluded, report.occluded = self._occlusion.apply(visible, viewport_area)

        collapsed, report.collapsed = self._collapser.apply(unoccluded)

        # ── stage 5: PII leaves the data here and never comes back ──
        tokenized = self._tokenize(collapsed)

        indexed, geometry = self._indexer.apply(tokenized)

        listed, report.budget_dropped = self._formatter.apply(
            indexed,
            viewport_height=meta.viewport_height,
            previous_indices=previous_identities,
            # When a dialog is open, its fields outrank the mailbox behind it. Without this
            # the compose subject line loses a budget contest to two hundred inbox rows.
            focus_box=meta.focus_box,
        )
        report.shown = len(listed)

        # Trim the geometry map to what was actually listed. An index the model was never
        # shown must not be dispatchable — otherwise a hallucinated number could land on a
        # real element by coincidence.
        shown = {element.index for element in listed}
        geometry = {i: point for i, point in geometry.items() if i in shown}

        # Thread token FIRST, then the context id. The order is observable — the vault
        # numbers tokens sequentially — and the TypeScript funnel in `bridge-extension/`
        # must mint them the same way or the two surfaces describe the same page
        # differently. `fixtures/funnel/` is what holds them together.
        thread_token = (
            self._tokenizer.tokenize_identifier(meta.thread_ref) if meta.thread_ref else None
        )
        observation = Observation(
            context_id=self._tokenizer.tokenize_identifier(meta.context_ref),
            title=self._tokenizer.tokenize(meta.title),
            viewport=Viewport(
                width=meta.viewport_width,
                height=meta.viewport_height,
                scrollX=meta.scroll_x,
                scrollY=meta.scroll_y,
            ),
            elements=listed,
            mail=MailContext(
                view=meta.view,
                threadToken=thread_token,
                unreadCount=meta.unread_count,
                composeOpen=meta.compose_open,
                toFilled=meta.to_filled,
                subjectFilled=meta.subject_filled,
                bodyFilled=meta.body_filled,
            ),
            screenshotRef=screenshot_ref,
            changed=changed,
            droppedCount=report.reachable_but_unlisted,
            hint=_scroll_hint(report),
        )

        logger.debug(
            "funnel %d -> %d (hidden=%d offscreen=%d occluded=%d collapsed=%d budget=%d)",
            report.extracted, report.shown, report.hidden, report.offscreen,
            report.occluded, report.collapsed, report.budget_dropped,
        )
        return observation, geometry, report

    def _register_people(self, elements: list[RawElement]) -> None:
        """Teach the vault every structured name on the page, before anything is pruned.

        A whole pass of its own because a sender's display name met on row 40 must already
        be known when row 1 is tokenized — otherwise one person appears as a token in one
        row and as plain text in another, and the model reasons about them as two people.
        """
        for element in elements:
            if self._is_person_field(element):
                self._tokenizer.register_person(element.name)

    def _tokenize(self, elements: list[RawElement]) -> list[RawElement]:
        """Rewrite every name and value through the vault."""
        return [
            replace(
                element,
                # A sender or recipient chip is a real correspondent in THIS mailbox, so an
                # address there is somewhere the agent may legitimately write. An address in
                # a subject line or a message body is content a stranger controls: tokenized
                # all the same, but never a valid target.
                name=self._tokenizer.tokenize(
                    element.name, addressable=element.role in _PERSON_ROLES
                ),
                value=(
                    self._tokenizer.tokenize(
                        element.value, addressable=element.role in _PERSON_ROLES
                    )
                    if element.value
                    else element.value
                ),
            )
            for element in elements
        ]

    @staticmethod
    def _is_person_field(element: RawElement) -> bool:
        """Is this element a structured name, rather than prose that mentions one?

        Only structured positions teach the tokenizer a name — see `tokenizer.py` for why
        guessing at names in prose is the wrong trade.
        """
        return element.role in _PERSON_ROLES and bool(element.name.strip())
