"""Runtime configuration, read from the environment (and a gitignored `.env`).

Two standing rules this module exists to enforce:

1. **Model slugs are configuration, never code.** Free-tier model rosters rotate; a slug
   hardcoded in a node is a time bomb that fires on someone else's schedule.

2. **Provider keys are `SecretStr`.** A `Settings` object ends up in a log line or an
   exception eventually. `SecretStr` makes that a non-event instead of a leak.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

EmailSurfaceName = Literal["fake", "playwright", "extension"]
AuthMode = Literal["off", "google"]

#: `backend/.env`, anchored to THIS FILE rather than to the working directory.
#:
#: A relative `env_file=".env"` is resolved against the CWD, so the same command loaded
#: different configuration depending on where it was typed. Running `uvicorn` from the repo
#: root picked up a stale root `.env` with no `CDP_ENDPOINT`, and the backend quietly
#: launched a fresh empty Chromium instead of attaching to the signed-in browser — which
#: presents as "Gmail is asking me to sign in", a symptom that points nowhere near the cause.
#:
#: One file, one location, whatever directory you are standing in.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, extra="ignore", case_sensitive=False
    )

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

    # Per-provider overrides. Model ids are NOT portable: `openai/gpt-oss-120b` is a Groq
    # and OpenRouter slug, and sending it to Gemini earns a 404 for a model that was never
    # going to exist there. The generic slugs above only work across Groq and OpenRouter
    # because those two happen to host the same roster.
    #
    # Empty means "use the generic one", so a single-provider setup needs none of these.
    # Still no slug in code: the defaults are empty and the values live in `.env`, because
    # free rosters rotate and a baked-in id fails on someone else's schedule.
    gemini_model_classifier: str = ""
    gemini_model_executor: str = ""
    gemini_model_validator: str = ""
    groq_model_classifier: str = ""
    groq_model_executor: str = ""
    groq_model_validator: str = ""
    openrouter_model_classifier: str = ""
    openrouter_model_executor: str = ""
    openrouter_model_validator: str = ""

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
    # Token budget for the conversation history before compaction kicks in. Sized for the
    # small free-tier windows this runs on: a run that overflows at step 39 has wasted
    # everything it spent getting there.
    context_budget_tokens: int = Field(default=8_000, gt=0)
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
    # Attach to a browser that is ALREADY RUNNING and already signed in, instead of
    # launching a fresh one. Set to e.g. "http://127.0.0.1:9222".
    #
    # This exists because Google refuses its sign-in flow inside an automation-controlled
    # browser ("this browser or app may not be secure"), and no launch flag reliably changes
    # that. Attaching sidesteps the problem rather than fighting it: the human signs in
    # normally, in their own browser, and the agent joins a session that is already
    # authenticated. It is also the honest shape of the product — the agent operates the
    # user's mailbox in the user's browser.
    # ── Authentication ──────────────────────────────────────────────────────
    # "off"    — anyone who can reach the server can run. Correct for localhost, and a
    #            breach on a public URL. A loud warning fires at startup.
    # "google" — Sign in with Google, identity only (openid/email/profile). Those scopes
    #            are NOT restricted, so no Google verification and no CASA assessment.
    auth_mode: AuthMode = "off"
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    # Must match a redirect URI registered on the OAuth client, exactly.
    google_redirect_uri: str = "http://localhost:8000/auth/callback"
    # Where to send the browser after a successful sign-in.
    cockpit_url: str = "http://localhost:3000"
    # Signs session tokens, bridge tokens, and the OAuth `state`. Rotating it is the
    # revocation lever: every session and every paired browser is invalidated at once.
    auth_secret: SecretStr = SecretStr("")

    # Browsers allowed to call this API. `*` was the old default and it is not a default
    # any more: with credentials in play it is the difference between a private API and a
    # public one. Comma-separated.
    allowed_origins: str = "http://localhost:3000"

    # ── Bridge extension (EMAIL_SURFACE=extension) ──────────────────────────
    # The shared secret an extension must present on /ws/bridge. EMPTY MEANS THE ROUTE
    # REFUSES EVERYONE — an unset secret is a misconfiguration, and the failure mode of
    # guessing "they probably meant open" is somebody else's mailbox.
    #
    # One code authenticates a BROWSER, not a user. That is honest for a single-operator
    # deployment; multi-tenant needs per-user codes hung off a real identity.
    bridge_pairing_code: SecretStr = SecretStr("")
    # How long one RPC to the extension may take. Generous: the far end is a real browser
    # typing real text, and the extension enforces its own tighter per-verb walls.
    bridge_call_timeout: float = Field(default=90.0, gt=0)

    cdp_endpoint: str = ""
    # When the endpoint refuses a connection, start Chrome ourselves rather than making the
    # human remember a second terminal. Off is for CI and for anyone who wants the browser
    # under their own control.
    cdp_auto_launch: bool = True
    # The profile the auto-launcher signs into and reuses. Empty means `~/.inbox-agent-
    # profile`. Deliberately NOT the everyday profile: Chrome ignores the debugging port
    # while another instance owns the user-data-dir, which is the entire "close every Chrome
    # window" problem.
    chrome_profile_dir: str = ""
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

    def origins(self) -> list[str]:
        """The CORS allowlist, as a list."""
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]

    def auth_ready(self) -> bool:
        """Is Google sign-in actually usable, or merely switched on?

        Checked at startup rather than discovered on the first login attempt, because a
        half-configured auth mode fails in the one place a user cannot debug it.
        """
        return bool(
            self.auth_mode == "google"
            and self.google_client_id
            and self.google_client_secret.get_secret_value()
            and self.auth_secret.get_secret_value()
        )

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
