"""Schwab brokerage tools (BR-FC-1..6).

Six tools wrapping the schwab-py SDK for Charles Schwab's Trader API:

Read tier (`requires_confirmation = False`):
- ``schwab_holdings``      — positions + balances (cached, atomic write)
- ``schwab_quote``         — bid/ask/last/mark for one or more symbols
- ``schwab_open_orders``   — pending orders across allowed accounts
- ``schwab_transactions``  — historical fills for an account

Write tier (`requires_confirmation = True`, gated via TLC-3):
- ``schwab_place_orders``  — batch equity buy/sell (limit or market)
- ``schwab_cancel_order``  — cancel a single pending order

Cross-cutting invariants (BR-TLC-1..8):
- App key/secret are SecretStr; never logged.
- Notional caps enforced *before* the gate (BR-TLC-2).
- Account allowlist is exact, case-insensitive (BR-TLC-3).
- Append-only audit log at ``<data_dir>/trade_log.jsonl`` (BR-TLC-4).
- Write tools refuse to run from non-interactive contexts (BR-TLC-5).
- Token + cache files are mode 0600 (BR-TLC-6).

The schwab-py SDK is sync (httpx-based); we wrap blocking calls with
``asyncio.to_thread``, mirroring the tiller pattern. The import is lazy
so module import does not require schwab-py to be installed.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

from ember.config import Settings
from ember.context.interactive import in_interactive_turn
from ember.errors import AuthError, ConfigError, ToolError, ToolRetryable, TransportError
from ember.tools.base import ToolResult

_log = logging.getLogger("ember.tools.schwab")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TRADE_LOG_FILENAME = "trade_log.jsonl"
_HOLDINGS_FILENAME = "holdings.json"
_TOKEN_FILENAME = ".schwab_token.json"

_AUTH_BOOTSTRAP_HINT = (
    "schwab: not authenticated; run "
    "uv run --with schwab-py --with python-dotenv "
    "python -m ember.tools.schwab_auth"
)

_AUTH_STALE_HINT = (
    "schwab: token is stale (Schwab refresh tokens expire after ~7 days); "
    "run /schwab-reauth to re-authenticate"
)

# Mirror schwab-py's `easy_client` default `max_token_age` (6.5 days). At or
# past this age easy_client silently discards the token and launches its
# interactive browser login flow — which blocks forever in a headless
# container (surfaces as a 300s MCP idle timeout). We check first and fail
# fast with an AuthError instead.
_MAX_TOKEN_AGE_SECONDS = 60 * 60 * 24 * 6.5

# Trader API: a successful place_order returns 201 with a Location header
# whose final path component is the order_id. The schwab-py SDK does not
# parse this; we strip it ourselves.
_OK_STATUS = (200, 201)

# Order-status filter for `schwab_open_orders`. Names match Schwab's enum.
_OPEN_STATUSES = ("WORKING", "PENDING_ACTIVATION", "AWAITING_PARENT_ORDER")

# Terminal statuses for cancel safety check.
_TERMINAL_STATUSES = (
    "FILLED",
    "CANCELED",
    "REJECTED",
    "EXPIRED",
    "REPLACED",
)


# ---------------------------------------------------------------------------
# Helpers — independent of any tool instance
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    """ISO-8601 UTC timestamp with millisecond precision and `Z` suffix."""
    now = _dt.datetime.now(_dt.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _parse_iso_date(s: str) -> _dt.date:
    return _dt.datetime.strptime(s, "%Y-%m-%d").date()


def _hash_request(args: dict[str, Any]) -> str:
    """Deterministic hash of canonical args for audit-log dedup."""
    try:
        canonical = json.dumps(args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        canonical = repr(args)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _audit(log_path: Path, event: str, **fields: Any) -> bool:
    """Append one JSON line to ``trade_log.jsonl`` (BR-TLC-4).

    Best-effort: log + swallow IOErrors. The tool call must not fail if
    the audit log can't be written (disk full, EROFS, etc.).

    Returns True when the line was durably written. Callers that depend on
    the log for *accounting* — the rolling daily cap reads back `placed`
    events — must not ignore a False: a dropped line silently frees up
    budget that was in fact spent.
    """
    entry: dict[str, Any] = {
        "ts": _utcnow_iso(),
        "event": event,
    }
    entry.update({k: v for k, v in fields.items() if v is not None or k in ("error",)})
    try:
        # Use opener to set mode 0600 on first creation. ``open()`` with
        # ``mode='a'`` honors the umask; passing an opener gives explicit
        # 0600 regardless. Subsequent appends keep whatever mode the file
        # already has.
        existed = log_path.exists()

        def _opener(p: str, flags: int) -> int:
            return os.open(p, flags, 0o600)

        with open(log_path, "a", encoding="utf-8", opener=_opener) as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if not existed:
            try:
                os.chmod(log_path, 0o600)
            except OSError:
                pass
        return True
    except OSError as e:
        _log.warning("schwab: failed to append audit log %s: %s", log_path, e)
        return False


def _read_daily_used_usd(log_path: Path, *, now: float | None = None) -> float:
    """Sum ``notional_usd`` from `placed` events in last 24h.

    Used by ``_enforce_caps`` for the rolling-daily cap (BR-FC-7). A
    missing log file returns 0.0 — a fresh deploy must not hit a false
    cap.

    An *unreadable* log is a different matter: it used to return 0.0 too,
    which silently reset the rolling budget to zero at exactly the moment
    we lost the ability to account for it (full disk, permissions, EROFS).
    That is the wrong direction to fail in a money path, so it now raises
    and the caller refuses the batch.
    """
    if not log_path.exists():
        return 0.0
    cutoff = (now or time.time()) - 86400.0
    total = 0.0
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if entry.get("event") != "placed":
                    continue
                ts = entry.get("ts")
                if not isinstance(ts, str):
                    continue
                try:
                    # `_utcnow_iso` writes ms + Z; tolerate other forms too.
                    if ts.endswith("Z"):
                        ts_dt = _dt.datetime.fromisoformat(ts[:-1] + "+00:00")
                    else:
                        ts_dt = _dt.datetime.fromisoformat(ts)
                except ValueError:
                    continue
                if ts_dt.timestamp() < cutoff:
                    continue
                amount = entry.get("notional_usd")
                if isinstance(amount, (int, float)):
                    total += float(amount)
    except OSError as e:
        _log.error("schwab: failed to read trade log for daily-used: %s", e)
        raise ToolError(
            "schwab: cannot read the trade log to compute the rolling daily cap "
            f"({type(e).__name__}); refusing to place orders while spend "
            "accounting is unavailable"
        ) from None
    return total


def _token_creation_timestamp(path: Path) -> float | None:
    """Read `creation_timestamp` from a schwab-py token file.

    Returns None if the file is unreadable, malformed, or missing the
    field — callers treat that as "age unknown" and let easy_client's
    own error paths handle it.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    ts = data.get("creation_timestamp") if isinstance(data, dict) else None
    return float(ts) if isinstance(ts, (int, float)) else None


def _format_money(amount: float) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def _resolve_account(
    name: str,
    settings: Settings,
    *,
    for_write: bool,
) -> str:
    """Resolve a friendly name to its canonical (cased) form.

    Match is case-insensitive but EXACT (no fuzzy substring) per BR-TLC-3.
    For write operations the friendly name must additionally appear in
    ``SCHWAB_ALLOWED_ACCOUNTS``; if that list is empty, all writes are
    refused.

    Returns the canonical friendly name (the spelling from
    ``SCHWAB_ACCOUNT_NAMES``) on success. Raises ``ToolError`` on miss.
    """
    if not isinstance(name, str) or not name.strip():
        raise ToolError("schwab: account name required")
    needle = name.strip().lower()

    canonical_by_lc: dict[str, str] = {v.lower(): v for v in settings.schwab_account_names.values()}
    canonical = canonical_by_lc.get(needle)
    if canonical is None:
        available = sorted(settings.schwab_account_names.values())
        raise ToolError(
            f"schwab: unknown account {name!r}; available: {', '.join(available) or '(none configured)'}"
        )

    if for_write:
        allowed_lc = {a.lower() for a in settings.schwab_allowed_accounts}
        if not allowed_lc:
            raise ToolError(
                "schwab: write operations require SCHWAB_ALLOWED_ACCOUNTS; "
                "currently empty (read-only)"
            )
        if canonical.lower() not in allowed_lc:
            raise ToolError(
                f"schwab: account {canonical!r} is not in SCHWAB_ALLOWED_ACCOUNTS "
                f"({', '.join(sorted(settings.schwab_allowed_accounts))})"
            )
    return canonical


def _allowed_friendly_names(settings: Settings, *, for_write: bool) -> set[str]:
    """Return the set of friendly names allowed for the given tier.

    For reads: every friendly name in ``SCHWAB_ACCOUNT_NAMES`` (or the
    ALLOWED_ACCOUNTS subset when set). For writes: only names in
    ``SCHWAB_ALLOWED_ACCOUNTS``.
    """
    known = set(settings.schwab_account_names.values())
    if for_write:
        return {a for a in settings.schwab_allowed_accounts if a in known}
    if settings.schwab_allowed_accounts:
        # If the user has an allowlist configured, reads honor it too —
        # this matches the BR-TLC-3 expectation that a name not in the
        # allowlist is implicitly off-limits unless the dict is empty.
        # Re-reading BR-TLC-3: "If SCHWAB_ALLOWED_ACCOUNTS is empty, all
        # accounts known to SCHWAB_ACCOUNT_NAMES are allowed for read".
        # The inverse is not stated — for symmetry we honor the
        # allowlist for reads too when present.
        return {a for a in settings.schwab_allowed_accounts if a in known}
    return known


def _holdings_price_for(
    holdings: dict[str, Any] | None,
    account_name: str,
    symbol: str,
) -> float | None:
    """Best-effort lookup of last price for a symbol from cached holdings.

    Used for cap evaluation on market orders and for the preview block.
    Returns ``None`` if holdings is missing or the symbol isn't held.
    """
    if not holdings:
        return None
    sym = symbol.upper()
    for acct in holdings.get("accounts", []):
        if acct.get("name", "").lower() != account_name.lower():
            continue
        for pos in acct.get("positions", []):
            if str(pos.get("symbol", "")).upper() != sym:
                continue
            qty = pos.get("quantity") or 0
            mv = pos.get("market_value") or 0
            if qty:
                try:
                    return float(mv) / float(qty)
                except (TypeError, ValueError, ZeroDivisionError):
                    return None
    return None


def _avg_price_for(
    holdings: dict[str, Any] | None,
    account_name: str,
    symbol: str,
) -> float | None:
    if not holdings:
        return None
    sym = symbol.upper()
    for acct in holdings.get("accounts", []):
        if acct.get("name", "").lower() != account_name.lower():
            continue
        for pos in acct.get("positions", []):
            if str(pos.get("symbol", "")).upper() != sym:
                continue
            avg = pos.get("average_price")
            try:
                return float(avg) if avg is not None else None
            except (TypeError, ValueError):
                return None
    return None


def _atomic_write_json(path: Path, payload: Any, *, mode: int = 0o600) -> None:
    """Write JSON atomically with explicit mode (BR-TLC-6)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    os.replace(tmp, path)


def _translate_http_status(status: int, body: str, *, where: str) -> Exception:
    """Map a Schwab HTTP status to the Ember error taxonomy."""
    truncated = body[:200] if isinstance(body, str) else ""
    if status == 401 or status == 403:
        return AuthError(f"{where}: HTTP {status}")
    if status == 404:
        return ToolError(f"{where}: HTTP 404: {truncated}")
    if status == 429:
        return ToolRetryable(f"{where}: HTTP 429 rate-limited")
    if 500 <= status < 600:
        return ToolRetryable(f"{where}: HTTP {status}: {truncated}")
    if 400 <= status < 500:
        return ToolError(f"schwab: HTTP {status}: {truncated}")
    return ToolError(f"{where}: HTTP {status}: {truncated}")


def _translate_sdk_error(exc: BaseException, *, where: str) -> Exception:
    """Map a raw schwab-py / httpx exception to the typed taxonomy."""
    # Lazy import — both schwab-py and httpx may be absent in some test
    # environments; we just want the type names.
    httpx_mod: Any
    try:
        import httpx as httpx_mod  # type: ignore
    except ImportError:
        httpx_mod = None

    if httpx_mod is not None:
        if isinstance(exc, httpx_mod.ConnectError):
            return TransportError(f"{where}: connect error: {exc}")
        if isinstance(exc, httpx_mod.ReadTimeout):
            return ToolRetryable(f"{where}: read timeout: {exc}")
        if isinstance(exc, httpx_mod.HTTPError):
            return TransportError(f"{where}: http error: {exc}")
    if isinstance(exc, OSError):
        return TransportError(f"{where}: os error: {exc}")
    return ToolError(f"{where}: sdk error: {exc}")


def _response_status(resp: Any) -> int:
    """Extract HTTP status from an httpx-like response."""
    s = getattr(resp, "status_code", None)
    return int(s) if isinstance(s, int) else 0


def _response_body_text(resp: Any) -> str:
    text = getattr(resp, "text", None)
    if isinstance(text, str):
        return text
    try:
        return json.dumps(resp.json())
    except Exception:
        return ""


def _parse_order_id_from_location(resp: Any) -> str | None:
    """Trader API returns `Location: https://.../orders/<id>` on 201."""
    headers = getattr(resp, "headers", None) or {}
    # httpx headers are case-insensitive, but a dict in tests may not be.
    location = None
    if hasattr(headers, "get"):
        location = headers.get("location") or headers.get("Location")
    if not location:
        return None
    return str(location).rstrip("/").split("/")[-1] or None


# ---------------------------------------------------------------------------
# Cap enforcement (BR-FC-7)
# ---------------------------------------------------------------------------


def _enforce_caps(
    orders: list[dict[str, Any]],
    settings: Settings,
    *,
    audit_log_path: Path,
    holdings: dict[str, Any] | None,
    holdings_age_s: float | None = None,
) -> list[float]:
    """Compute and validate notionals. Raises ``ToolError`` on overrun.

    Returns a parallel list of per-order notional in USD on success
    (used by the preview block + the audit `placed` line).

    A limit order carries its own price, so its notional is exact. A
    *market* order has no price in the request — we previously scored it
    against whatever `holdings.json` last said, with no age check. A
    position that has run up since the snapshot (cached $50, now $500)
    would then be scored at a tenth of its true cost and sail past a cap
    that should have stopped it. Market orders therefore now require a
    price from a snapshot no older than
    ``schwab_market_price_max_age_seconds``, and are refused otherwise —
    the caller can always pass an explicit `limit` instead, which both
    bounds the notional exactly and bounds the fill.
    """
    fallback = float(settings.schwab_fallback_price_usd)
    max_age = float(settings.schwab_market_price_max_age_seconds)
    per_order: list[float] = []

    for o in orders:
        symbol = str(o.get("symbol", "")).upper()
        qty = float(o.get("quantity") or 0)
        limit = o.get("limit")
        account = str(o.get("account", ""))

        if limit is not None:
            try:
                price = float(limit)
            except (TypeError, ValueError):
                price = fallback
        else:
            price_from_holdings = _holdings_price_for(holdings, account, symbol)
            if price_from_holdings is None:
                raise ToolError(
                    f"schwab: market order for {symbol} in {account!r} has no price "
                    "basis — the position is not in the cached holdings, so its "
                    "notional cannot be bounded against the caps. Pass an explicit "
                    "`limit` price instead."
                )
            if holdings_age_s is None or holdings_age_s > max_age:
                age_desc = (
                    f"{holdings_age_s / 3600:.1f}h old" if holdings_age_s else "of unknown age"
                )
                raise ToolError(
                    f"schwab: market order for {symbol} would be priced off a "
                    f"holdings snapshot {age_desc} (max "
                    f"{max_age / 60:.0f}m) — the cap check would be meaningless. "
                    "Refresh via schwab_holdings(force_refresh=true), or pass an "
                    "explicit `limit` price."
                )
            price = price_from_holdings

        notional = max(qty * price, 0.0)
        per_order.append(notional)

        if notional > float(settings.schwab_max_order_notional_usd):
            raise ToolError(
                f"schwab: order cap exceeded: {symbol} notional "
                f"{_format_money(notional)} > MAX_ORDER "
                f"{_format_money(settings.schwab_max_order_notional_usd)}"
            )

    batch = sum(per_order)
    if batch > float(settings.schwab_max_batch_notional_usd):
        raise ToolError(
            f"schwab: batch cap exceeded: {_format_money(batch)} > MAX_BATCH "
            f"{_format_money(settings.schwab_max_batch_notional_usd)}"
        )

    daily_used = _read_daily_used_usd(audit_log_path)
    daily_total = daily_used + batch
    if daily_total > float(settings.schwab_max_daily_notional_usd):
        raise ToolError(
            f"schwab: daily cap exceeded: {_format_money(daily_total)} > "
            f"MAX_DAILY {_format_money(settings.schwab_max_daily_notional_usd)} "
            f"(used {_format_money(daily_used)} in last 24h)"
        )
    return per_order


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------


def _summarize_write_for_alert(result: ToolResult) -> str:
    """Render a one-line alert for a completed write, or "" if there is
    nothing worth announcing (a call that placed and cancelled nothing).

    Shows account, side, quantity and symbol — enough to recognize an order
    you didn't authorize at a glance, without needing to open the channel it
    was placed from.
    """
    output = result.output
    if not isinstance(output, dict):
        return ""

    # place_orders: {"placed": n, "failed": n, "results": [...]}
    rows = output.get("results")
    if isinstance(rows, list):
        placed = [r for r in rows if isinstance(r, dict) and r.get("status") == "placed"]
        if not placed:
            return ""
        lines = [f"\N{MONEY BAG} **Schwab order{'s' if len(placed) > 1 else ''} placed**"]
        for r in placed:
            lines.append(
                f"• {r.get('action')} {r.get('quantity')} {r.get('symbol')} "
                f"— {r.get('account')} (order {r.get('order_id')})"
            )
        failed = output.get("failed") or 0
        if failed:
            lines.append(f"({failed} failed)")
        return "\n".join(lines)

    # cancel_order: {"canceled": True, "order_id": ..., "account": ...}
    # Note the single-l spelling the tool actually emits.
    if output.get("canceled"):
        return (
            f"\N{MONEY BAG} **Schwab order canceled** — "
            f"{output.get('order_id')} in {output.get('account')}"
        )
    return ""


class _SchwabBase:
    """Construction + lazy-client + account-hash resolution for all six
    Schwab tools.

    Shared fields:
    - ``self._settings``      — read-only Settings snapshot.
    - ``self._data_dir``      — ``<settings.data_dir>/schwab``.
    - ``self._token_path``    — token JSON (mode 0600).
    - ``self._holdings_path`` — cached normalized holdings (mode 0600).
    - ``self._audit_path``    — append-only trade log (mode 0600).

    The schwab-py ``Client`` is cached per tool instance; each tool builds
    its own (acceptable: identity is a small in-memory object). Multiple
    tools in the same process do *not* share a client — they're cheap and
    deduplicating ownership across instances introduces lifecycle
    questions we don't need to answer for v1.
    """

    name: str = ""
    description: str = ""
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {}

    def __init__(self, *, settings: Settings, data_dir: Path) -> None:
        self._settings = settings
        self._data_dir = data_dir
        self._token_path = data_dir / _TOKEN_FILENAME
        self._holdings_path = data_dir / _HOLDINGS_FILENAME
        self._audit_path = data_dir / _TRADE_LOG_FILENAME
        self._client: Any = None  # schwab.client.Client
        self._account_hash_cache: dict[str, tuple[str, str]] | None = None
        # Set after construction by main.py, once the channel exists (tools
        # are registered before it). None in tests and read-only deploys.
        self.notifier: Callable[[str], Awaitable[None]] | None = None

    async def _notify_writes(self, result: ToolResult) -> None:
        """Announce a completed write to the scheduler channel.

        Detection, not prevention: an order placed through Ember should
        leave a trace somewhere the user actually looks, rather than only
        in the trade log. Deliberately fired from `call()` rather than from
        the worker thread that writes the audit line, so it stays on the
        event loop.

        Best-effort — a failed notification must never turn a completed
        order into a tool error.
        """
        if self.notifier is None:
            return
        try:
            summary = _summarize_write_for_alert(result)
            if summary:
                await self.notifier(summary)
        except Exception:
            _log.exception("schwab: write alert failed (order itself unaffected)")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(data_dir={self._data_dir!s})"

    __str__ = __repr__

    # ----- client -------------------------------------------------------

    def _ensure_client(self) -> Any:
        """Lazily construct ``schwab.client.Client``.

        Wrapped exceptions (BR-FC-8):
        - missing app key/secret in settings → ``AuthError``
        - missing token file → ``AuthError`` with bootstrap hint
        - any other exception from ``easy_client`` → ``AuthError``
        """
        if self._client is not None:
            return self._client

        app_key = self._settings.schwab_app_key
        app_secret = self._settings.schwab_app_secret
        if app_key is None or app_secret is None:
            raise AuthError("schwab: SCHWAB_APP_KEY/SCHWAB_APP_SECRET unset")

        # easy_client(interactive=False) reads the token from disk; if
        # the file is missing it raises FileNotFoundError. Detect that
        # case explicitly so the user sees the bootstrap hint.
        if not self._token_path.exists():
            raise AuthError(_AUTH_BOOTSTRAP_HINT)

        created = _token_creation_timestamp(self._token_path)
        if created is not None and time.time() - created >= _MAX_TOKEN_AGE_SECONDS:
            raise AuthError(_AUTH_STALE_HINT)

        try:
            import schwab  # type: ignore
        except ImportError as e:
            raise ConfigError(f"schwab: schwab-py not installed: {e}") from None

        try:
            client = schwab.auth.easy_client(
                api_key=app_key.get_secret_value(),
                app_secret=app_secret.get_secret_value(),
                callback_url=self._settings.schwab_callback_url,
                token_path=str(self._token_path),
                interactive=False,
            )
        except FileNotFoundError:
            raise AuthError(_AUTH_BOOTSTRAP_HINT) from None
        except Exception as e:
            # Any other failure from schwab.auth.easy_client (token
            # decoding, refresh failure, etc.) — surface as AuthError
            # without the raw token in the message.
            msg = str(e)
            # schwab-py may stringify with the token; defensively strip
            # anything that looks like the app key/secret literal.
            for secret in (
                app_key.get_secret_value(),
                app_secret.get_secret_value(),
            ):
                if secret and secret in msg:
                    msg = msg.replace(secret, "<redacted>")
            raise AuthError(f"schwab: auth failed: {msg}") from None

        self._client = client
        return client

    # ----- account hashes -----------------------------------------------

    def _refresh_account_hashes(self) -> dict[str, tuple[str, str]]:
        """Map last4 → (hash, friendly_name). Cached per instance."""
        client = self._ensure_client()
        try:
            resp = client.get_account_numbers()
        except Exception as e:
            raise _translate_sdk_error(e, where="schwab: get_account_numbers")

        status = _response_status(resp)
        if status not in _OK_STATUS:
            raise _translate_http_status(
                status, _response_body_text(resp), where="schwab: get_account_numbers"
            )

        try:
            data = resp.json()
        except Exception as e:
            raise ToolError(f"schwab: malformed get_account_numbers payload: {e}") from None

        names = self._settings.schwab_account_names
        out: dict[str, tuple[str, str]] = {}
        for entry in data:
            account_number = str(entry.get("accountNumber", ""))
            hash_value = str(entry.get("hashValue", ""))
            if not account_number or not hash_value:
                continue
            last4 = account_number[-4:]
            friendly = names.get(last4, f"...{last4}")
            out[last4] = (hash_value, friendly)
        self._account_hash_cache = out
        return out

    def _resolve_account_hash(self, friendly_name: str) -> tuple[str, str]:
        """Return ``(hash, canonical_friendly_name)`` for ``friendly_name``.

        Raises ``ToolError`` if the name does not match any known account
        (after BR-TLC-3 friendly-name resolution).
        """
        canonical = _resolve_account(friendly_name, self._settings, for_write=False)
        cache = self._account_hash_cache or self._refresh_account_hashes()
        for last4, (hsh, fn) in cache.items():
            if fn.lower() == canonical.lower():
                return hsh, fn
        # The name is in SCHWAB_ACCOUNT_NAMES but the live API didn't
        # return a matching last4. Surface clearly.
        raise ToolError(
            f"schwab: account {canonical!r} configured but not present in "
            f"the account list returned by Schwab"
        )

    # ----- holdings cache ----------------------------------------------

    def _read_holdings_cache(self) -> dict[str, Any] | None:
        """Return the cached holdings payload, or None if missing/unreadable.

        Used by ``summarize_action`` (must be sync, no I/O on Schwab) and
        by ``_enforce_caps`` for last-known prices.
        """
        if not self._holdings_path.exists():
            return None
        try:
            return json.loads(self._holdings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            _log.warning("schwab: failed to read holdings cache: %s", e)
            return None

    def _read_holdings_cache_with_age(self) -> tuple[dict[str, Any], float | None] | None:
        """Cached holdings plus their age in seconds.

        Age is ``None`` when ``fetched_at`` is missing or unparseable — the
        payload is still usable, we just can't say how old it is. Returns
        ``None`` only when there is no readable cache at all.
        """
        cached = self._read_holdings_cache()
        if cached is None:
            return None
        fetched_at = cached.get("fetched_at")
        if not isinstance(fetched_at, str):
            return cached, None
        try:
            raw = fetched_at[:-1] + "+00:00" if fetched_at.endswith("Z") else fetched_at
            ts = _dt.datetime.fromisoformat(raw)
        except ValueError:
            return cached, None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        return cached, time.time() - ts.timestamp()

    def _invalidate_holdings_cache(self) -> None:
        """Delete the cache so the next read forces a refresh.

        Called post-confirm by `schwab_place_orders` — once we've placed
        orders, the cached positions are immediately stale (BC-doc open
        ambiguity #2: chose option (a)).
        """
        try:
            self._holdings_path.unlink(missing_ok=True)
        except OSError as e:
            _log.warning("schwab: failed to invalidate holdings cache: %s", e)


# ---------------------------------------------------------------------------
# Holdings normalization
# ---------------------------------------------------------------------------


def _normalize_position(pos: dict[str, Any]) -> dict[str, Any]:
    """Translate a Schwab position dict to the BR-FC-1 schema."""
    instrument = pos.get("instrument", {}) or {}
    symbol = instrument.get("symbol", "UNKNOWN")
    long_qty = float(pos.get("longQuantity", 0) or 0)
    short_qty = float(pos.get("shortQuantity", 0) or 0)
    quantity = long_qty - short_qty
    average_price = float(pos.get("averagePrice", 0) or 0)
    market_value = float(pos.get("marketValue", 0) or 0)

    # Schwab quirk: `currentDayProfitLoss` is computed as
    # `(marketValue - currentDayCost) - prevDayMarketValue`. For positions
    # bought today, `currentDayCost` is the cost of today's buy and the
    # math comes out right. For some positions Schwab erroneously sets
    # `currentDayCost` to the FULL cost basis even when no shares were
    # bought today (likely tied to journals / transfers / corp actions),
    # which makes the day P/L look catastrophic — e.g., NVDA shown as
    # -38% on a +1.7% day. Recover the true day P/L by adding
    # `currentDayCost` back; for positions where `currentDayCost == 0`
    # this is a no-op.
    raw_day_pl = float(pos.get("currentDayProfitLoss", 0) or 0)
    raw_day_cost = float(pos.get("currentDayCost", 0) or 0)
    current_day_pl = raw_day_pl + raw_day_cost
    # Recompute the day percent against the implied prior-session
    # market value rather than trusting Schwab's percent (which is also
    # affected by the same bug).
    prev_session_mv = market_value - current_day_pl
    current_day_pl_pct = (current_day_pl / prev_session_mv * 100.0) if prev_session_mv else 0.0

    cost_basis = average_price * abs(quantity) if average_price else 0.0
    gain_dollar = market_value - cost_basis
    gain_percent = (gain_dollar / cost_basis * 100.0) if cost_basis else 0.0

    return {
        "symbol": symbol,
        "description": instrument.get("description", "") or "",
        "asset_type": instrument.get("assetType", "UNKNOWN") or "UNKNOWN",
        "quantity": quantity,
        "average_price": round(average_price, 4),
        "market_value": round(market_value, 2),
        "cost_basis": round(cost_basis, 2),
        "gain_dollar": round(gain_dollar, 2),
        "gain_percent": round(gain_percent, 2),
        "day_gain_dollar": round(current_day_pl, 2),
        "day_gain_percent": round(current_day_pl_pct, 2),
    }


def _normalize_account(
    raw: dict[str, Any],
    *,
    name_map: dict[str, str],
) -> dict[str, Any]:
    """Translate a Schwab raw account dict to the BR-FC-1 schema."""
    securities = raw.get("securitiesAccount", {}) or {}
    acct_number = str(securities.get("accountNumber", "") or "")
    acct_type = str(securities.get("type", "UNKNOWN") or "UNKNOWN")
    balances = securities.get("currentBalances", {}) or {}

    last4 = acct_number[-4:] if acct_number else ""
    friendly = name_map.get(last4, f"{acct_type} (...{last4})") if last4 else acct_type

    positions_raw = securities.get("positions", []) or []
    positions = [_normalize_position(p) for p in positions_raw]
    positions.sort(key=lambda p: abs(p["market_value"]), reverse=True)

    cash = balances.get("cashBalance", balances.get("availableFunds", 0)) or 0
    liquidation = balances.get("liquidationValue", 0) or 0

    return {
        "name": friendly,
        "account_number_last4": last4,
        "type": acct_type,
        "current_balances": {
            "cash_balance": round(float(cash), 2),
            "liquidation_value": round(float(liquidation), 2),
        },
        "positions": positions,
    }


# ---------------------------------------------------------------------------
# schwab_holdings
# ---------------------------------------------------------------------------


class SchwabHoldingsTool(_SchwabBase):
    """Read positions + balances for all allowed Schwab accounts (BR-FC-1).

    Cache: ``holdings.json`` is reused if < ``schwab_holdings_cache_seconds``
    old (default 30 minutes). ``force_refresh=True`` bypasses the cache.

    Stale fallback: if the live path fails to authenticate (expired refresh
    token, revoked grant, 401) the cache is served regardless of age with
    ``stale: true`` + ``age_seconds`` + ``stale_reason`` set. Positions only
    change when a trade settles, so an old snapshot beats no answer — but
    balances and market values are as of ``fetched_at``, not now.
    """

    name: str = "schwab_holdings"
    description: str = (
        "Fetch and return positions + balances for all allowed Schwab "
        "accounts. By default returns cached holdings if < 30 minutes old; "
        "pass force_refresh=true to bypass the cache. Returns per-account "
        "positions with symbol, quantity, average_price, market_value, "
        "cost_basis, gain_dollar, gain_percent, day_gain_dollar, "
        "day_gain_percent, asset_type plus account balances. If Schwab auth "
        "has expired, returns the last cached snapshot with stale=true and "
        "age_seconds set — report positions as of fetched_at and tell the "
        "user to run /schwab-reauth rather than presenting them as current."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "force_refresh": {
                "type": "boolean",
                "default": False,
                "description": "Bypass the 30-minute cache.",
            },
            "accounts": {
                "type": ["array", "null"],
                "default": None,
                "items": {"type": "string"},
                "description": (
                    "Friendly account names to filter to. Default: all allowed accounts."
                ),
            },
        },
        "additionalProperties": False,
    }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        force_refresh = bool(args.get("force_refresh") or False)
        accounts_filter_raw = args.get("accounts")
        accounts_filter: list[str] | None
        if accounts_filter_raw is None:
            accounts_filter = None
        else:
            accounts_filter = [str(a) for a in accounts_filter_raw]
        return await asyncio.to_thread(
            self._fetch,
            force_refresh=force_refresh,
            accounts_filter=accounts_filter,
        )

    def _fetch(
        self,
        *,
        force_refresh: bool,
        accounts_filter: list[str] | None,
    ) -> ToolResult:
        # Cache hit?
        if not force_refresh:
            entry = self._read_holdings_cache_with_age()
            if entry is not None:
                cached, age = entry
                if age is not None and age < float(self._settings.schwab_holdings_cache_seconds):
                    cached["from_cache"] = True
                    payload = self._filter_accounts(cached, accounts_filter)
                    _audit(
                        self._audit_path,
                        "read",
                        tool=self.name,
                        from_cache=True,
                        account_count=len(payload.get("accounts", [])),
                    )
                    return ToolResult(output=payload)

        try:
            payload = self._live_fetch()
        except AuthError as e:
            # Auth is dead (expired refresh token, revoked grant, 401).
            # Positions only move when a trade settles, so a stale snapshot
            # is still the best available answer to "what do I hold?" —
            # serve it, loudly marked, instead of returning nothing. The
            # daily brief already treats this same file as the declared
            # portfolio; this closes the gap between the two consumers.
            stale = self._stale_fallback(accounts_filter, reason=str(e))
            if stale is None:
                raise
            return stale

        filtered = self._filter_accounts(payload, accounts_filter)
        _audit(
            self._audit_path,
            "read",
            tool=self.name,
            from_cache=False,
            account_count=len(filtered.get("accounts", [])),
        )
        return ToolResult(output=filtered)

    def _stale_fallback(
        self,
        accounts_filter: list[str] | None,
        *,
        reason: str,
    ) -> ToolResult | None:
        """Serve the stale cache when the live path can't authenticate.

        Returns None when there is nothing cached, in which case the caller
        re-raises the original ``AuthError``.
        """
        entry = self._read_holdings_cache_with_age()
        if entry is None:
            return None
        cached, age = entry
        cached["from_cache"] = True
        cached["stale"] = True
        cached["stale_reason"] = reason
        if age is not None:
            cached["age_seconds"] = int(age)
        payload = self._filter_accounts(cached, accounts_filter)
        _log.warning(
            "schwab_holdings: serving stale cache (age=%ss): %s",
            int(age) if age is not None else "unknown",
            reason,
        )
        _audit(
            self._audit_path,
            "read",
            tool=self.name,
            from_cache=True,
            stale=True,
            account_count=len(payload.get("accounts", [])),
        )
        return ToolResult(output=payload)

    def _live_fetch(self) -> dict[str, Any]:
        """Fetch positions + balances from Schwab and refresh the cache."""
        client = self._ensure_client()

        # Live fetch.
        try:
            resp_acct = client.get_account_numbers()
        except Exception as e:
            raise _translate_sdk_error(e, where="schwab_holdings: get_account_numbers")
        status = _response_status(resp_acct)
        if status not in _OK_STATUS:
            raise _translate_http_status(
                status,
                _response_body_text(resp_acct),
                where="schwab_holdings: get_account_numbers",
            )

        try:
            from schwab.client import Client as _Client  # type: ignore

            fields = [_Client.Account.Fields.POSITIONS]
        except ImportError as e:
            raise ConfigError(f"schwab: schwab-py not installed: {e}") from None
        except Exception:
            # If the SDK shape changed and the enum isn't where we expect,
            # fall back to the documented string value.
            fields = ["positions"]  # type: ignore[list-item]

        try:
            resp = client.get_accounts(fields=fields)
        except Exception as e:
            raise _translate_sdk_error(e, where="schwab_holdings: get_accounts")
        status = _response_status(resp)
        if status not in _OK_STATUS:
            raise _translate_http_status(
                status,
                _response_body_text(resp),
                where="schwab_holdings: get_accounts",
            )

        try:
            raw_accounts = resp.json()
        except Exception as e:
            raise ToolError(f"schwab_holdings: malformed payload: {e}") from None

        name_map = self._settings.schwab_account_names
        accounts = [_normalize_account(a, name_map=name_map) for a in raw_accounts]
        accounts.sort(key=lambda a: a["name"])

        payload = {
            "fetched_at": _utcnow_iso(),
            "from_cache": False,
            "accounts": accounts,
        }

        # Atomic write of the *unfiltered* payload — cache the whole
        # thing, then return a filtered view to the caller.
        try:
            _atomic_write_json(self._holdings_path, payload, mode=0o600)
        except OSError as e:
            _log.warning("schwab_holdings: cache write failed: %s", e)

        return payload

    def _filter_accounts(
        self,
        payload: dict[str, Any],
        accounts_filter: list[str] | None,
    ) -> dict[str, Any]:
        """Apply allowlist + caller-provided filter."""
        allowed = _allowed_friendly_names(self._settings, for_write=False)
        wanted_lc: set[str] | None = None
        if accounts_filter:
            wanted_lc = {a.lower() for a in accounts_filter}

        out_accounts = []
        for acct in payload.get("accounts", []):
            name = acct.get("name", "")
            if name not in allowed:
                # If the account isn't in SCHWAB_ACCOUNT_NAMES, skip it
                # (allowlist semantics — unknown accounts are not exposed).
                # Detect by membership in `allowed` since `allowed` already
                # honors the allowlist toggle.
                continue
            if wanted_lc is not None and name.lower() not in wanted_lc:
                continue
            out_accounts.append(acct)
        out: dict[str, Any] = {
            "fetched_at": payload.get("fetched_at"),
            "from_cache": payload.get("from_cache", False),
            "accounts": out_accounts,
        }
        # Staleness markers must survive filtering — they're the only thing
        # stopping a three-week-old snapshot from reading as live.
        for key in ("stale", "stale_reason", "age_seconds"):
            if key in payload:
                out[key] = payload[key]
        return out


# ---------------------------------------------------------------------------
# schwab_quote
# ---------------------------------------------------------------------------


def _normalize_quote(symbol: str, raw: dict[str, Any] | None) -> dict[str, Any]:
    """Translate a Schwab quote payload to the BR-FC-2 schema.

    Schwab returns each ticker keyed by symbol; the value is a dict with
    nested ``quote`` and ``regular`` blocks. We pluck the ones we need
    and return per-ticker `error="not_found"` for missing entries.
    """
    if not raw:
        return {
            "symbol": symbol,
            "last": None,
            "bid": None,
            "ask": None,
            "mark": None,
            "volume": None,
            "day_change_pct": None,
            "asof": None,
            "error": "not_found",
        }
    quote = raw.get("quote") or raw.get("regular") or {}

    def _f(*keys: str) -> Any:
        for k in keys:
            v = quote.get(k)
            if v is not None:
                return v
        return None

    asof_ms = _f("quoteTime", "tradeTime", "regularMarketTradeTime")
    asof_iso: str | None = None
    if isinstance(asof_ms, (int, float)) and asof_ms > 0:
        # Schwab returns epoch milliseconds.
        asof_iso = _dt.datetime.fromtimestamp(asof_ms / 1000.0, tz=_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    return {
        "symbol": symbol,
        "last": _f("lastPrice", "regularMarketLastPrice"),
        "bid": _f("bidPrice"),
        "ask": _f("askPrice"),
        "mark": _f("mark", "regularMarketLastPrice"),
        "volume": _f("totalVolume", "regularMarketVolume"),
        "day_change_pct": _f("netPercentChange", "netPercentChangeInDouble"),
        "asof": asof_iso,
        "error": None,
    }


class SchwabQuoteTool(_SchwabBase):
    """Get current quotes for one or more symbols (BR-FC-2)."""

    name: str = "schwab_quote"
    description: str = (
        "Get current quotes for one or more symbols. Returns last/bid/ask/"
        "mark, day change percent, and volume per ticker. Unknown tickers "
        "are returned with null prices and error='not_found'."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 10},
                "minItems": 1,
                "maxItems": 50,
            },
        },
        "required": ["symbols"],
        "additionalProperties": False,
    }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        raw_symbols = args["symbols"]
        # De-dupe while preserving order.
        seen: set[str] = set()
        symbols: list[str] = []
        for s in raw_symbols:
            up = str(s).upper().strip()
            if not up or up in seen:
                continue
            seen.add(up)
            symbols.append(up)
        if not symbols:
            raise ToolError("schwab_quote: no valid symbols")
        return await asyncio.to_thread(self._fetch, symbols=symbols)

    def _fetch(self, *, symbols: list[str]) -> ToolResult:
        client = self._ensure_client()
        try:
            resp = client.get_quotes(symbols)
        except Exception as e:
            raise _translate_sdk_error(e, where="schwab_quote: get_quotes")
        status = _response_status(resp)
        if status not in _OK_STATUS:
            raise _translate_http_status(
                status, _response_body_text(resp), where="schwab_quote: get_quotes"
            )
        try:
            data = resp.json()
        except Exception as e:
            raise ToolError(f"schwab_quote: malformed payload: {e}") from None

        # Schwab returns {symbol: {...}, ...}; missing symbols may not be
        # present at all OR may carry a per-symbol error block.
        quotes = []
        for s in symbols:
            quotes.append(_normalize_quote(s, data.get(s)))

        _audit(self._audit_path, "read", tool=self.name, symbols=",".join(symbols))
        return ToolResult(output={"asof": _utcnow_iso(), "quotes": quotes})


# ---------------------------------------------------------------------------
# schwab_open_orders
# ---------------------------------------------------------------------------


def _normalize_order(raw: dict[str, Any], *, name_map: dict[str, str]) -> dict[str, Any]:
    """Translate a Schwab order to the BR-FC-3 schema."""
    legs = raw.get("orderLegCollection") or []
    leg = legs[0] if legs else {}
    instrument = leg.get("instrument", {}) or {}
    instruction = str(leg.get("instruction", "")).upper()
    action = (
        "buy"
        if instruction.startswith("BUY")
        else ("sell" if instruction.startswith("SELL") else instruction.lower())
    )

    raw_account = str(raw.get("accountNumber") or raw.get("accountId") or "")
    last4 = raw_account[-4:] if raw_account else ""
    friendly = name_map.get(last4, f"...{last4}") if last4 else ""

    return {
        "order_id": str(raw.get("orderId") or ""),
        "account": friendly,
        "symbol": str(instrument.get("symbol") or ""),
        "action": action,
        "quantity": float(leg.get("quantity") or 0),
        "order_type": str(raw.get("orderType") or ""),
        "limit_price": (float(raw["price"]) if raw.get("price") is not None else None),
        "entered_at": str(raw.get("enteredTime") or ""),
        "status": str(raw.get("status") or ""),
    }


class SchwabOpenOrdersTool(_SchwabBase):
    """List pending orders across allowed Schwab accounts (BR-FC-3)."""

    name: str = "schwab_open_orders"
    description: str = (
        "List pending (working / pending-activation / awaiting-parent) "
        "orders across allowed Schwab accounts. Returns order id, account, "
        "symbol, action, quantity, order_type, limit_price, entered_at, "
        "status."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "account": {
                "type": ["string", "null"],
                "default": None,
                "description": "Friendly name; default: all allowed accounts.",
            },
            "max_age_days": {
                "type": "integer",
                "default": 30,
                "minimum": 1,
                "maximum": 90,
            },
        },
        "additionalProperties": False,
    }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        account = args.get("account")
        max_age_days = int(args.get("max_age_days") or 30)
        return await asyncio.to_thread(self._fetch, account=account, max_age_days=max_age_days)

    def _fetch(self, *, account: str | None, max_age_days: int) -> ToolResult:
        client = self._ensure_client()

        # Resolve filters. If `account` is given, validate against the
        # allowlist (read-tier). If absent, return for all allowed accounts.
        canonical: str | None = None
        if account:
            canonical = _resolve_account(account, self._settings, for_write=False)

        from_dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=max_age_days)
        to_dt = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1)

        # Resolve the status filter using whatever enum the SDK provides;
        # fall back to plain strings if introspection fails.
        statuses: list[Any] = list(_OPEN_STATUSES)
        try:
            from schwab.client import Client as _Client  # type: ignore

            sdk_statuses = []
            for s in _OPEN_STATUSES:
                try:
                    sdk_statuses.append(getattr(_Client.Order.Status, s))
                except AttributeError:
                    sdk_statuses.append(s)
            statuses = sdk_statuses
        except Exception:
            pass

        all_orders: list[dict[str, Any]] = []
        try:
            resp = client.get_orders_for_all_linked_accounts(
                from_entered_datetime=from_dt,
                to_entered_datetime=to_dt,
                statuses=statuses,
            )
        except TypeError:
            # Older SDK signatures may not accept the `statuses` kwarg as
            # a list — fall back to a single status to keep the read tier
            # alive even if the SDK shape drifts.
            try:
                resp = client.get_orders_for_all_linked_accounts(
                    from_entered_datetime=from_dt,
                    to_entered_datetime=to_dt,
                )
            except Exception as e:
                raise _translate_sdk_error(
                    e, where="schwab_open_orders: get_orders_for_all_linked_accounts"
                )
        except Exception as e:
            raise _translate_sdk_error(
                e, where="schwab_open_orders: get_orders_for_all_linked_accounts"
            )

        status = _response_status(resp)
        if status not in _OK_STATUS:
            raise _translate_http_status(
                status, _response_body_text(resp), where="schwab_open_orders"
            )
        try:
            data = resp.json()
        except Exception as e:
            raise ToolError(f"schwab_open_orders: malformed payload: {e}") from None

        if isinstance(data, list):
            all_orders = data
        elif isinstance(data, dict):
            all_orders = data.get("orders") or []

        allowed = _allowed_friendly_names(self._settings, for_write=False)
        name_map = self._settings.schwab_account_names

        normalized = []
        for raw in all_orders:
            n = _normalize_order(raw, name_map=name_map)
            if n["account"] not in allowed:
                continue
            if canonical is not None and n["account"].lower() != canonical.lower():
                continue
            # Defense-in-depth: if the SDK didn't filter status, do it here.
            if n["status"] and n["status"] not in _OPEN_STATUSES:
                continue
            normalized.append(n)

        _audit(
            self._audit_path,
            "read",
            tool=self.name,
            account=canonical,
            count=len(normalized),
        )
        return ToolResult(output={"orders": normalized})


# ---------------------------------------------------------------------------
# schwab_transactions
# ---------------------------------------------------------------------------


def _normalize_transaction(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate a Schwab transaction to the BR-FC-4 schema."""
    txn_type = str(raw.get("type") or raw.get("transactionType") or "")
    items = raw.get("transferItems") or []
    primary = items[0] if items else {}
    instrument = primary.get("instrument", {}) or {}
    symbol = str(instrument.get("symbol") or "")
    qty = primary.get("amount") if "amount" in primary else primary.get("quantity")
    price = primary.get("price")

    instruction = ""
    for it in items:
        # Schwab transaction items have a "positionEffect"/"feeType"; for
        # trade items, the side is conveyed via the sign of `amount`. Use
        # the legacy `description`/`subAccount` fallbacks first.
        desc = str(it.get("description") or "").upper()
        if "BUY" in desc:
            instruction = "buy"
            break
        if "SELL" in desc:
            instruction = "sell"
            break
    if not instruction and isinstance(qty, (int, float)):
        instruction = "buy" if qty > 0 else "sell" if qty < 0 else ""

    fee = 0.0
    for it in items:
        if str(it.get("feeType") or "").upper():
            try:
                fee += float(it.get("amount") or 0)
            except (TypeError, ValueError):
                continue

    amount = raw.get("netAmount")
    return {
        "ts": str(raw.get("time") or raw.get("transactionDate") or ""),
        "type": txn_type,
        "action": instruction,
        "symbol": symbol,
        "quantity": float(abs(qty)) if isinstance(qty, (int, float)) else 0.0,
        "price": float(price) if isinstance(price, (int, float)) else None,
        "amount": float(amount) if isinstance(amount, (int, float)) else None,
        "fees": round(fee, 2),
        "order_id": str(raw.get("orderId") or "") or None,
    }


class SchwabTransactionsTool(_SchwabBase):
    """Return executed transactions for an account in [since, until] (BR-FC-4)."""

    name: str = "schwab_transactions"
    description: str = (
        "Return executed transactions for an account in [since, until]. "
        "Useful for trade reconciliation and tax-lot tracking."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "account": {"type": "string"},
            "since": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD; inclusive.",
            },
            "until": {
                "type": ["string", "null"],
                "default": None,
                "description": "ISO date YYYY-MM-DD; inclusive. Default: today.",
            },
            "types": {
                "type": ["array", "null"],
                "default": None,
                "items": {"type": "string"},
                "description": (
                    "Schwab transaction types to keep "
                    "(e.g. ['TRADE','DIVIDEND_OR_INTEREST']). Default: all."
                ),
            },
        },
        "required": ["account", "since"],
        "additionalProperties": False,
    }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        account = str(args["account"])
        since = str(args["since"])
        until = args.get("until")
        types = args.get("types")
        try:
            since_d = _parse_iso_date(since)
        except ValueError:
            raise ToolError(f"schwab_transactions: invalid since={since!r}")
        until_d = _dt.date.today()
        if until:
            try:
                until_d = _parse_iso_date(str(until))
            except ValueError:
                raise ToolError(f"schwab_transactions: invalid until={until!r}")
        if since_d > until_d:
            raise ToolError(f"schwab_transactions: since {since_d} > until {until_d}")
        types_filter: list[str] | None = None
        if types:
            types_filter = [str(t) for t in types]

        return await asyncio.to_thread(
            self._fetch,
            account=account,
            since=since_d,
            until=until_d,
            types_filter=types_filter,
        )

    def _fetch(
        self,
        *,
        account: str,
        since: _dt.date,
        until: _dt.date,
        types_filter: list[str] | None,
    ) -> ToolResult:
        canonical = _resolve_account(account, self._settings, for_write=False)
        account_hash, friendly = self._resolve_account_hash(canonical)

        # Convert dates to UTC datetimes; Schwab accepts inclusive bounds.
        from_dt = _dt.datetime.combine(since, _dt.time.min, tzinfo=_dt.timezone.utc)
        to_dt = _dt.datetime.combine(until, _dt.time.max, tzinfo=_dt.timezone.utc)

        client = self._ensure_client()
        try:
            resp = client.get_transactions(
                account_hash,
                start_date=from_dt,
                end_date=to_dt,
            )
        except TypeError:
            # Older SDK uses positional args.
            try:
                resp = client.get_transactions(account_hash, from_dt, to_dt)
            except Exception as e:
                raise _translate_sdk_error(e, where="schwab_transactions: get_transactions")
        except Exception as e:
            raise _translate_sdk_error(e, where="schwab_transactions: get_transactions")

        status = _response_status(resp)
        if status not in _OK_STATUS:
            raise _translate_http_status(
                status, _response_body_text(resp), where="schwab_transactions"
            )
        try:
            raw = resp.json()
        except Exception as e:
            raise ToolError(f"schwab_transactions: malformed payload: {e}") from None
        if not isinstance(raw, list):
            raw = raw.get("transactions") if isinstance(raw, dict) else []
            raw = raw or []

        normalized = [_normalize_transaction(r) for r in raw]
        if types_filter:
            wanted = {t.upper() for t in types_filter}
            normalized = [t for t in normalized if t["type"].upper() in wanted]

        _audit(
            self._audit_path,
            "read",
            tool=self.name,
            account=friendly,
            count=len(normalized),
        )

        return ToolResult(
            output={
                "account": friendly,
                "since": since.isoformat(),
                "until": until.isoformat(),
                "transactions": normalized,
            }
        )


# ---------------------------------------------------------------------------
# schwab_place_orders
# ---------------------------------------------------------------------------


def _build_equity_order(action: str, symbol: str, quantity: float, limit: float | None) -> Any:
    """Construct a schwab-py order builder. Lazy import."""
    from schwab.orders.equities import (  # type: ignore
        equity_buy_limit,
        equity_buy_market,
        equity_sell_limit,
        equity_sell_market,
    )

    sym = symbol.upper()
    if action == "buy":
        return (
            equity_buy_limit(sym, quantity, limit)
            if limit is not None
            else equity_buy_market(sym, quantity)
        )
    if action == "sell":
        return (
            equity_sell_limit(sym, quantity, limit)
            if limit is not None
            else equity_sell_market(sym, quantity)
        )
    raise ToolError(f"schwab: unknown action {action!r}")


class SchwabPlaceOrdersTool(_SchwabBase):
    """Place a batch of equity orders (BR-FC-5; gated via TLC-3).

    Flow: validate args → resolve accounts → enforce caps → check
    interactive context → (gate) → place orders sequentially. Each order
    audit-logged on the way out.
    """

    name: str = "schwab_place_orders"
    description: str = (
        "Place a batch of equity orders across one or more Schwab "
        "accounts. Each order: action (buy|sell), symbol, quantity, "
        "optional limit price, account (friendly name). Always preview "
        "first; the system shows a confirmation prompt with the full "
        "preview block before any order is placed. Caps and account "
        "allowlist are enforced before the gate."
    )
    requires_confirmation: bool = True
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "orders": {
                "type": "array",
                "minItems": 1,
                "maxItems": 25,
                "items": {
                    "type": "object",
                    "properties": {
                        "account": {"type": "string"},
                        "action": {"enum": ["buy", "sell"]},
                        "symbol": {"type": "string", "minLength": 1, "maxLength": 10},
                        "quantity": {"type": "number", "exclusiveMinimum": 0},
                        "limit": {
                            "type": ["number", "null"],
                            "default": None,
                            "exclusiveMinimum": 0,
                        },
                    },
                    "required": ["account", "action", "symbol", "quantity"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["orders"],
        "additionalProperties": False,
    }

    # ----- summarize_action (no network I/O; reads holdings.json) ------

    def summarize_action(self, args: dict[str, Any]) -> str:
        """Render the user-facing confirm prompt (BR-FC-5).

        MUST NOT do network I/O. Reads `holdings.json` from disk for
        sells (to compute estimated gain) and for market-order price
        estimates. Falls back to "—" if a price isn't available.

        Market orders whose price basis is missing or too old will be
        refused by `_enforce_caps` after confirmation. Flag that here so
        the prompt doesn't ask you to approve something already doomed.
        """
        orders = args.get("orders") or []
        entry = self._read_holdings_cache_with_age()
        holdings, holdings_age = entry if entry is not None else (None, None)
        max_age = float(self._settings.schwab_market_price_max_age_seconds)
        price_is_stale = holdings_age is None or holdings_age > max_age

        lines: list[str] = []
        lines.append(f"schwab_place_orders — {len(orders)} order(s)\n")

        blocked: list[str] = []
        total = 0.0
        for o in orders:
            action = str(o.get("action", "")).upper()
            symbol = str(o.get("symbol", "")).upper()
            qty = o.get("quantity") or 0
            limit = o.get("limit")
            account = str(o.get("account", ""))

            price = None
            if limit is not None:
                price = float(limit)
            else:
                price = _holdings_price_for(holdings, account, symbol)
                if price is None:
                    blocked.append(
                        f"{symbol}: market order with no price basis "
                        f"(not held in {account}) — pass a limit price"
                    )
                elif price_is_stale:
                    age_desc = (
                        f"{holdings_age / 3600:.1f}h old" if holdings_age else "of unknown age"
                    )
                    blocked.append(
                        f"{symbol}: market order priced off a holdings snapshot "
                        f"{age_desc} (max {max_age / 60:.0f}m) — refresh holdings "
                        "or pass a limit price"
                    )

            if price is None:
                est_str = "est —"
            else:
                est = float(qty) * float(price)
                total += est
                est_str = f"est {_format_money(est)}"

            order_type = (
                f"@ limit {_format_money(float(limit))}" if limit is not None else "@ market"
            )

            extra = ""
            if action == "SELL":
                avg = _avg_price_for(holdings, account, symbol)
                if avg is not None and price is not None:
                    gain = (float(price) - avg) * float(qty)
                    sign = "+" if gain >= 0 else "-"
                    tax = abs(gain) * 0.25
                    extra = f"  (gain ~{sign}{_format_money(abs(gain))}, tax ~{_format_money(tax)})"

            lines.append(
                f"  {action:<4} {qty:g} {symbol} {order_type:<22} in {account}  {est_str}{extra}"
            )

        lines.append("")
        if total:
            lines.append(f"Total notional: {_format_money(total)}")

        s = self._settings
        try:
            used_str = f"used {_format_money(_read_daily_used_usd(self._audit_path))} in last 24h"
        except ToolError:
            # The daily cap now fails closed, so this batch will be refused
            # at execution. Say so in the prompt rather than rendering a
            # reassuring "$0.00 used".
            used_str = "24h spend UNREADABLE — orders will be refused"
        lines.append(
            f"Caps: order {_format_money(s.schwab_max_order_notional_usd)} · "
            f"batch {_format_money(s.schwab_max_batch_notional_usd)} · "
            f"daily {_format_money(s.schwab_max_daily_notional_usd)} "
            f"({used_str})"
        )

        if blocked:
            lines.append("")
            lines.append("⚠️ WILL BE REFUSED on approval:")
            lines.extend(f"  - {b}" for b in blocked)

        # BR-UoW-5 step 3: one preview audit line per order on the gate path.
        # summarize_action is the only hook that fires before the user's "yes" —
        # auditing here captures every gated proposal whether or not it's confirmed.
        for o in orders:
            try:
                _audit(
                    self._audit_path,
                    event="preview",
                    tool=self.name,
                    account=str(o.get("account", "")),
                    action=str(o.get("action", "")),
                    symbol=str(o.get("symbol", "")).upper(),
                    quantity=o.get("quantity"),
                    limit=o.get("limit"),
                )
            except Exception:
                pass
        return "\n".join(lines)

    # ----- call ---------------------------------------------------------

    async def call(self, args: dict[str, Any]) -> ToolResult:
        orders_raw = args.get("orders") or []
        orders: list[dict[str, Any]] = []
        for o in orders_raw:
            orders.append(
                {
                    "account": str(o.get("account", "")),
                    "action": str(o.get("action", "")),
                    "symbol": str(o.get("symbol", "")).upper(),
                    "quantity": float(o.get("quantity") or 0),
                    "limit": (float(o["limit"]) if o.get("limit") is not None else None),
                }
            )

        # BR-TLC-3: validate the friendly names + write allowlist.
        # Done before caps + context so a malformed/unauthorized account
        # short-circuits without touching the audit log.
        for o in orders:
            _resolve_account(o["account"], self._settings, for_write=True)

        # BR-FC-7 / BR-TLC-2: cap enforcement. Caps are a safety
        # mechanism; they fire regardless of interactive context so a
        # buggy scheduler can't get past them either. Holdings is
        # best-effort; missing cache → fallback price.
        entry = self._read_holdings_cache_with_age()
        holdings, holdings_age = entry if entry is not None else (None, None)
        per_order_notional = _enforce_caps(
            orders,
            self._settings,
            audit_log_path=self._audit_path,
            holdings=holdings,
            holdings_age_s=holdings_age,
        )

        # BR-TLC-5: refuse if not in an interactive turn. The registry
        # gate already returned the confirmation marker on the first
        # call; by the time we land here we should be inside a user
        # turn (the user replied "yes"). If the contextvar is False,
        # this is a scheduler/proactive path and we fail closed.
        if not in_interactive_turn.get():
            return ToolResult(
                output={"error": "no_interactive_context"},
                is_error=True,
            )

        # Every order gets a `preview` audit line — captures the request
        # even if the SDK call later fails or the process dies mid-batch.
        for o, notional in zip(orders, per_order_notional):
            _audit(
                self._audit_path,
                "preview",
                tool=self.name,
                account=o["account"],
                action=o["action"],
                symbol=o["symbol"],
                quantity=o["quantity"],
                limit=o["limit"],
                notional_usd=round(notional, 2),
                request_hash=_hash_request(o),
            )

        result = await asyncio.to_thread(
            self._place_all, orders=orders, notional=per_order_notional
        )
        await self._notify_writes(result)
        return result

    # ----- _place_all (sync, runs on worker thread) ---------------------

    def _place_all(
        self,
        *,
        orders: list[dict[str, Any]],
        notional: list[float],
    ) -> ToolResult:
        client = self._ensure_client()

        # Resolve every account hash up front. If any miss, abort the
        # whole batch — partial submission is worse than none.
        account_hashes: dict[str, tuple[str, str]] = {}
        for o in orders:
            if o["account"] not in account_hashes:
                account_hashes[o["account"]] = self._resolve_account_hash(o["account"])

        # `confirmed` audit line: one per order, captures that the user
        # said yes and we're about to submit.
        for o in orders:
            _audit(
                self._audit_path,
                "confirmed",
                tool=self.name,
                account=o["account"],
                action=o["action"],
                symbol=o["symbol"],
                quantity=o["quantity"],
                limit=o["limit"],
            )

        results: list[dict[str, Any]] = []
        audit_write_failures: list[dict[str, Any]] = []
        placed = 0
        failed = 0
        for i, o in enumerate(orders):
            if i > 0:
                time.sleep(1.0)  # BR-UoW-5: 1s between submissions
            account_hash, friendly = account_hashes[o["account"]]
            try:
                builder = _build_equity_order(o["action"], o["symbol"], o["quantity"], o["limit"])
                resp = client.place_order(account_hash, builder.build())
            except Exception as e:
                err = str(e)
                results.append(
                    {
                        "symbol": o["symbol"],
                        "action": o["action"],
                        "quantity": o["quantity"],
                        "account": friendly,
                        "status": "failed",
                        "error": f"sdk error: {err}",
                    }
                )
                failed += 1
                _audit(
                    self._audit_path,
                    "failed",
                    tool=self.name,
                    account=friendly,
                    action=o["action"],
                    symbol=o["symbol"],
                    quantity=o["quantity"],
                    limit=o["limit"],
                    http_status=None,
                    error=err[:500],
                    notional_usd=round(notional[i], 2),
                    request_hash=_hash_request(o),
                )
                continue

            status = _response_status(resp)
            body = _response_body_text(resp)

            if status in _OK_STATUS:
                order_id = _parse_order_id_from_location(resp)
                results.append(
                    {
                        "symbol": o["symbol"],
                        "action": o["action"],
                        "quantity": o["quantity"],
                        "account": friendly,
                        "status": "placed",
                        "order_id": order_id,
                    }
                )
                placed += 1
                if not _audit(
                    self._audit_path,
                    "placed",
                    tool=self.name,
                    account=friendly,
                    action=o["action"],
                    symbol=o["symbol"],
                    quantity=o["quantity"],
                    limit=o["limit"],
                    order_id=order_id,
                    http_status=status,
                    notional_usd=round(notional[i], 2),
                    request_hash=_hash_request(o),
                ):
                    # The order is real but its spend is now invisible to
                    # the rolling daily cap. We can't unplace it, so say so
                    # loudly rather than let the budget quietly refill.
                    _log.error(
                        "schwab_place_orders: order %s placed but audit write failed; "
                        "daily-cap accounting is now under-counting by %.2f",
                        order_id,
                        notional[i],
                    )
                    audit_write_failures.append(
                        {
                            "order_id": order_id,
                            "symbol": o["symbol"],
                            "notional_usd": round(notional[i], 2),
                        }
                    )
            else:
                results.append(
                    {
                        "symbol": o["symbol"],
                        "action": o["action"],
                        "quantity": o["quantity"],
                        "account": friendly,
                        "status": "failed",
                        "error": f"HTTP {status}: {body[:200]}",
                    }
                )
                failed += 1
                _audit(
                    self._audit_path,
                    "failed",
                    tool=self.name,
                    account=friendly,
                    action=o["action"],
                    symbol=o["symbol"],
                    quantity=o["quantity"],
                    limit=o["limit"],
                    http_status=status,
                    error=body[:500],
                    notional_usd=round(notional[i], 2),
                    request_hash=_hash_request(o),
                )

        # Holdings cache invalidation — open ambiguity #2, chose option
        # (a). Best-effort; failure to delete is logged and ignored.
        if placed > 0:
            self._invalidate_holdings_cache()

        output: dict[str, Any] = {"placed": placed, "failed": failed, "results": results}
        if audit_write_failures:
            output["audit_write_failed"] = audit_write_failures
            output["warning"] = (
                "One or more orders were placed but could not be written to the "
                "trade log. The rolling daily cap is now under-counting that "
                "spend — reconcile against Schwab before placing more orders."
            )
        return ToolResult(output=output)


# ---------------------------------------------------------------------------
# schwab_cancel_order
# ---------------------------------------------------------------------------


class SchwabCancelOrderTool(_SchwabBase):
    """Cancel a single pending Schwab order (BR-FC-6; gated via TLC-3)."""

    name: str = "schwab_cancel_order"
    description: str = (
        "Cancel a single pending Schwab order. The confirmation prompt "
        "shows the order's symbol, action, quantity, and status before "
        "you approve."
    )
    requires_confirmation: bool = True
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "account": {"type": "string"},
            "order_id": {"type": "string", "minLength": 1},
        },
        "required": ["account", "order_id"],
        "additionalProperties": False,
    }

    # ----- summarize_action --------------------------------------------

    def summarize_action(self, args: dict[str, Any]) -> str:
        """Look up the order and render details. May do network I/O —
        unlike `schwab_place_orders.summarize_action`, this gate is
        single-call and the latency is acceptable. If lookup fails we
        return a message rather than blowing up the gate path."""
        account = str(args.get("account") or "")
        order_id = str(args.get("order_id") or "")
        try:
            details = self._lookup_order_sync(account, order_id)
        except Exception as e:
            return f"schwab_cancel_order: cannot look up {order_id} in {account}: {e}"

        if details is None:
            return f"schwab_cancel_order: order {order_id} not found in {account}"
        status = details.get("status", "")
        if status in _TERMINAL_STATUSES:
            return (
                f"schwab_cancel_order: order {order_id} is {status} (terminal); nothing to cancel"
            )

        action = str(details.get("action", "")).upper()
        qty = details.get("quantity") or 0
        sym = details.get("symbol") or ""
        limit = details.get("limit_price")
        price_str = f" @ {_format_money(float(limit))}" if limit is not None else ""
        return (
            f"schwab_cancel_order: {action} {qty:g} {sym}{price_str} "
            f"({status}) in {account} — order {order_id}"
        )

    def _lookup_order_sync(self, account: str, order_id: str) -> dict[str, Any] | None:
        canonical = _resolve_account(account, self._settings, for_write=False)
        account_hash, _ = self._resolve_account_hash(canonical)
        client = self._ensure_client()
        try:
            resp = client.get_order(order_id, account_hash)
        except Exception as e:
            raise _translate_sdk_error(e, where="schwab_cancel_order: get_order")
        status = _response_status(resp)
        if status == 404:
            return None
        if status not in _OK_STATUS:
            raise _translate_http_status(
                status, _response_body_text(resp), where="schwab_cancel_order: get_order"
            )
        try:
            raw = resp.json()
        except Exception as e:
            raise ToolError(f"schwab_cancel_order: malformed payload: {e}") from None
        return _normalize_order(raw, name_map=self._settings.schwab_account_names)

    # ----- call ---------------------------------------------------------

    async def call(self, args: dict[str, Any]) -> ToolResult:
        account = str(args["account"])
        order_id = str(args["order_id"])

        if not in_interactive_turn.get():
            return ToolResult(
                output={"error": "no_interactive_context"},
                is_error=True,
            )

        # BR-TLC-3: cancel is a write op — must be in ALLOWED_ACCOUNTS.
        canonical = _resolve_account(account, self._settings, for_write=True)
        result = await asyncio.to_thread(self._cancel, canonical=canonical, order_id=order_id)
        await self._notify_writes(result)
        return result

    def _cancel(self, *, canonical: str, order_id: str) -> ToolResult:
        account_hash, friendly = self._resolve_account_hash(canonical)
        client = self._ensure_client()

        # Look up to capture details for audit + to refuse terminal-state
        # cancels (a no-op on the API side but the post-confirm path
        # should surface clearly).
        try:
            details = self._lookup_order_sync(canonical, order_id)
        except Exception as e:
            _audit(
                self._audit_path,
                "failed",
                tool=self.name,
                account=friendly,
                order_id=order_id,
                error=str(e)[:500],
            )
            return ToolResult(
                output={"canceled": False, "error": f"lookup failed: {e}"},
                is_error=True,
            )
        if details is None:
            _audit(
                self._audit_path,
                "failed",
                tool=self.name,
                account=friendly,
                order_id=order_id,
                error="not_found",
            )
            return ToolResult(
                output={"canceled": False, "error": "order not found"},
                is_error=True,
            )
        if details.get("status") in _TERMINAL_STATUSES:
            _audit(
                self._audit_path,
                "failed",
                tool=self.name,
                account=friendly,
                order_id=order_id,
                error=f"terminal:{details.get('status')}",
            )
            return ToolResult(
                output={
                    "canceled": False,
                    "error": f"order in terminal state {details.get('status')}",
                },
                is_error=True,
            )

        try:
            resp = client.cancel_order(order_id, account_hash)
        except Exception as e:
            err = str(e)
            _audit(
                self._audit_path,
                "failed",
                tool=self.name,
                account=friendly,
                order_id=order_id,
                error=err[:500],
            )
            return ToolResult(
                output={"canceled": False, "error": f"sdk error: {err}"},
                is_error=True,
            )
        status = _response_status(resp)
        if status not in _OK_STATUS:
            body = _response_body_text(resp)
            _audit(
                self._audit_path,
                "failed",
                tool=self.name,
                account=friendly,
                order_id=order_id,
                http_status=status,
                error=body[:500],
            )
            return ToolResult(
                output={
                    "canceled": False,
                    "error": f"HTTP {status}: {body[:200]}",
                },
                is_error=True,
            )

        _audit(
            self._audit_path,
            "canceled",
            tool=self.name,
            account=friendly,
            order_id=order_id,
            symbol=details.get("symbol"),
            action=details.get("action"),
            quantity=details.get("quantity"),
            http_status=status,
        )
        return ToolResult(
            output={
                "canceled": True,
                "order_id": order_id,
                "account": friendly,
                "status": status,
            }
        )


__all__ = [
    "SchwabHoldingsTool",
    "SchwabQuoteTool",
    "SchwabOpenOrdersTool",
    "SchwabTransactionsTool",
    "SchwabPlaceOrdersTool",
    "SchwabCancelOrderTool",
    "_enforce_caps",
    "_audit",
    "_resolve_account",
]
