"""The composition root — the ONLY place concrete implementations are constructed.

Everything else in the backend depends on abstractions and receives them by injection.
Graph nodes are closures over ports; services take ports in `__init__`. If you find
yourself importing a concrete adapter anywhere outside this module, the dependency
inversion has broken and the fix belongs here, not at the call site.

**How this grows.** Each milestone adds one field to `AppContainer` and one matching
keyword override to `build_container`:

    M1  llm: LLMClient          surface: EmailSurface       vault: PiiVault
    M3  events: EventSink       trajectory: TrajectoryStore
    M4  approver: Approver
    M5  rules: RulesStore       skills: SkillRegistry

The override parameter is not a convenience — it is how the whole test pyramid stays
browser-free and network-free. A dependency with no override is a dependency that will
eventually force a real browser into a unit test.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import Settings, get_settings


@dataclass(frozen=True)
class AppContainer:
    """The wired application.

    Frozen on purpose: run state belongs in `AgentState`, never on the container. If
    something here needs to change during a run, it is state and it is in the wrong place.
    """

    settings: Settings


def build_container(*, settings: Settings | None = None) -> AppContainer:
    """Wire the application. No logic, no branching on environment beyond selection.

    Building must never perform I/O: no network, no browser launch, no key validation.
    That keeps import-and-build cheap enough to do in every test.
    """
    return AppContainer(settings=settings or get_settings())
