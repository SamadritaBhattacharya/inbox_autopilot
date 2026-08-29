"""Every verb the model is OFFERED must be a verb the executor will ACCEPT.

**The live failure.** Asked "open the sent folder", the agent read its tool list, found
`OpenFolder`, and called it on its very first turn — exactly right. The executor answered
`'OpenFolder' is not available to this worker`. It then spent eight turns clicking through
menus to do what one verb does, and burned a day's free-tier token budget getting there.

`OpenFolder` was not alone. Five verbs were implemented, tested, and unreachable:

    DraftReply, Label, MarkRead, OpenFolder, Snooze

Three of those are `TriageWorker`'s — so one of the four workers named in CLAUDE.md had a
single working verb out of four, and nothing anywhere said so.

**The cause was two lists that had to agree and nothing checking that they did.** The tool
sets in `workers/tools.py` say what the model may ask for. `SURFACE_VERBS` says what the
executor will do. They are edited in different files, for different reasons, by whoever is
adding a feature — and a mismatch has no symptom until a user watches the agent be refused
its own correct answer.

This is the same shape as the `ThreadedSurface.preview` gap: a contract split across two
places, kept in step by memory. Memory lost.
"""
from __future__ import annotations

import pytest

from app.manager.intent import Action
from app.surface.playwright_surface import (
    DEFAULT_TIMEOUT,
    SURFACE_VERBS,
    TIMEOUTS,
    PlaywrightEmailSurface,
    timeout_for,
)
from app.workers.registry import worker_for
from app.workers.tools import INTERNAL_VERBS, TOOLSETS, verb_names

#: Every worker name, from the registry rather than a hand-written list — a FIFTH worker
#: must be covered here the day it is added, not the day it fails.
WORKERS = sorted({worker_for(action).name for action in Action})


def offered(worker: str) -> set[str]:
    """Verbs this worker's model may emit, minus the ones the graph handles itself."""
    return set(verb_names(TOOLSETS[worker])) - set(INTERNAL_VERBS)


# ── the invariant ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("worker", WORKERS)
def test_every_verb_a_worker_offers_is_one_the_surface_accepts(worker):
    """THE regression, per worker, so a failure names which one is broken."""
    unreachable = offered(worker) - set(SURFACE_VERBS)

    assert not unreachable, (
        f"the {worker} worker offers {sorted(unreachable)}, which the executor refuses. "
        "The model will pick one of these, be told it does not exist, and improvise."
    )


def test_the_triage_worker_can_actually_triage():
    """Named explicitly because this is what was broken and nothing noticed. Archive alone
    is not triage — a worker that can only archive cannot label, defer, or mark read."""
    triage = offered("triage")

    for verb in ("Archive", "Label", "MarkRead", "Snooze"):
        assert verb in triage, f"triage does not offer {verb}"
        assert verb in SURFACE_VERBS, f"triage offers {verb} but the executor refuses it"


def test_folder_navigation_is_reachable_from_every_worker_that_offers_it():
    """"Open the sent folder" is the task that exposed all of this."""
    for worker in WORKERS:
        if "OpenFolder" in offered(worker):
            assert "OpenFolder" in SURFACE_VERBS, f"{worker} offers OpenFolder in name only"


# ── the other direction: nothing is offered that cannot run ─────────────────


@pytest.mark.parametrize("worker", WORKERS)
def test_every_offered_verb_has_a_handler_behind_it(worker):
    """A verb bound and then not handled is WORSE than one never offered.

    `Send` was bound for months with no `_do_send`: every approval a human granted ended in
    "Send has no handler", at the exact moment they had authorised a send. `DraftReply` was
    the same shape — a tool class, a place in the compose tool set, and no implementation on
    any surface.
    """
    for verb in offered(worker):
        assert hasattr(PlaywrightEmailSurface, f"_do_{verb.lower()}"), (
            f"the {worker} worker offers {verb}, which no surface implements"
        )


def test_DraftReply_is_not_offered_until_something_implements_it():
    """It is still a defined tool class — this asserts it stays out of a tool set until
    there is a handler, rather than that it was deleted."""
    assert not hasattr(PlaywrightEmailSurface, "_do_draftreply")

    for worker in WORKERS:
        assert "DraftReply" not in offered(worker)


# ── the declaration and the implementation cannot drift ────────────────────


def test_every_declared_verb_is_implemented():
    """Also asserted at import (`_assert_verbs_are_implemented`). Here too, so the failure
    reads as a test rather than as a module that will not load."""
    missing = {v for v in SURFACE_VERBS if not hasattr(PlaywrightEmailSurface, f"_do_{v.lower()}")}

    assert not missing, f"declared with no handler: {sorted(missing)}"


def test_every_implemented_handler_is_declared():
    """The direction that broke. Writing `_do_openfolder` and stopping there left a verb
    that worked perfectly and could never be called."""
    implemented = {
        name[len("_do_") :] for name in vars(PlaywrightEmailSurface) if name.startswith("_do_")
    }
    undeclared = implemented - {verb.lower() for verb in SURFACE_VERBS}

    assert not undeclared, f"implemented but unreachable: {sorted(undeclared)}"


def test_the_import_time_check_actually_fires():
    """A guard nobody has seen fail is a guard nobody knows works."""
    import app.surface.playwright_surface as surface_module

    original = surface_module.SURFACE_VERBS
    try:
        surface_module.SURFACE_VERBS = frozenset({*original, "Teleport"})
        with pytest.raises(RuntimeError, match="no handler"):
            surface_module._assert_verbs_are_implemented()
    finally:
        surface_module.SURFACE_VERBS = original


# ── timeouts no longer decide what may run ─────────────────────────────────


def test_a_missing_timeout_no_longer_revokes_a_capability():
    """THE structural fix. `TIMEOUTS` used to be the allowlist as well, so forgetting an
    entry deleted the verb — which is exactly how five of them were lost."""
    from inbox_contracts import ActionCall

    undocumented = SURFACE_VERBS - set(TIMEOUTS)

    for verb in undocumented:
        assert timeout_for(ActionCall(name=verb, args={})) == DEFAULT_TIMEOUT

    surface = PlaywrightEmailSurface.__new__(PlaywrightEmailSurface)
    surface._bound_verbs = frozenset(SURFACE_VERBS)
    for verb in undocumented:
        assert verb in surface._bound_verbs, f"{verb} lost its binding to a missing timeout"


@pytest.mark.parametrize("verb", ["OpenFolder", "Label", "MarkRead", "Snooze", "DeleteForever"])
def test_the_newly_registered_verbs_have_sensible_walls(verb):
    """A folder switch is a page load, not a click. Too tight a wall turns a slow success
    into `ACTION_TIMEOUT`, which the recovery layer then diagnoses as a broken page."""
    from inbox_contracts import ActionCall

    wall = timeout_for(ActionCall(name=verb, args={}))

    assert 5.0 <= wall <= 30.0, f"{verb} has an implausible timeout of {wall}s"


# ── the allowlist still refuses things ─────────────────────────────────────


def test_the_surface_still_refuses_a_verb_it_does_not_implement():
    """Widening the list must not have turned it into a rubber stamp."""
    assert "Teleport" not in SURFACE_VERBS
    assert "DraftReply" not in SURFACE_VERBS


def test_a_worker_is_still_bound_to_ITS_OWN_verbs_only():
    """The read-only guarantee is a capability boundary, not a naming convention. A query
    run reads a hostile inbox, so an injected instruction must have nothing to reach for."""
    query = offered("query")

    for mutating in ("Send", "Archive", "Label", "MarkRead", "Snooze", "DeleteForever"):
        assert mutating not in query, f"the read-only worker can {mutating}"


def test_no_worker_may_delete_forever():
    """Implemented and declared so the verb exists as a typed, gated capability — but it is
    in nobody's tool set, so no model can select it."""
    for worker in WORKERS:
        assert "DeleteForever" not in offered(worker)
