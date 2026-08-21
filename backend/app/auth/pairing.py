"""Pairing — binding one browser extension to one signed-in person.

The standard device-pairing shape, and it is standard because the alternatives are worse:

    cockpit (signed in) --> short code --> human retypes it --> extension
    extension --> code --> backend --> durable bridge token --> extension

**A short code, alive for minutes.** Ten characters is retypeable and, over a ten-minute
window, unguessable. Because the window is minutes, holding codes in memory is *correct*
rather than a compromise — there is nothing here worth surviving a restart.

**A durable token, from then on.** The extension trades its code for a signed bridge token
and never uses the code again. That token is self-contained, so a backend restart does not
unpair anybody: the crucial property, because MV3 suspends service workers constantly and an
extension that had to be re-paired every reconnect would be unusable.

**Why this replaced one shared secret.** With a single code for everyone, every extension
registered under the same owner, and a second user's browser *replaced* the first's in the
registry — so user A's next run would have driven user B's mailbox. Not a degraded
experience: a data breach.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

#: Long enough that guessing inside the TTL is hopeless, short enough to retype without
#: resentment. The alphabet omits look-alikes: this is read off one screen and typed into
#: another. `I`, `O`, `0` and `1` are out for the obvious reason, and so is `L`: in a
#: sans-serif font it is indistinguishable from `1` and from a capital `I`, which is the
#: confusion people actually hit rather than the one they expect.
CODE_LENGTH = 10
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

#: How long a code is worth typing. Short on purpose — it is a hand-off, not a credential.
DEFAULT_TTL_SECONDS = 10 * 60


class NotPaired(Exception):
    """The code presented is unknown, already used, or expired.

    One exception for all three: distinguishing them is a free hint to anyone probing, and
    the correct response — get a fresh code — is the same either way.
    """


def new_code() -> str:
    """A fresh code from the system CSPRNG.

    `secrets`, never `random`: a code minted from a predictable generator is not a code, and
    the module that gets this wrong always looks exactly like this one.
    """
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def normalise(code: str) -> str:
    """What the user typed, as the code they meant.

    People paste with spaces, add hyphens, and type in lower case. Rejecting a correct code
    over presentation is friction that gets blamed on the product.
    """
    return "".join(c for c in code.upper() if c in ALPHABET)


@dataclass
class _Pending:
    user_id: str
    email: str
    expires_at: float


class PairingCodes:
    """Short-lived `code -> user`, held in memory.

    In-memory is the right call *because* of the TTL: a code that stops being valid in ten
    minutes has nothing worth persisting. The durable half of pairing is the bridge token,
    which is signed and stateless.
    """

    def __init__(self, *, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._codes: dict[str, _Pending] = {}

    def issue(self, user_id: str, email: str) -> str:
        """A fresh code for this user, replacing any they had outstanding.

        Replacing matters: a user who clicks "pair" twice because the first code went
        missing must not leave a second valid code lying around behind them.
        """
        self._sweep()
        self._codes = {c: p for c, p in self._codes.items() if p.user_id != user_id}

        code = new_code()
        self._codes[code] = _Pending(user_id, email, time.monotonic() + self._ttl)
        return code

    def redeem(self, code: str) -> tuple[str, str]:
        """Consume a code, returning `(user_id, email)`.

        **Single use.** A code that still works after it has been redeemed is a code that
        can pair a second browser to somebody else's mailbox.
        """
        self._sweep()
        pending = self._codes.pop(normalise(code), None)
        if pending is None:
            raise NotPaired("that pairing code is not valid — generate a fresh one")
        return pending.user_id, pending.email

    def _sweep(self) -> None:
        now = time.monotonic()
        self._codes = {c: p for c, p in self._codes.items() if p.expires_at > now}

    @property
    def outstanding(self) -> int:
        self._sweep()
        return len(self._codes)
