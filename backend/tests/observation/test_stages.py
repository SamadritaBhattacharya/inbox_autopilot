"""Each funnel stage in isolation, against synthetic snapshots."""
from __future__ import annotations

from app.observation.funnel.occlusion import OcclusionCuller
from app.observation.funnel.reading_order import ReadingOrderFormatter, identity_set
from app.observation.funnel.som import SoMIndexer
from app.observation.funnel.visibility import VisibilityFilter
from app.observation.funnel.wrapper_collapse import WrapperCollapser
from tests.observation.conftest import VIEWPORT_H, VIEWPORT_W, element, meta

VIEWPORT_AREA = float(VIEWPORT_W * VIEWPORT_H)


# ── stage 2: visibility ─────────────────────────────────────────────────────


def test_undisplayed_elements_are_hidden_not_offscreen():
    kept, hidden, above, below = VisibilityFilter().apply(
        [element(1, displayed=False), element(2, name="real")], meta()
    )
    assert [e.node_id for e in kept] == [2]
    assert (hidden, above, below) == (1, 0, 0)


def test_zero_size_elements_are_dropped():
    kept, hidden, _, _ = VisibilityFilter().apply([element(1, width=0, height=0)], meta())
    assert kept == []
    assert hidden == 1


def test_offscreen_is_counted_separately_because_the_agent_can_scroll_there():
    """Conflating the two would send the agent scrolling after content that isn't there."""
    kept, hidden, above, below = VisibilityFilter().apply(
        [element(1, y=VIEWPORT_H + 500), element(2, y=100)], meta()
    )
    assert [e.node_id for e in kept] == [2]
    assert (hidden, above, below) == (0, 0, 1)


def test_an_element_peeking_into_the_viewport_survives():
    """Otherwise the list flickers as the page settles by a pixel."""
    kept, _, _, _ = VisibilityFilter().apply([element(1, y=VIEWPORT_H - 2, height=40)], meta())
    assert len(kept) == 1


def test_content_scrolled_past_is_reported_as_ABOVE():
    """Direction is what makes the count actionable — without it the agent scrolls the
    wrong way, sees the number unchanged, and scrolls the wrong way again."""
    kept, _, above, below = VisibilityFilter().apply([element(1, y=-300, height=40)], meta())
    assert kept == []
    assert (above, below) == (1, 0)


def test_content_not_yet_reached_is_reported_as_BELOW():
    _, _, above, below = VisibilityFilter().apply(
        [element(1, y=VIEWPORT_H + 300, height=40)], meta()
    )
    assert (above, below) == (0, 1)


# ── stage 3: occlusion ──────────────────────────────────────────────────────


def test_content_behind_a_modal_is_culled():
    """The modal becomes the salient thing — the same perception a human has."""
    row = element(1, x=100, y=100, width=400, height=40, paint_order=1)
    modal = element(2, x=50, y=50, width=600, height=300, paint_order=10)

    kept, occluded = OcclusionCuller().apply([row, modal], VIEWPORT_AREA)

    assert [e.node_id for e in kept] == [2]
    assert occluded == 1


def test_partial_overlap_is_not_occlusion():
    """Shadows and borders overlap constantly; dropping on that would gut the list."""
    row = element(1, x=0, y=0, width=400, height=100, paint_order=1)
    strip = element(2, x=0, y=0, width=400, height=10, paint_order=9)

    kept, occluded = OcclusionCuller().apply([row, strip], VIEWPORT_AREA)

    assert len(kept) == 2
    assert occluded == 0


def test_a_full_page_backdrop_does_not_blank_the_observation():
    """A scrim covering everything is the worst possible false positive for this stage."""
    scrim = element(99, x=0, y=0, width=VIEWPORT_W, height=VIEWPORT_H, paint_order=5)
    dialog = element(2, x=300, y=200, width=400, height=200, paint_order=10)
    row = element(1, x=0, y=600, width=400, height=40, paint_order=1)

    kept, _ = OcclusionCuller().apply([row, scrim, dialog], VIEWPORT_AREA)

    assert 1 in [e.node_id for e in kept], "the scrim must not cull the whole page"


def test_a_parent_does_not_occlude_its_own_child():
    parent = element(1, x=0, y=0, width=200, height=50, paint_order=1)
    child = element(2, x=0, y=0, width=200, height=50, paint_order=2, parent_id=1)

    kept, occluded = OcclusionCuller().apply([parent, child], VIEWPORT_AREA)

    assert occluded == 0
    assert len(kept) == 2


def test_original_ordering_is_preserved():
    """Reading order is decided by the stage that knows about reading order."""
    items = [element(i, y=i * 50, paint_order=i) for i in range(1, 5)]
    kept, _ = OcclusionCuller().apply(items, VIEWPORT_AREA)
    assert [e.node_id for e in kept] == [1, 2, 3, 4]


def test_single_element_is_never_occluded():
    kept, occluded = OcclusionCuller().apply([element(1)], VIEWPORT_AREA)
    assert (len(kept), occluded) == (1, 0)


# The browser's hit-test beats geometry whenever we have it — geometry cannot tell a
# transparent full-page scrim from a real cover, and that is the case that matters.


def test_the_browsers_hit_test_is_believed_over_geometry():
    """A blocked element is culled even when nothing geometrically covers it."""
    blocked = element(1, x=0, y=0, width=400, height=40, receives_pointer=False)
    reachable = element(2, x=0, y=600, width=400, height=40, receives_pointer=True)

    kept, occluded = OcclusionCuller().apply([blocked, reachable], VIEWPORT_AREA)

    assert [e.node_id for e in kept] == [2]
    assert occluded == 1


def test_a_hit_tested_element_survives_a_geometric_false_positive():
    """The browser said the click lands here, so a covering box is irrelevant."""
    covered = element(1, x=0, y=0, width=100, height=100, paint_order=1, receives_pointer=True)
    cover = element(2, x=0, y=0, width=200, height=200, paint_order=9, receives_pointer=True)

    _, occluded = OcclusionCuller().apply([covered, cover], VIEWPORT_AREA)
    assert occluded == 0


def test_geometry_is_used_when_the_element_could_not_be_hit_tested():
    """Off-screen centres cannot be asked, so the fallback still has to work."""
    row = element(1, x=100, y=100, width=400, height=40, paint_order=1, receives_pointer=None)
    modal = element(2, x=50, y=50, width=600, height=300, paint_order=10, receives_pointer=None)

    _, occluded = OcclusionCuller().apply([row, modal], VIEWPORT_AREA)
    assert occluded == 1


# ── stage 4: wrapper collapse ───────────────────────────────────────────────


def test_a_layout_wrapper_folds_into_its_child():
    wrapper = element(1, role="generic", x=0, y=0, width=100, height=40)
    button = element(2, role="button", name="Compose", x=0, y=0, width=100, height=40,
                     interactive=True, parent_id=1)

    kept, collapsed = WrapperCollapser().apply([wrapper, button])

    assert [e.node_id for e in kept] == [2], "the CHILD survives: behaviour beats layout"
    assert collapsed == 1


def test_an_interactive_parent_is_never_a_wrapper():
    """The handler may be on the parent and the child may be an inert span."""
    parent = element(1, role="button", interactive=True, width=100, height=40)
    child = element(2, role="generic", width=100, height=40, parent_id=1)

    _, collapsed = WrapperCollapser().apply([parent, child])
    assert collapsed == 0


def test_a_container_with_several_children_survives():
    """It gives its children meaning by grouping them."""
    row = element(1, width=400, height=40)
    children = [element(i, width=100, height=40, parent_id=1) for i in (2, 3, 4)]

    kept, collapsed = WrapperCollapser().apply([row, *children])

    assert collapsed == 0
    assert len(kept) == 4


def test_a_parent_noticeably_bigger_than_its_child_survives():
    """A row containing a button is doing something; it is not pure layout."""
    row = element(1, width=400, height=100)
    button = element(2, width=80, height=30, interactive=True, parent_id=1)

    _, collapsed = WrapperCollapser().apply([row, button])
    assert collapsed == 0


def test_a_parent_with_its_own_text_survives():
    parent = element(1, name="Unread from Priya", width=100, height=40)
    child = element(2, name="", width=100, height=40, parent_id=1)

    _, collapsed = WrapperCollapser().apply([parent, child])
    assert collapsed == 0


# ── stage 6: Set-of-Marks ───────────────────────────────────────────────────


def test_indices_follow_reading_order():
    items = [
        element(1, name="third", x=10, y=200, interactive=True),
        element(2, name="first", x=10, y=10, interactive=True),
        element(3, name="second", x=400, y=12, interactive=True),
    ]

    indexed, _ = SoMIndexer().apply(items)

    assert [e.name for e in indexed] == ["first", "second", "third"]
    assert [e.index for e in indexed] == [1, 2, 3]


def test_same_row_elements_sort_left_to_right():
    """Baselines differ by a few pixels; strict y-sorting would interleave columns."""
    items = [
        element(1, name="right", x=500, y=104, interactive=True),
        element(2, name="left", x=20, y=100, interactive=True),
    ]
    indexed, _ = SoMIndexer().apply(items)
    assert [e.name for e in indexed] == ["left", "right"]


def test_geometry_stays_out_of_the_element_and_in_the_map():
    """This indirection is the safety property: the model gets a number, not a point."""
    indexed, geometry = SoMIndexer().apply(
        [element(1, name="Compose", x=100, y=200, width=80, height=40, interactive=True)]
    )
    assert geometry[1] == (140.0, 220.0)
    assert not hasattr(indexed[0], "center_x")


def test_elements_with_nothing_to_do_or_read_are_not_indexed():
    """An index promises there is something there; empty numbers teach the model noise."""
    indexed, _ = SoMIndexer().apply([element(1, name="", interactive=False)])
    assert indexed == []


def test_non_interactive_text_is_still_indexed():
    indexed, _ = SoMIndexer().apply([element(1, name="Friday demo", interactive=False)])
    assert len(indexed) == 1


# ── stage 7: reading order + budget ─────────────────────────────────────────


def _indexed(count: int, **kwargs):
    items = [element(i, name=f"item {i}" * 20, y=i * 10, **kwargs) for i in range(1, count + 1)]
    return SoMIndexer().apply(items)[0]


def test_budget_drops_are_reported_never_silent():
    """An agent that thinks it saw everything concludes a message does not exist."""
    listed, dropped = ReadingOrderFormatter(token_budget=40).apply(
        _indexed(40), viewport_height=VIEWPORT_H
    )
    assert dropped > 0
    assert len(listed) < 40


def test_interactive_elements_above_the_fold_survive_the_cut():
    """That is the task surface; cutting it makes the page unusable."""
    button = element(1, name="Compose", y=10, interactive=True)
    filler = [element(i, name="x" * 400, y=VIEWPORT_H - 10) for i in range(2, 60)]
    indexed, _ = SoMIndexer().apply([button, *filler])

    listed, dropped = ReadingOrderFormatter(token_budget=60).apply(
        indexed, viewport_height=VIEWPORT_H
    )

    assert any(e.name == "Compose" for e in listed)
    assert dropped > 0


def test_output_is_in_reading_order_not_priority_order():
    """The model reads the list as a picture of the page."""
    listed, _ = ReadingOrderFormatter().apply(_indexed(5), viewport_height=VIEWPORT_H)
    assert [e.index for e in listed] == sorted(e.index for e in listed)


def test_long_values_are_clipped_not_dropped():
    """The agent needs to know the field HAS content, not re-read it every turn."""
    indexed, _ = SoMIndexer().apply([element(1, name="Body", value="x" * 5000, interactive=True)])
    listed, _ = ReadingOrderFormatter().apply(indexed, viewport_height=VIEWPORT_H)

    assert listed[0].value is not None
    assert len(listed[0].value) < 200
    assert listed[0].value.endswith("…")


def test_whitespace_is_normalised():
    indexed, _ = SoMIndexer().apply([element(1, name="  Friday\n\n   demo  ", interactive=True)])
    listed, _ = ReadingOrderFormatter().apply(indexed, viewport_height=VIEWPORT_H)
    assert listed[0].name == "Friday demo"


def test_new_elements_are_flagged_against_the_previous_turn():
    """`isNew` is what makes 'a dialog just appeared' legible at a glance."""
    first, _ = ReadingOrderFormatter().apply(
        SoMIndexer().apply([element(1, name="Compose", role="button", interactive=True)])[0],
        viewport_height=VIEWPORT_H,
    )
    previous = identity_set(first)

    second, _ = ReadingOrderFormatter().apply(
        SoMIndexer().apply([
            element(1, name="Compose", role="button", interactive=True),
            element(2, name="Send", role="button", y=50, interactive=True),
        ])[0],
        viewport_height=VIEWPORT_H,
        previous_indices=previous,
    )

    flags = {e.name: e.is_new for e in second}
    assert flags == {"Compose": False, "Send": True}


def test_identity_is_role_and_name_not_index():
    """Indices are rebuilt every turn; an index-based diff marks the page new constantly."""
    listed, _ = ReadingOrderFormatter().apply(
        SoMIndexer().apply([element(1, name="Compose", role="button", interactive=True)])[0],
        viewport_height=VIEWPORT_H,
    )
    assert identity_set(listed) == {"button:Compose"}


# ── region-of-interest scoping (B3): a focus box hard-caps what is OUTSIDE it ──


def _inbox_and_compose(count: int = 140):
    """A realistically-sized inbox behind a small compose dialog, at the DEFAULT budget.

    Deliberately sized to what a real Gmail inbox looks like — not the artificially tight
    budgets (`token_budget=40`, `=300`) the other tests here use to force a cut. At the
    default 2000-token budget, 140 short rows cost well under 1500 tokens on their own,
    which is precisely how the compose subject field was trimmed in production while
    "priority, not exclusion" left plenty of room for everything behind it.
    """
    rows = [
        element(
            i,
            role="listitem",
            name=f"Person {i} — Re: a fairly typical subject line about something {i}",
            y=100 + i * 12,
            interactive=True,
        )
        for i in range(1, count + 1)
    ]
    fields = [
        element(900 + n, role=role, name=name, x=820.0, y=y, interactive=True)
        for n, (role, name, y) in enumerate(
            [
                ("textbox", "To", 500.0),
                ("textbox", "Subject", 540.0),
                ("textbox", "Message Body", 580.0),
                ("button", "Send", 720.0),
            ]
        )
    ]
    indexed, _ = SoMIndexer().apply([*rows, *fields])
    return indexed


_COMPOSE_FOCUS = (810.0, 480.0, 340.0, 300.0)


def test_a_focus_box_hard_caps_whats_outside_it_even_at_the_default_budget():
    """The actual regression: priority alone left ~99 of 140 background rows visible at
    the default budget, because the dialog's own fields cost so little that most of the
    budget was never spent. A separate, small allowance for everything outside the box is
    what turns "wins a tie-break" into "is mostly absent" — see B3 in
    docs/IMPROVEMENT-PLAN.md.
    """
    listed, dropped = ReadingOrderFormatter().apply(
        _inbox_and_compose(), viewport_height=VIEWPORT_H, focus_box=_COMPOSE_FOCUS
    )

    compose_names = {"To", "Subject", "Message Body", "Send"}
    survived = [e for e in listed if e.name not in compose_names]

    assert compose_names <= {e.name for e in listed}, "the dialog itself must stay intact"
    assert len(survived) < 20, f"{len(survived)} background rows still visible while composing"
    assert dropped > 100


def test_without_a_focus_box_the_default_budget_is_unaffected():
    """No dialog open -> ordinary browsing and triage must see exactly what they always
    did. The cap is scoped to `focus_box is not None`, not to element count or budget."""
    without_dialog = [e for e in _inbox_and_compose() if e.name not in {
        "To", "Subject", "Message Body", "Send"
    }]

    listed, _ = ReadingOrderFormatter().apply(
        without_dialog, viewport_height=VIEWPORT_H, focus_box=None
    )

    assert len(listed) > 100, "background rows must not be capped with no focus box active"


def test_the_outside_cap_never_touches_elements_inside_the_box():
    """A large focus region containing many elements must not have ITS OWN contents capped
    by the outside allowance — only what is genuinely outside the box is limited."""
    many_fields = [
        element(900 + n, role="textbox", name=f"field {n}", x=820.0, y=480.0 + n * 20,
                interactive=True)
        for n in range(30)
    ]
    indexed, _ = SoMIndexer().apply(many_fields)

    listed, dropped = ReadingOrderFormatter().apply(
        indexed, viewport_height=VIEWPORT_H, focus_box=_COMPOSE_FOCUS
    )

    assert dropped == 0
    assert len(listed) == 30
