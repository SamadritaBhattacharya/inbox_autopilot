"""The PII vault — the reason the model never sees a real address.

One vault per session. It holds the only mapping from token back to the real value, it
lives in the executor next to the DOM, and it is **never persisted** — not to the
checkpointer, not to the trajectory, not to a log.

Three properties carry the whole security story:

**Stable within a session.** `alice@corp.com` is `P17` for the entire run, so the model can
reason about "the same person" across turns without ever learning who that is.

**Never reused across sessions.** A fresh `thread_id` gets a fresh vault and fresh
numbering. If tokens were global and stable, they would themselves become identifiers —
`P17` meaning the same human every day is just a pseudonym, and pseudonyms correlate.

**One-way for the brain.** The backend holds no reverse map and cannot resolve `P17`. Only
the executor can, and only at the moment of dispatch.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.security.patterns import TOKEN_PREFIX, PiiKind, find_emails


class UnknownToken(KeyError):
    """A token this vault never minted.

    Raised rather than passed through. A silent passthrough would let an injected string
    like `P999` — or a literal address the model invented — reach a real action, which is
    precisely the attack the token scheme exists to stop.
    """


@runtime_checkable
class PiiVault(Protocol):
    def tokenize(self, text: str) -> str: ...
    def resolve(self, token: str) -> str: ...


class SessionPiiVault:
    """Per-session token store. In memory, and it stays there."""

    def __init__(self) -> None:
        # value -> token, and token -> value. Two dicts rather than one plus a scan: the
        # forward direction runs on every element of every observation.
        self._forward: dict[str, str] = {}
        self._reverse: dict[str, str] = {}
        self._counters: dict[PiiKind, int] = dict.fromkeys(PiiKind, 0)
        # Tokens that may be used as an ACTION TARGET.
        #
        # Every address on the page is tokenized — that is redaction, and it is unconditional.
        # But tokenizing an address is not the same as endorsing it as a recipient. An address
        # sitting in the body of a hostile email gets a token so the model never sees it in
        # the clear; it must NOT thereby become somewhere the agent can send mail.
        #
        # Addressable means the value came from somewhere the OPERATOR controls: a sender or
        # recipient chip (a person genuinely in this mailbox), or the user's own instruction.
        self._addressable: set[str] = set()

    # ── minting ─────────────────────────────────────────────────────────────

    def token_for(self, value: str, kind: PiiKind, *, addressable: bool = False) -> str:
        """The token for `value`, minting one on first sight.

        Normalised on the way in so `Alice@Corp.com` and `alice@corp.com` are one person
        rather than two — the model would otherwise reason about them as different people
        and, worse, the approval preview would show two recipients where there is one.
        """
        normalised = self._normalise(value, kind)
        if normalised in self._forward:
            token = self._forward[normalised]
            # Upgrade only, never downgrade: an address seen once in a structured position
            # is a real correspondent, whatever else it also appears inside.
            if addressable:
                self._addressable.add(token)
            return token

        self._counters[kind] += 1
        token = f"{TOKEN_PREFIX[kind]}{self._counters[kind]}"
        self._forward[normalised] = token
        # The reverse map keeps the ORIGINAL spelling: what gets typed into Gmail should be
        # what the user actually wrote, not our lowercased version.
        self._reverse[token] = value
        if addressable:
            self._addressable.add(token)
        return token

    @staticmethod
    def _normalise(value: str, kind: PiiKind) -> str:
        stripped = value.strip()
        if kind is PiiKind.EMAIL:
            return stripped.lower()
        if kind is PiiKind.PHONE:
            # Formatting is not identity: +91 98765 43210 and +919876543210 are one number.
            return "".join(c for c in stripped if c.isdigit() or c == "+")
        if kind is PiiKind.PERSON:
            return " ".join(stripped.lower().split())
        return stripped

    # ── resolution (executor-side only) ─────────────────────────────────────

    def resolve(self, token: str) -> str:
        try:
            return self._reverse[token.strip()]
        except KeyError as exc:
            raise UnknownToken(
                f"{token!r} was never minted by this session's vault. A token the model "
                "invented, or one carried over from another session, must not reach an action."
            ) from exc

    def knows(self, token: str) -> bool:
        return token.strip() in self._reverse

    def token_of(self, value: str, kind: PiiKind = PiiKind.EMAIL) -> str | None:
        """The token standing in for `value`, if this session ever minted one.

        For tests and for the approval card, never for the model.
        """
        return self._forward.get(self._normalise(value, kind))

    def is_addressable(self, token: str) -> bool:
        """May this token be used as an action TARGET?

        False for anything the vault only ever saw inside page content. That is the
        difference between "the model must not read this address" and "the agent may send
        mail here", and conflating them is what lets an injected instruction pick a
        recipient.
        """
        return token.strip() in self._addressable

    def trust(self, value: str, kind: PiiKind = PiiKind.EMAIL) -> str:
        """Mint a token the operator supplied, and mark it addressable.

        An address in the USER's own instruction is trusted input: they typed it, so it is
        somewhere they meant to write to. An address in an email body is not, however
        confidently the email asserts otherwise.
        """
        return self.token_for(value, kind, addressable=True)

    # ── introspection, for tests and the leak suite ─────────────────────────

    @property
    def size(self) -> int:
        return len(self._reverse)

    def tokens(self) -> list[str]:
        return list(self._reverse)

    def __repr__(self) -> str:
        """Never render the mapping.

        A vault ends up inside an exception or a debug log eventually; the default dataclass
        style repr would print every address it holds at exactly that moment.
        """
        return f"<SessionPiiVault {self.size} tokens>"

    __str__ = __repr__


def trust_addresses(text: str, vault: PiiVault | None) -> str:
    """Replace every operator-supplied address in `text` with an ADDRESSABLE token.

    Shared by the two places raw human text enters the system: the task at intake, and a
    mid-run correction. Both are the operator's own words, so an address in either is
    trusted input — they typed it, so it is somewhere they meant to write.

    Corrections went untokenized for a long time and it cost two things at once. The
    obvious one: a real address reached the model in the clear, straight past the vault
    that exists to stop exactly that, and was persisted in the feedback store on its way.
    The subtler one: "also add alex@corp.com" gave the model an address with no token
    behind it, so the dispatcher — which only ever accepts minted tokens — refused it. The
    user's correction could not be carried out no matter how well the model understood it.

    A `None` vault returns the text unchanged: a caller with no session (a unit test, a
    read-only path) should not crash, and there is nothing to mint against.
    """
    if vault is None:
        return text
    for address in find_emails(text):
        text = text.replace(address, vault.trust(address))
    return text
