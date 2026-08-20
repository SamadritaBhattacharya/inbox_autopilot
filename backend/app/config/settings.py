"""Runtime configuration, read from the environment (and a gitignored `.env`).

Two standing rules this module exists to enforce:

1. **Model slugs are configuration, never code.** Free-tier model rosters rotate; a slug
   hardcoded in a node is a time bomb that fires on someone else's schedule.

2. **Provider keys are `SecretStr`.** A `Settings` object ends up in a log line or an
   exception eventually. `SecretStr` makes that a non-event instead of a leak.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

EmailSurfaceName = Literal["fake", "playwright", "extension"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # ── LLM providers, in fallback order ────────────────────────────────────
    groq_api_key: SecretStr = SecretStr("")
    openrouter_api_key: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")

    # Model per role: a small classifier for high-volume/low-difficulty work, a large
    # executor for actual judgment, a small validator for binary checks.
    #
    # These defaults are a starting point, NOT a guarantee — free-tier rosters rotate
    # without notice, and this project has already watched a whole model family disappear
    # from a live account. Treat "model does not exist" as a config task, not an outage:
    # list what the key can reach and set LLM_MODEL_* in .env.
    llm_model_classifier: str = "openai/gpt-oss-20b"
    llm_model_executor: str = "openai/gpt-oss-120b"
    llm_model_validator: str = "openai/gpt-oss-20b"

    # ── Agent loop ──────────────────────────────────────────────────────────
    max_steps: int = Field(default=40, gt=0)
    llm_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    llm_max_retries: int = Field(default=3, ge=0)
    # A per-request cap so an occasional provider stall cannot hang a whole turn. Normal
    # calls return in a few seconds, so this only trips on a genuine hang — and the
    # client's own retry then recovers on a fresh request.
    llm_request_timeout: float = Field(default=45.0, gt=0)
    # Output cap per completion. One reasoning block plus one tool call needs far less.
    # Without a cap, a degenerate repetition loop runs to the provider limit and the blob
    # then poisons every later turn's context.
    llm_max_output_tokens: int = Field(default=2000, gt=0)
    # context_gate will not dispatch a worker below this confidence — the "100% context"
    # rule, expressed as a number the gate can actually test.
    context_confidence_threshold: float = Field(default=0.85, gt=0.0, le=1.0)

    # ── Email surface ───────────────────────────────────────────────────────
    # Default is `fake`: nothing reaches a real mailbox unless explicitly configured.
    email_surface: EmailSurfaceName = "fake"
    # Headful is the anti-detection lever (headless is what gets fingerprinted). On a
    # server this still needs a display, so the container runs under xvfb.
    cdp_headless: bool = False
    stealth: bool = True
    start_url: str = "https://mail.google.com"
    browser_locale: str = "en-IN"
    browser_timezone: str = "Asia/Kolkata"

    # ── Security ────────────────────────────────────────────────────────────
    # Addresses and phones are ALWAYS tokenized and there is no switch for that. This
    # widens coverage to best-effort personal names.
    pii_tokenize_names: bool = True
    approval_timeout_seconds: int = Field(default=600, gt=0)

    # ── Persistence ─────────────────────────────────────────────────────────
    checkpoint_dsn: str = "sqlite:///runs/checkpoints.db"
    runs_dir: str = "runs"

    def configured_providers(self) -> tuple[str, ...]:
        """Which providers actually have a key, in fallback order.

        The gateway builds its chain from this, so an unkeyed provider is skipped rather
        than attempted and failed — the difference between a clean startup and a 401 on
        the first real turn.
        """
        keyed = (
            ("groq", self.groq_api_key),
            ("openrouter", self.openrouter_api_key),
            ("gemini", self.gemini_api_key),
        )
        return tuple(name for name, key in keyed if key.get_secret_value().strip())


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings. Cached because reading env on every access is pointless.

    Tests construct `Settings(_env_file=None, ...)` directly instead of going through
    here, so a developer's local `.env` can never change a test outcome.
    """
    return Settings()
