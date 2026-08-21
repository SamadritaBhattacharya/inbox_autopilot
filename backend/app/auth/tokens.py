"""Session tokens — who is on the other end of a socket.

Signed with HMAC-SHA256 using a server secret, so the token is self-contained and there is
no session table to keep, replicate, or lose on restart. The payload is *not* secret — it
holds a user id and an expiry, both of which the holder already knows — so it is signed, not
encrypted.

**Why not a library.** This needs exactly two operations over a dict of three fields.
`hmac` and `base64` are in the standard library, the comparison is `compare_digest`, and the
whole thing is auditable in one screen. A dependency here would be more code to trust, not
less.

Rotating `AUTH_SECRET` invalidates every session at once, which is the revocation story: no
list to maintain, and the emergency lever is one environment variable.
"""
from __future__ import annotations

import base64
import hmac
import json
import time
from dataclasses import dataclass
from hashlib import sha256

#: Eight hours. Long enough that a working session is not interrupted, short enough that a
#: token copied off a shared machine is not a standing key.
DEFAULT_TTL_SECONDS = 8 * 60 * 60

#: A bridge token lives far longer than a login, and deliberately. MV3 suspends service
#: workers constantly, so an extension re-authenticates many times a day; making it re-pair
#: whenever a login lapsed would make it unusable. Revocation is `AUTH_SECRET` rotation.
BRIDGE_TTL_SECONDS = 90 * 24 * 60 * 60

#: Distinguishes the two token kinds. Without it a session token would be accepted as a
#: bridge token and vice versa — same signature, same shape, wildly different authority.
SESSION_AUDIENCE = "session"
BRIDGE_AUDIENCE = "bridge"


class InvalidToken(Exception):
    """Malformed, tampered with, or expired.

    Deliberately one exception for all three. Telling a caller *which* is a free hint for
    anyone probing, and the correct response — sign in again — is identical.
    """


@dataclass(frozen=True)
class Session:
    """A verified identity. Never constructed from unverified input."""

    user_id: str
    email: str
    expires_at: int
    audience: str = SESSION_AUDIENCE

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at


def _b64(raw: bytes) -> str:
    # URL-safe and unpadded: the token travels in a query string on the WebSocket handshake,
    # where `+`, `/`, and `=` all need escaping that something eventually forgets.
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mint(
    user_id: str,
    email: str,
    secret: str,
    *,
    ttl: int = DEFAULT_TTL_SECONDS,
    audience: str = SESSION_AUDIENCE,
) -> str:
    """A signed token for this user."""
    if not secret:
        raise ValueError("AUTH_SECRET is empty; refusing to mint an unsigned session token")

    payload = {
        "sub": user_id,
        "email": email,
        "exp": int(time.time()) + ttl,
        "aud": audience,
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = _b64(hmac.new(secret.encode(), body.encode(), sha256).digest())
    return f"{body}.{signature}"


def mint_bridge(user_id: str, email: str, secret: str) -> str:
    """The long-lived token an extension uses after it has paired once."""
    return mint(user_id, email, secret, ttl=BRIDGE_TTL_SECONDS, audience=BRIDGE_AUDIENCE)


def verify(token: str, secret: str, *, audience: str = SESSION_AUDIENCE) -> Session:
    """The session this token proves, or `InvalidToken`.

    `audience` is checked, not merely recorded: a session token and a bridge token are
    signed the same way and carry the same fields, so without this a cockpit login would be
    a valid credential for driving somebody's mailbox.
    """
    if not secret:
        raise InvalidToken("no signing secret is configured")

    try:
        body, signature = token.strip().split(".", 1)
    except ValueError as exc:
        raise InvalidToken("malformed token") from exc

    expected = _b64(hmac.new(secret.encode(), body.encode(), sha256).digest())
    # Constant time: `==` on a signature leaks its prefix through timing, which is enough to
    # forge one a byte at a time.
    if not hmac.compare_digest(signature, expected):
        raise InvalidToken("signature does not match")

    try:
        payload = json.loads(_unb64(body))
        session = Session(
            user_id=str(payload["sub"]),
            email=str(payload["email"]),
            expires_at=int(payload["exp"]),
            audience=str(payload.get("aud") or SESSION_AUDIENCE),
        )
    except Exception as exc:
        # Reached only when the signature checked out, so this is our own bug or a rotated
        # payload shape — never an attacker, who cannot get this far.
        raise InvalidToken("token payload is unreadable") from exc

    if session.audience != audience:
        raise InvalidToken("this token is not valid for that purpose")
    if session.expired:
        raise InvalidToken("session has expired")
    return session
