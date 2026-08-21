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

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.config.settings import Settings, get_settings
from app.feedback.store import FeedbackStore, InMemoryFeedbackStore
from app.llm.base import LLMClient
from app.llm.providers import build_llm_client
from app.llm.usage import UsageTracker
from app.rules.store import InMemoryRulesStore, RulesStore
from app.security.redaction import install_redaction
from app.security.tokenizer import PiiTokenizer
from app.security.vault import SessionPiiVault
from app.surface.base import EmailSurface
from app.telemetry.store import InMemoryTrajectoryStore, TrajectoryStore

#: How to release a per-run resource (a browser) when the run ends.
Closer = Callable[[], Awaitable[None]]


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
    rules: RulesStore
    feedback: FeedbackStore
    surface: EmailSurface | None = None

    async def new_surface(self) -> tuple[EmailSurface | None, Closer]:
        """A browser for ONE run, plus how to close it.

        Per run, never per process — a surface is one browser session, and sharing it would
        mean two users driving the same page. Same reason the vault is per session: both are
        session-scoped by nature, and treating either as a singleton is a correctness bug
        that only shows up under concurrency.

        Returns `(None, noop)` for the fake surface, which is what keeps the whole test
        pyramid browser-free.
        """

        async def noop() -> None:
            return None

        if self.surface is not None:
            return self.surface, noop  # injected by a test or a caller; not ours to close

        if self.settings.email_surface != "playwright":
            return None, noop

        from app.surface.browser_thread import (
            BrowserLoop,
            ThreadedSurface,
            loop_can_spawn_subprocesses,
        )
        from app.surface.playwright_surface import connect_surface, launch_surface

        security = self.new_session_security()
        kwargs = {"vault": security.vault, "tokenizer": security.tokenizer}

        if self.settings.cdp_endpoint:
            # Attach to the user's own signed-in browser.
            async def build():
                return await connect_surface(
                    endpoint=self.settings.cdp_endpoint,
                    start_url=self.settings.start_url,
                    auto_launch=self.settings.cdp_auto_launch,
                    profile_dir=self.settings.chrome_profile_dir or None,
                    **kwargs,
                )
        else:
            async def build():
                return await launch_surface(
                    headless=self.settings.cdp_headless,
                    start_url=self.settings.start_url,
                    **kwargs,
                )

        # The server does not get to choose its own event loop, and on Windows uvicorn
        # hands us one that cannot spawn the browser process. Rather than require a
        # particular start command, give the browser a loop of its own when it needs one.
        if loop_can_spawn_subprocesses():
            return await build()

        browser_loop = BrowserLoop()
        surface, close_inner = await browser_loop.call(build())

        async def close() -> None:
            await browser_loop.call(close_inner())
            await browser_loop.shutdown()

        return ThreadedSurface(surface, browser_loop), close

    def build_graph(
        self,
        *,
        emitter=None,
        feedback: FeedbackStore | None = None,
        surface: EmailSurface | None = None,
        vault=None,
    ):
        """Compile a graph from the wired ports.

        Built per run rather than once: a graph closes over a surface, and a surface is one
        browser session. Sharing one across runs would mean two users driving the same page.
        """
        from app.agent.graph import build_manager_graph

        return build_manager_graph(
            llm=self.require_llm(),
            surface=surface if surface is not None else self.surface,
            emitter=emitter,
            vault=vault,
            rules=self.rules,
            feedback=feedback or self.feedback,
            threshold=self.settings.context_confidence_threshold,
            max_steps=self.settings.max_steps,
            context_budget=self.settings.context_budget_tokens,
            approval_timeout_seconds=self.settings.approval_timeout_seconds,
        )

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
    rules: RulesStore | None = None,
    feedback: FeedbackStore | None = None,
    surface: EmailSurface | None = None,
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
        rules=rules or InMemoryRulesStore(),
        feedback=feedback or InMemoryFeedbackStore(),
        surface=surface,
    )
