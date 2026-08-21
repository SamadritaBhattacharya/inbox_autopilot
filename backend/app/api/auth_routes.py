"""Sign-in, session, and pairing — HTTP only.

Transport, like every other module in `api/`. The flow itself lives in `app/auth/`, so what
happens here is: take a request, call one function, turn the answer into a response.

**Three endpoints, and what each is for.**

- `GET  /auth/login`    — start the Google redirect
- `GET  /auth/callback` — finish it, mint a session, hand the browser back to the cockpit
- `POST /auth/pairing`  — a signed-in user asks for a code to type into their extension

`/auth/me` exists too, so the cockpit can ask "am I signed in?" without guessing from a 401
on something else.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.auth.google import (
    GoogleAuthError,
    GoogleVerifier,
    authorization_url,
    exchange_code,
    make_state,
    read_state,
)
from app.auth.pairing import PairingCodes
from app.auth.tokens import InvalidToken, Session, mint, verify
from app.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

#: Short-lived `code -> user`. Process-level, and correct as such: a code that expires in ten
#: minutes has nothing worth surviving a restart, and the durable half of pairing is a signed
#: token that needs no store at all.
PAIRING = PairingCodes()

#: Built once — `PyJWKClient` caches Google's signing keys, and rebuilding it per request
#: would add a network round trip to every sign-in.
_verifier: GoogleVerifier | None = None


def verifier_for(settings: Settings) -> GoogleVerifier:
    global _verifier
    if _verifier is None or _verifier._client_id != settings.google_client_id:  # noqa: SLF001
        _verifier = GoogleVerifier(settings.google_client_id)
    return _verifier


def _require_configured(settings: Settings) -> None:
    if settings.auth_mode != "google":
        raise HTTPException(404, "sign-in is not enabled on this server")
    if not settings.auth_ready():
        # A half-configured auth mode otherwise fails at Google with a message about a
        # client id, which sends people to the wrong console page.
        raise HTTPException(
            500,
            "sign-in is enabled but not configured. Set GOOGLE_CLIENT_ID, "
            "GOOGLE_CLIENT_SECRET, and AUTH_SECRET.",
        )


def session_from(request: Request, settings: Settings) -> Session | None:
    """The caller's session, from either the header or a query parameter.

    Two places because two transports: HTTP sends `Authorization: Bearer`, and a browser
    WebSocket cannot set headers at all, so the socket handshake carries `?token=`.
    """
    header = request.headers.get("authorization", "")
    raw = header[7:].strip() if header.lower().startswith("bearer ") else ""
    raw = raw or request.query_params.get("token", "")
    if not raw:
        return None
    try:
        return verify(raw, settings.auth_secret.get_secret_value())
    except InvalidToken:
        return None


def require_session(request: Request, settings: Settings) -> Session:
    """The caller's session, or 401.

    With `auth_mode="off"` this returns a single local identity rather than raising. That is
    what keeps a localhost setup usable without a Google project, and it is why the startup
    banner is loud about the mode.
    """
    if settings.auth_mode == "off":
        return Session(user_id="local", email="local@localhost", expires_at=2**31)
    session = session_from(request, settings)
    if session is None:
        raise HTTPException(401, "sign in to continue")
    return session


@router.get("/login")
async def login(request: Request, next: str = "/") -> RedirectResponse:
    """Start the Google redirect."""
    settings = get_settings()
    _require_configured(settings)

    state = make_state(settings.auth_secret.get_secret_value(), next_url=next)
    return RedirectResponse(
        authorization_url(
            client_id=settings.google_client_id,
            redirect_uri=settings.google_redirect_uri,
            state=state,
        )
    )


@router.get("/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Finish the redirect and hand the browser back to the cockpit.

    The session token goes back in the URL fragment (`#token=`), not the query string.
    A fragment is never sent to a server, never logged by one, and never lands in a Referer
    header — which a query parameter does, on the very next request the page makes.
    """
    settings = get_settings()
    _require_configured(settings)

    if error:
        raise HTTPException(400, f"Google reported: {error}")
    if not code:
        raise HTTPException(400, "that callback carried no authorization code")

    secret = settings.auth_secret.get_secret_value()
    try:
        next_url = read_state(state, secret)
        id_token = await exchange_code(
            code,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret.get_secret_value(),
            redirect_uri=settings.google_redirect_uri,
        )
        identity = verifier_for(settings).verify(id_token)
    except GoogleAuthError as exc:
        raise HTTPException(400, str(exc)) from exc

    logger.info("signed in: %s", identity.email)
    token = mint(identity.user_id, identity.email, secret)
    return RedirectResponse(f"{settings.cockpit_url.rstrip('/')}{next_url}#token={token}")


@router.get("/me")
async def me(request: Request) -> dict:
    """Who the cockpit is talking as. Never 401s — 'nobody' is a real answer."""
    settings = get_settings()
    if settings.auth_mode == "off":
        return {"authenticated": True, "mode": "off", "email": "local", "userId": "local"}

    session = session_from(request, settings)
    if session is None:
        return {"authenticated": False, "mode": "google", "loginUrl": "/auth/login"}
    return {
        "authenticated": True,
        "mode": "google",
        "email": session.email,
        "userId": session.user_id,
    }


@router.post("/pairing")
async def pairing(request: Request) -> dict:
    """A fresh code to type into the extension.

    Issuing replaces any code this user had outstanding: someone who clicks twice because
    the first code went missing must not leave a second valid code behind them.
    """
    settings = get_settings()
    session = require_session(request, settings)
    code = PAIRING.issue(session.user_id, session.email)
    logger.info("pairing code issued for %s", session.email)
    return {"code": code, "expiresInSeconds": int(PAIRING._ttl)}  # noqa: SLF001
