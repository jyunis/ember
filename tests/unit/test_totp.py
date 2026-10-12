"""RFC 6238 TOTP primitive.

Covers the properties the confirmation gate depends on: a code verifies
only inside its window, only once, and a malformed secret fails loudly at
construction rather than quietly at verify time.
"""

from __future__ import annotations

import pytest

from ember.totp import TotpError, TotpVerifier, _code_for_counter, _decode_secret

# 16-char base32, the shape authenticator enrollment strings take.
SECRET = "JBSWY3DPEHPK3PXP"
PERIOD = 30


def _code_at(secret: str, at: float) -> str:
    return _code_for_counter(_decode_secret(secret), int(at // PERIOD), 6)


class TestRfcVectors:
    def test_matches_known_hotp_vector(self) -> None:
        """RFC 4226 appendix D vector for the ASCII secret '12345678901234567890'
        (base32 GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ), counter 0."""
        key = _decode_secret("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        assert _code_for_counter(key, 0, 6) == "755224"
        assert _code_for_counter(key, 1, 6) == "287082"
        assert _code_for_counter(key, 9, 6) == "520489"

    def test_code_is_six_digits(self) -> None:
        code = _code_at(SECRET, 1_700_000_000)
        assert len(code) == 6 and code.isdigit()


class TestVerify:
    def test_current_code_verifies(self) -> None:
        now = 1_700_000_000.0
        v = TotpVerifier(SECRET)
        assert v.verify(_code_at(SECRET, now), at=now) is True

    def test_wrong_code_rejected(self) -> None:
        now = 1_700_000_000.0
        v = TotpVerifier(SECRET)
        assert v.verify("000000", at=now) is False

    def test_adjacent_steps_accepted_for_clock_skew(self) -> None:
        now = 1_700_000_000.0
        for offset in (-PERIOD, PERIOD):
            v = TotpVerifier(SECRET)
            assert v.verify(_code_at(SECRET, now + offset), at=now) is True

    def test_code_outside_window_rejected(self) -> None:
        now = 1_700_000_000.0
        v = TotpVerifier(SECRET)
        # Two steps back is outside the ±1 window.
        assert v.verify(_code_at(SECRET, now - 2 * PERIOD), at=now) is False

    def test_spacing_tolerated(self) -> None:
        """Apps render codes as `418 902`; a paste of that must work."""
        now = 1_700_000_000.0
        code = _code_at(SECRET, now)
        v = TotpVerifier(SECRET)
        assert v.verify(f"{code[:3]} {code[3:]}", at=now) is True

    @pytest.mark.parametrize("bad", ["", "abcdef", "12345", "1234567", "41890a"])
    def test_malformed_codes_rejected(self, bad: str) -> None:
        v = TotpVerifier(SECRET)
        assert v.verify(bad, at=1_700_000_000.0) is False


class TestReplay:
    def test_same_code_cannot_be_redeemed_twice(self) -> None:
        """The whole point: a code read out of channel scrollback is dead
        the moment it has been used, even inside its validity window."""
        now = 1_700_000_000.0
        code = _code_at(SECRET, now)
        v = TotpVerifier(SECRET)
        assert v.verify(code, at=now) is True
        assert v.verify(code, at=now) is False
        # Still refused later in the same step.
        assert v.verify(code, at=now + 5) is False

    def test_distinct_codes_both_redeem(self) -> None:
        now = 1_700_000_000.0
        v = TotpVerifier(SECRET)
        assert v.verify(_code_at(SECRET, now), at=now) is True
        later = now + PERIOD
        assert v.verify(_code_at(SECRET, later), at=later) is True

    def test_used_counters_are_pruned(self) -> None:
        """Redeemed counters age out once they can no longer verify, so the
        set doesn't grow without bound in a long-lived process."""
        now = 1_700_000_000.0
        v = TotpVerifier(SECRET)
        v.verify(_code_at(SECRET, now), at=now)
        assert v._used
        v.verify("000000", at=now + 100 * PERIOD)
        assert not v._used


class TestSecretValidation:
    @pytest.mark.parametrize("bad", ["", "   ", "not!base32", "1"])
    def test_malformed_secret_raises_at_construction(self, bad: str) -> None:
        with pytest.raises(TotpError):
            TotpVerifier(bad)

    def test_lowercase_and_spacing_accepted(self) -> None:
        now = 1_700_000_000.0
        v = TotpVerifier("jbswy3dp ehpk3pxp")
        assert v.verify(_code_at(SECRET, now), at=now) is True
