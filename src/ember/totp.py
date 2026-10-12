"""RFC 6238 TOTP verification (stdlib only).

Second factor for confirmation-gated tools. The in-band `y` reply that
redeems a `requires_confirmation` prompt travels over the same Discord
conversation that proposed it, so it authenticates nothing an attacker
holding that conversation can't also produce. A rotating code carried on
a separate device does.

Deliberately dependency-free: the algorithm is ~20 lines of `hmac`, and a
vendored implementation that guards trading is easier to audit than a
pinned wheel. Compatible with any standard authenticator app (6 digits,
30s period, SHA-1 — the defaults every app assumes).

Replay protection lives in `TotpVerifier`, which remembers the counters it
has already redeemed. That state is per-process: a restart forgets it, so
a code could in principle be replayed across one. Redeeming it would mean
inducing a restart inside the same 30s step, which is not a threat the
in-band gate was defending against anyway.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time

_DIGITS = 6
_PERIOD_S = 30
# Accept the adjacent steps either side of now, so a code typed as it
# rolls over still verifies (RFC 6238 §5.2 clock-skew allowance).
_WINDOW = 1


class TotpError(ValueError):
    """Malformed secret. Raised at construction, never during verify."""


def _decode_secret(secret: str) -> bytes:
    """Decode a base32 secret, tolerating the spacing/casing that
    authenticator apps display and the missing `=` padding most enrollment
    strings omit."""
    cleaned = secret.strip().replace(" ", "").replace("-", "").upper()
    if not cleaned:
        raise TotpError("TOTP secret is empty")
    cleaned += "=" * (-len(cleaned) % 8)
    try:
        raw = base64.b32decode(cleaned, casefold=True)
    except Exception as e:  # binascii.Error and friends
        raise TotpError("TOTP secret is not valid base32") from e
    if not raw:
        raise TotpError("TOTP secret decoded to zero bytes")
    return raw


def _code_for_counter(key: bytes, counter: int, digits: int) -> str:
    """The HOTP truncation of RFC 4226 §5.3."""
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    truncated = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**digits)).zfill(digits)


def _normalize(code: str) -> str:
    """Strip the spacing apps like to render (`418 902`)."""
    return "".join(ch for ch in (code or "") if not ch.isspace())


class TotpVerifier:
    """Verifies rotating codes for one secret, refusing replays.

    Construction raises `TotpError` on a malformed secret so misconfiguration
    surfaces at startup rather than at the moment someone is trying to
    confirm a trade.
    """

    def __init__(
        self,
        secret: str,
        *,
        digits: int = _DIGITS,
        period_s: int = _PERIOD_S,
        window: int = _WINDOW,
    ) -> None:
        self._key = _decode_secret(secret)
        self._digits = digits
        self._period_s = period_s
        self._window = window
        # Counters already redeemed, so a code observed in the channel
        # can't be replayed inside its own validity window.
        self._used: set[int] = set()

    def _counter(self, at: float) -> int:
        return int(at // self._period_s)

    def _prune(self, now_counter: int) -> None:
        """Drop counters that can no longer verify anyway."""
        cutoff = now_counter - self._window
        self._used = {c for c in self._used if c >= cutoff}

    def verify(self, code: str, *, at: float | None = None) -> bool:
        """True if `code` is currently valid and not already redeemed.

        A successful verify consumes the code. Comparison is constant-time;
        the surrounding loop is not, but it runs over a fixed 3-step window
        regardless of input, so it leaks nothing about the secret.
        """
        candidate = _normalize(code)
        if not candidate.isdigit() or len(candidate) != self._digits:
            return False

        now = time.time() if at is None else at
        now_counter = self._counter(now)
        self._prune(now_counter)

        matched: int | None = None
        for offset in range(-self._window, self._window + 1):
            counter = now_counter + offset
            if counter < 0:
                continue
            expected = _code_for_counter(self._key, counter, self._digits)
            if hmac.compare_digest(expected, candidate):
                matched = counter
                # No early break: keep the loop's cost independent of
                # which step matched.

        if matched is None or matched in self._used:
            return False
        self._used.add(matched)
        return True
