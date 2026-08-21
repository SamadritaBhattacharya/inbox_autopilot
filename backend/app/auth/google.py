"""Sign in with Google — the authorization-code flow, for identity only.

**Identity only, and that is the whole point.** The scopes are `openid email profile`:
non-restricted, so this needs no Google verification and no CASA assessment. It answers
"who is this person" and nothing else. It grants no access to their mail — the agent reaches
Gmail through the user's own browser, which is a completely separate mechanism, and mixing
the two would drag the entire project into restricted-scope review for a login button.

**Why the code flow rather than a JS credential.** The client secret stays server-side, the
browser never handles a token it could leak, and the only thing that crosses back to the
cockpit is our own short-lived session token. It also means the frontend needs no Google
SDK — one redirect and one callback.

The ID token arrives directly from Google's token endpoint over TLS, so its signature is
already implicitly trusted; it is verified anyway. That check is cheap, and "the transport
vouched for it" stops being true the first time someone puts a proxy in front of this.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import time
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")

#: Identity, and deliberately nothing more. Adding a `gmail.*` scope here would move this
#: application into Google's restricted-scope review — weeks and a security assessment — for
#: no capability the agent actually uses.
SCOPES = ("openid", "email", "profile")

#: How long a login may take between leaving and coming back. Long enough for a password
#: manager and a 2FA prompt; short enough that a captured link is not a standing invitation.
STATE_TTL_SECONDS = 10 * 60


class GoogleAuthError(Exception):
    """The sign-in did not complete. Safe to show a user."""


@dataclass(frozen=True)
class GoogleIdentity:
    """Who Google says this is."""

    user_id: str  # the `sub` claim: stable per user per client, and never reused
    email: str
    name: str
    picture: str


# ── CSRF state ──────────────────────────────────────────────────────────────


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_state(secret: str, *, next_url: str = "/") -> str:
    """A signed, expiring `state` parameter.

    Signed rather than stored: this has to survive a restart mid-login, and a server-side
    state table is one more thing to lose. It carries where to return to, so the callback
    does not need to guess.
    """
    payload = {"exp": int(time.time()) + STATE_TTL_SECONDS, "next": next_url}
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = _b64(hmac.new(secret.encode(), body.encode(), sha256).digest())
    return f"{body}.{signature}"


def read_state(state: str, secret: str) -> str:
    """The `next` URL this state carries, if it is genuinely ours and still fresh.

    An unverified `state` is not a CSRF defence, it is decoration: without this check anyone
    can hand a victim a callback URL and log them into an attacker's account.
    """
    try:
        body, signature = state.split(".", 1)
    except ValueError as exc:
        raise GoogleAuthError("that sign-in link is malformed") from exc

    expected = _b64(hmac.new(secret.encode(), body.encode(), sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise GoogleAuthError("that sign-in link did not come from here")

    payload = json.loads(_unb64(body))
    if time.time() >= int(payload["exp"]):
        raise GoogleAuthError("that sign-in link has expired — try again")
    return str(payload.get("next") or "/")


# ── the flow ────────────────────────────────────────────────────────────────


def authorization_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """Where to send the browser to sign in."""
    return f"{AUTH_ENDPOINT}?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "state": state,
            # Refresh tokens are pointless here: we want an identity once, not ongoing
            # access. Asking for offline access would request a durable grant we would then
            # have to store and protect for no reason.
            "access_type": "online",
            "prompt": "select_account",
        }
    )


async def exchange_code(
    code: str,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    http: httpx.AsyncClient | None = None,
) -> str:
    """Trade the one-time code for an ID token. Returns the raw JWT."""
    client = http or httpx.AsyncClient(timeout=15.0)
    try:
        response = await client.post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    except httpx.HTTPError as exc:
        raise GoogleAuthError(f"could not reach Google to complete sign-in: {exc}") from exc
    finally:
        if http is None:
            await client.aclose()

    if response.status_code != 200:
        # Google's body names the real problem (`redirect_uri_mismatch` above all), and it
        # contains no secret. Passing it through turns an afternoon of guessing into a fix.
        detail = _error_detail(response)
        raise GoogleAuthError(f"Google refused the sign-in ({response.status_code}): {detail}")

    id_token = response.json().get("id_token")
    if not id_token:
        raise GoogleAuthError("Google's response carried no identity token")
    return str(id_token)


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    return str(body.get("error_description") or body.get("error") or body)[:200]


class GoogleVerifier:
    """Verifies ID tokens, caching Google's signing keys.

    `PyJWKClient` caches internally, so the JWKS is fetched once rather than on every login.
    One instance is built at startup; building one per request would turn each sign-in into
    an extra network round trip.
    """

    def __init__(self, client_id: str, *, jwks_uri: str = JWKS_URI) -> None:
        self._client_id = client_id
        self._jwks = PyJWKClient(jwks_uri, cache_keys=True)

    def verify(self, id_token: str) -> GoogleIdentity:
        try:
            key = self._jwks.get_signing_key_from_jwt(id_token).key
            claims = jwt.decode(
                id_token,
                key,
                algorithms=["RS256"],
                audience=self._client_id,
                issuer=ISSUERS,
                # Every one of these matters. Without `audience`, an ID token minted for a
                # DIFFERENT Google app is accepted here — the classic confused-deputy
                # sign-in bug, and it is silent.
                options={"require": ["exp", "iat", "aud", "iss", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise GoogleAuthError(f"that identity token is not valid: {exc}") from exc

        if not claims.get("email_verified", False):
            # An unverified address is an address somebody else may still own.
            raise GoogleAuthError("that Google account has no verified email address")

        return GoogleIdentity(
            user_id=str(claims["sub"]),
            email=str(claims.get("email") or ""),
            name=str(claims.get("name") or ""),
            picture=str(claims.get("picture") or ""),
        )
