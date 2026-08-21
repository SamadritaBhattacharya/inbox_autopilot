"""Session tokens, pairing codes, and the OAuth state parameter.

Everything here is a refusal test. These three primitives are the whole of "who is allowed
to drive whose mailbox", and each one fails silently when it fails — a forged token that is
accepted looks exactly like a real one.
"""
from __future__ import annotations

import time

import pytest

from app.auth.google import GoogleAuthError, authorization_url, make_state, read_state
from app.auth.pairing import ALPHABET, CODE_LENGTH, NotPaired, PairingCodes, normalise
from app.auth.tokens import (
    BRIDGE_AUDIENCE,
    InvalidToken,
    Session,
    mint,
    mint_bridge,
    verify,
)

SECRET = "a-long-server-side-signing-secret"


# ── session tokens ──────────────────────────────────────────────────────────


class TestSessionTokens:
    def test_a_minted_token_verifies_back_to_its_user(self):
        token = mint("user-1", "priya@corp.com", SECRET)

        session = verify(token, SECRET)

        assert session.user_id == "user-1"
        assert session.email == "priya@corp.com"

    def test_a_token_signed_with_another_secret_is_refused(self):
        # Rotating AUTH_SECRET is the revocation lever, and this is what makes it one.
        forged = mint("user-1", "priya@corp.com", "some-other-secret")

        with pytest.raises(InvalidToken):
            verify(forged, SECRET)

    def test_a_tampered_payload_is_refused(self):
        """Changing the user id without re-signing is the obvious attack: the payload is
        base64, not encrypted, so anyone can read and edit it."""
        body, signature = mint("user-1", "a@b.com", SECRET).split(".", 1)
        other = mint("user-2", "b@c.com", SECRET).split(".", 1)[0]

        with pytest.raises(InvalidToken):
            verify(f"{other}.{signature}", SECRET)

    @pytest.mark.parametrize("token", ["", "nonsense", "a.b.c", "onlyonepart", "."])
    def test_malformed_tokens_are_refused(self, token):
        with pytest.raises(InvalidToken):
            verify(token, SECRET)

    def test_an_expired_token_is_refused(self):
        with pytest.raises(InvalidToken, match="expired"):
            verify(mint("user-1", "a@b.com", SECRET, ttl=-1), SECRET)

    def test_no_secret_means_no_token(self):
        """Minting unsigned would be worse than failing: it produces something that LOOKS
        like a credential."""
        with pytest.raises(ValueError, match="AUTH_SECRET"):
            mint("user-1", "a@b.com", "")

    def test_verification_without_a_secret_refuses_everything(self):
        with pytest.raises(InvalidToken):
            verify(mint("user-1", "a@b.com", SECRET), "")

    def test_a_session_token_is_not_a_bridge_token(self):
        """Same signature, same shape, wildly different authority: one watches a run, the
        other drives a mailbox."""
        session = mint("user-1", "a@b.com", SECRET)

        with pytest.raises(InvalidToken):
            verify(session, SECRET, audience=BRIDGE_AUDIENCE)

    def test_a_bridge_token_is_not_a_session_token(self):
        bridge = mint_bridge("user-1", "a@b.com", SECRET)

        with pytest.raises(InvalidToken):
            verify(bridge, SECRET)

    def test_a_bridge_token_outlives_a_login(self):
        # MV3 suspends service workers constantly; re-pairing on every reconnect would make
        # the extension unusable.
        session = verify(mint("u", "a@b.com", SECRET), SECRET)
        bridge = verify(mint_bridge("u", "a@b.com", SECRET), SECRET, audience=BRIDGE_AUDIENCE)

        assert bridge.expires_at > session.expires_at

    def test_expiry_is_read_from_the_token_not_the_clock_at_mint(self):
        assert Session("u", "a@b.com", int(time.time()) - 1).expired is True
        assert Session("u", "a@b.com", int(time.time()) + 60).expired is False


# ── pairing codes ───────────────────────────────────────────────────────────


class TestPairingCodes:
    def test_a_code_redeems_to_the_user_who_asked_for_it(self):
        codes = PairingCodes()
        code = codes.issue("user-1", "priya@corp.com")

        assert codes.redeem(code) == ("user-1", "priya@corp.com")

    def test_a_code_is_single_use(self):
        """A code that still works after redemption can pair a second browser to the same
        mailbox."""
        codes = PairingCodes()
        code = codes.issue("user-1", "a@b.com")
        codes.redeem(code)

        with pytest.raises(NotPaired):
            codes.redeem(code)

    def test_issuing_again_invalidates_the_previous_code(self):
        """Someone who clicks 'pair' twice must not leave a second valid code behind."""
        codes = PairingCodes()
        first = codes.issue("user-1", "a@b.com")
        codes.issue("user-1", "a@b.com")

        with pytest.raises(NotPaired):
            codes.redeem(first)

    def test_an_expired_code_is_refused(self):
        codes = PairingCodes(ttl_seconds=-1)
        code = codes.issue("user-1", "a@b.com")

        with pytest.raises(NotPaired):
            codes.redeem(code)

    def test_an_unknown_code_is_refused(self):
        with pytest.raises(NotPaired):
            PairingCodes().redeem("ABCDEFGHJK")

    def test_two_users_get_different_codes(self):
        codes = PairingCodes()

        assert codes.issue("user-A", "a@b.com") != codes.issue("user-B", "b@c.com")

    def test_codes_avoid_look_alike_characters(self):
        """It is read off one screen and typed into another; 0/O confusion turns a working
        setup into a support question."""
        code = PairingCodes().issue("u", "a@b.com")

        assert len(code) == CODE_LENGTH
        assert set(code) <= set(ALPHABET)
        assert not (set("O0I1L") & set(ALPHABET))

    @pytest.mark.parametrize(
        "typed", ["abcd-efgh-jk", "ABCD EFGH JK", "  abcdefghjk  ", "abcd_efgh_jk"]
    )
    def test_presentation_does_not_break_a_correct_code(self, typed):
        # Rejecting a correct code over spacing is friction that gets blamed on the product.
        assert normalise(typed) == "ABCDEFGHJK"

    def test_codes_are_not_predictable(self):
        """Minted from `secrets`, never `random`. A predictable code is not a code."""
        codes = PairingCodes()
        minted = {codes.issue(f"u{i}", "a@b.com") for i in range(200)}

        assert len(minted) == 200


# ── OAuth state ─────────────────────────────────────────────────────────────


class TestOAuthState:
    def test_state_round_trips_the_return_url(self):
        assert read_state(make_state(SECRET, next_url="/run/abc"), SECRET) == "/run/abc"

    def test_a_forged_state_is_refused(self):
        """Without this anyone can hand a victim a callback URL and sign them into an
        attacker's account."""
        with pytest.raises(GoogleAuthError):
            read_state(make_state("other-secret"), SECRET)

    def test_an_expired_state_is_refused(self):
        import app.auth.google as google

        state = make_state(SECRET)
        original = google.time.time
        try:
            google.time.time = lambda: original() + google.STATE_TTL_SECONDS + 1
            with pytest.raises(GoogleAuthError, match="expired"):
                read_state(state, SECRET)
        finally:
            google.time.time = original

    def test_the_consent_url_asks_for_identity_only(self):
        """A `gmail.*` scope here would drag this into Google's restricted-scope review —
        weeks and a security assessment — for a login button."""
        url = authorization_url(
            client_id="cid", redirect_uri="http://localhost:8000/auth/callback", state="S"
        )

        assert "scope=openid+email+profile" in url
        assert "gmail" not in url
        assert "response_type=code" in url
