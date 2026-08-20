"""The composition root — the ONLY place concrete implementations are constructed.

Everything else in the backend depends on abstractions and receives them by injection.
Graph nodes are closures over ports; services take ports in `__init__`. If you find
yourself importing a concrete adapter anywhere outside this module, dependency inversion
has broken and the fix belongs here, not at the call site.

**How this grows.** Each milestone adds one field to `AppContainer` and one matching
keyword override to `build_container`:

    M1  llm · trajectory · usage · per-session security   ← done
    M3  events: EventSink       surface: EmailSurface
    M4  approver: Approver
    M5  rules: RulesStore       skills: SkillRegistry

The override parameter is not a convenience — it is how the whole test pyramid stays
browser-free and network-free. A dependency with no override is a dependency that will
eventually force a real browser into a unit test.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import Settings, get_settings
from app.llm.base import LLMClient
from app.llm.providers import build_llm_client
from app.llm.usage import UsageTracker
from app.security.redaction import install_redaction
from app.security.tokenizer import PiiTokenizer
from app.security.vault import SessionPiiVault
from app.telemetry.store import InMemoryTrajectoryStore, TrajectoryStore


@dataclass(frozen=True)
class SessionSecurity:
    """One session's vault and the tokenizer writing into it.

    Built **per session, never per process**. A shared vault would make tokens stable
    across runs, and a token that means the same human every day is a pseudonym — which is
    an identifier wearing a hat. `new_session_security()` is the only way to get one.
    """

    vault: SessionPiiVault
    tokenizer: PiiTokenizer


@dataclass(frozen=True)
class AppContainer:
    """The wired application.

    Frozen on purpose: run state belongs in `AgentState`, never on the container. If
    something here needs to change during a run, it is state and it is in the wrong place.
    """

    settings: Settings
    trajectory: TrajectoryStore
    usage: UsageTracker
    llm: LLMClient | None

    def require_llm(self) -> LLMClient:
        """The LLM, or a clear explanation of why there isn't one.

        Building the container stays inert so `/health` and the whole unit suite need no
        key. This is the wiring-time check for anything that will actually reason — it
        fires before a run starts rather than three nodes deep, where the cause would be
        an opaque 401 from a provider nobody configured.
        """
        if self.llm is None:
            raise RuntimeError(
                "No LLM provider is configured. Set GROQ_API_KEY, OPENROUTER_API_KEY, or "
                "GEMINI_API_KEY in .env (server-side only), then restart."
            )
        return self.llm

    def new_session_security(self) -> SessionSecurity:
        """A fresh vault + tokenizer for one `thread_id`."""
        vault = SessionPiiVault()
        return SessionSecurity(
            vault=vault,
            tokenizer=PiiTokenizer(vault, tokenize_names=self.settings.pii_tokenize_names),
        )


def build_container(
    *,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    trajectory: TrajectoryStore | None = None,
    usage: UsageTracker | None = None,
) -> AppContainer:
    """Wire the application. No logic, no branching beyond selection.

    Building must never perform I/O: no network, no browser launch, no key validation.
    That keeps import-and-build cheap enough to do in every test.
    """
    settings = settings or get_settings()

    # Installed here because this is the one place guaranteed to run before anything logs.
    # Opt-in redaction protects the loggers someone remembered; this protects the rest.
    install_redaction()

    usage = usage or UsageTracker()

    if llm is None and settings.configured_providers():
        # Metering is pushed from the chain rather than polled, so it is wired at
        # construction — there is no later point where every attempt is still visible.
        llm = build_llm_client(settings, on_attempt=usage.record)

    return AppContainer(
        settings=settings,
        trajectory=trajectory or InMemoryTrajectoryStore(),
        usage=usage,
        llm=llm,
    )
