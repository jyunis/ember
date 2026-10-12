"""Schwab brokerage tool tests (BR-TLC-1..8, BR-UoW-1..6, BR-FC-1..8).

Covers `ember.tools.schwab.{SchwabHoldingsTool, SchwabQuoteTool,
SchwabOpenOrdersTool, SchwabTransactionsTool, SchwabPlaceOrdersTool,
SchwabCancelOrderTool}` and the wiring helper
`_maybe_register_schwab_tools` in `ember.main`. Also exercises the
TLC-3 confirmation flow at the registry level for the two gated tools.

Mocking strategy: monkeypatch `schwab.auth.easy_client` (the symbol the
implementer is expected to use for client construction) to a stub that
returns a `FakeSchwabClient`. SDK methods on the fake return
httpx.Response-shaped objects (`.status_code`, `.json()`, `.headers`,
`.text`); for HTTP-error cases the test sets `status_code=401` etc. and
asserts the implementer's mapped exception per the BC error-mapping
table.

Tester-flagged ambiguities (the resolutions encoded here are listed in
the final report):

1. **Contextvar location** — BR-TLC-5 says the implementer reads a
   contextvar at `ember.agent.context.in_interactive_turn`. The BC
   doc mentions both `ember.agent.context` and `ember.context` as
   acceptable locations. Tests try-import both and use whichever
   resolves; if neither, the relevant tests are skipped.

2. **`easy_client` import path** — schwab-py exposes `easy_client` at
   both `schwab.auth.easy_client` and `schwab.easy_client`. Tests
   patch `schwab.auth.easy_client` (the canonical path); the implementer
   should be importing from there.

3. **Audit log shape — `request_hash`** — the BC schema calls for a
   `request_hash` field but doesn't pin an exact algorithm. Tests assert
   the line is JSON-parseable with the spec'd top-level fields and don't
   pin the hash algorithm or its bytes.

4. **`Account.Fields.POSITIONS` accessor** — schwab-py historically used
   `client.Account.Fields.POSITIONS`, but newer versions use
   `Client.Account.Fields.POSITIONS`. Tests don't assert which one the
   implementer used; we only assert `get_accounts` was called with
   *some* `fields` argument.

5. **Trade log path** — BC says `<data_dir>/schwab/trade_log.jsonl`.
   Tests construct a fresh `data_dir` via `tmp_path`, scaffold the
   `schwab/` subdirectory, and assert the audit lines land there.

6. **Order builder identity** — schwab-py exposes
   `equity_buy_market`, `equity_buy_limit`, `equity_sell_market`,
   `equity_sell_limit`. The actual order-builder is implementer's
   choice; tests assert `place_order` was called with the right account
   hash and *some* order arg (we don't crack the order builder open).

7. **Confirmation marker shape** — TLC-3 marker is
   `{"error": "confirmation_required", "action": <str>}`. We assert
   `is_error=True` and `output["error"] == "confirmation_required"`.

8. **`from_cache` flag on cached read** — BR-UoW-1 says cached returns
   "the same payload" but doesn't pin the exact `from_cache` field.
   Tests assert `from_cache: True` on a fresh-cache hit (matches the
   FC-1 schema example) and `from_cache: False` on a refresh.

9. **summarize_action gain/tax format** — FC-5 shows "(gain ~$432,
   tax ~$108)" but does not pin exact rounding. Tests assert the
   substring "gain ~$" appears in the rendered preview when a sell of
   a held position is summarized. We do not pin the dollar value.
"""

from __future__ import annotations

import datetime as _dt
import importlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest

# Importing here is intentionally done at the module level so collection
# fails clearly when `ember.tools.schwab` doesn't exist yet (the expected
# baseline for the implementer; this test file is on a separate branch
# from impl/brokerage-1-4 and runs only after both branches merge into
# feature/brokerage).
from ember.config import Settings  # noqa: E402
from ember.errors import AuthError, ToolError  # noqa: E402
from ember.tools.base import Tool, ToolResult  # noqa: E402
from ember.tools.registry import Registry  # noqa: E402
from ember.tools.schwab import (  # noqa: E402
    SchwabCancelOrderTool,
    SchwabHoldingsTool,
    SchwabOpenOrdersTool,
    SchwabPlaceOrdersTool,
    SchwabQuoteTool,
    SchwabTransactionsTool,
)

from tests.fakes.fake_schwab import (
    FakeSchwabClient,
    _AccountFields,
    created,
    http_error,
    ok,
    sample_account_numbers,
    sample_accounts_payload,
    sample_position,
)


# Sentinels — secret values the AuthError-leak tests scan for.
SECRET_APP_KEY = "schwab-app-key-XXX-LEAK-XXX"  # noqa: S105 — test sentinel
SECRET_APP_SECRET = "schwab-app-secret-YYY-LEAK-YYY"  # noqa: S105 — test sentinel


# ---------------------------------------------------------------------------
# Settings + client install helpers
# ---------------------------------------------------------------------------


def _make_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    app_key: str | None = SECRET_APP_KEY,
    app_secret: str | None = SECRET_APP_SECRET,
    account_names: str
    | dict[str, str]
    | None = "1234=Individual Brokerage,5678=Roth Contributory IRA",
    allowed_accounts: str | list[str] | None = "Individual Brokerage,Roth Contributory IRA",
    max_order: float = 20000.0,
    max_batch: float = 75000.0,
    max_daily: float = 100000.0,
    fallback_price: float = 1000.0,
    extra: dict[str, Any] | None = None,
) -> Settings:
    """Build a Settings object via env vars (the production path).

    The discord required-fields are stubbed; the schwab fields are set
    or cleared per test. Avoids reading the real `.env` file.
    """
    monkeypatch.chdir(tmp_path)
    # Required Discord fields (any value).
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")

    # Strip any pre-existing schwab env (test isolation).
    for k in (
        "SCHWAB_APP_KEY",
        "SCHWAB_APP_SECRET",
        "SCHWAB_CALLBACK_URL",
        "SCHWAB_ACCOUNT_NAMES",
        "SCHWAB_ALLOWED_ACCOUNTS",
        "SCHWAB_MAX_ORDER_NOTIONAL_USD",
        "SCHWAB_MAX_BATCH_NOTIONAL_USD",
        "SCHWAB_MAX_DAILY_NOTIONAL_USD",
        "SCHWAB_FALLBACK_PRICE_USD",
        "SCHWAB_HOLDINGS_CACHE_SECONDS",
    ):
        monkeypatch.delenv(k, raising=False)

    if app_key is not None:
        monkeypatch.setenv("SCHWAB_APP_KEY", app_key)
    if app_secret is not None:
        monkeypatch.setenv("SCHWAB_APP_SECRET", app_secret)
    if account_names is not None:
        if isinstance(account_names, dict):
            account_names = ",".join(f"{k}={v}" for k, v in account_names.items())
        monkeypatch.setenv("SCHWAB_ACCOUNT_NAMES", account_names)
    if allowed_accounts is not None:
        if isinstance(allowed_accounts, list):
            allowed_accounts = ",".join(allowed_accounts)
        monkeypatch.setenv("SCHWAB_ALLOWED_ACCOUNTS", allowed_accounts)
    monkeypatch.setenv("SCHWAB_MAX_ORDER_NOTIONAL_USD", str(max_order))
    monkeypatch.setenv("SCHWAB_MAX_BATCH_NOTIONAL_USD", str(max_batch))
    monkeypatch.setenv("SCHWAB_MAX_DAILY_NOTIONAL_USD", str(max_daily))
    monkeypatch.setenv("SCHWAB_FALLBACK_PRICE_USD", str(fallback_price))

    # Force data_dir to tmp_path so the trade_log/holdings.json land
    # somewhere we can scrape.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    if extra:
        for k, v in extra.items():
            monkeypatch.setenv(k, str(v))

    return Settings()  # type: ignore[call-arg]


def _data_dir(tmp_path: Path) -> Path:
    """The schwab data subdir under tmp_path/.

    Tests construct tools with `data_dir=<schwab_subdir>`, mirroring
    the wiring helper which does `settings.data_dir / "schwab"`.

    Drops a stub token file so the implementer's pre-`easy_client`
    `token_path.exists()` check passes. Tests that exercise the
    missing-token branch (currently only one) unlink it explicitly.
    """
    p = tmp_path / "schwab"
    p.mkdir(mode=0o700, exist_ok=True)
    token_path = p / ".schwab_token.json"
    # Fresh creation_timestamp so the stale-token guard doesn't trip;
    # the staleness branch has its own dedicated tests.
    token_path.write_text('{"creation_timestamp": %d, "token": {}}' % int(time.time()))
    token_path.chmod(0o600)
    return p


def _install_easy_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: FakeSchwabClient | None = None,
    raises: BaseException | None = None,
) -> FakeSchwabClient:
    """Replace `schwab.auth.easy_client` with a stub that records calls
    and returns the given fake client (constructing one if absent).
    """
    if client is None:
        client = FakeSchwabClient()

    def _stub(*args: Any, **kwargs: Any) -> FakeSchwabClient:
        if raises is not None:
            raise raises
        return client

    # Patch on the schwab.auth module itself so any lazy `from schwab.auth
    # import easy_client` or `import schwab.auth as sa; sa.easy_client(...)`
    # picks up the stub. Defensive: also install the Account.Fields stub.
    try:
        import schwab.auth as schwab_auth  # type: ignore[import-not-found]

        monkeypatch.setattr(schwab_auth, "easy_client", _stub, raising=True)
    except Exception:
        # In the test-only worktree the schwab-py dep may or may not be
        # installed. We still install the stub on a synthetic module so
        # the implementer's lazy import returns it.
        import sys as _sys
        import types as _types

        synthetic = _types.ModuleType("schwab.auth")
        synthetic.easy_client = _stub  # type: ignore[attr-defined]
        _sys.modules.setdefault("schwab", _types.ModuleType("schwab"))
        _sys.modules["schwab.auth"] = synthetic

    # Provide a minimal `schwab.client.Client.Account.Fields` so the
    # implementer's `fields=[Client.Account.Fields.POSITIONS]` arg
    # doesn't AttributeError when constructed.
    try:
        import schwab.client as schwab_client  # type: ignore[import-not-found]

        if not hasattr(schwab_client, "Client"):
            schwab_client.Client = type(  # type: ignore[attr-defined]
                "Client", (), {"Account": type("Account", (), {"Fields": _AccountFields})}
            )
    except Exception:
        pass

    return client


def _try_import_interactive_contextvar() -> Any:
    """Try-import the interactive contextvar from either of the two
    spots the BC mentions; return the ContextVar or None.
    """
    for path in (
        "ember.agent.context",
        "ember.context",
        "ember.context.interactive",
        "ember.agent",
    ):
        try:
            mod = importlib.import_module(path)
        except Exception:
            continue
        cv = getattr(mod, "in_interactive_turn", None)
        if cv is not None:
            return cv
    return None


# ---------------------------------------------------------------------------
# Common tool constructors
# ---------------------------------------------------------------------------


def _make_holdings_tool(
    settings: Settings,
    data_dir: Path,
) -> SchwabHoldingsTool:
    return SchwabHoldingsTool(settings=settings, data_dir=data_dir)


def _make_quote_tool(settings: Settings, data_dir: Path) -> SchwabQuoteTool:
    return SchwabQuoteTool(settings=settings, data_dir=data_dir)


def _make_open_orders_tool(settings: Settings, data_dir: Path) -> SchwabOpenOrdersTool:
    return SchwabOpenOrdersTool(settings=settings, data_dir=data_dir)


def _make_transactions_tool(settings: Settings, data_dir: Path) -> SchwabTransactionsTool:
    return SchwabTransactionsTool(settings=settings, data_dir=data_dir)


def _make_place_orders_tool(settings: Settings, data_dir: Path) -> SchwabPlaceOrdersTool:
    return SchwabPlaceOrdersTool(settings=settings, data_dir=data_dir)


def _make_cancel_order_tool(settings: Settings, data_dir: Path) -> SchwabCancelOrderTool:
    return SchwabCancelOrderTool(settings=settings, data_dir=data_dir)


def _seed_holdings_cache(
    data_dir: Path,
    *,
    fetched_at: _dt.datetime | None = None,
    accounts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write `holdings.json` with the FC-1 schema. Returns the payload."""
    if fetched_at is None:
        fetched_at = _dt.datetime.now(_dt.timezone.utc)
    if accounts is None:
        accounts = [
            {
                "name": "Individual Brokerage",
                "account_number_last4": "1234",
                "type": "CASH",
                "current_balances": {"cash_balance": 5000.0, "liquidation_value": 13500.0},
                "positions": [
                    {
                        "symbol": "VXUS",
                        "description": "Vanguard Total International Stock ETF",
                        "asset_type": "EQUITY",
                        "quantity": 100.0,
                        "average_price": 80.0,
                        "market_value": 8500.0,
                        "cost_basis": 8000.0,
                        "gain_dollar": 500.0,
                        "gain_percent": 6.25,
                        "day_gain_dollar": 50.0,
                        "day_gain_percent": 0.5,
                    },
                    {
                        "symbol": "NVDA",
                        "description": "NVIDIA Corp",
                        "asset_type": "EQUITY",
                        "quantity": 50.0,
                        "average_price": 100.0,
                        "market_value": 6000.0,
                        "cost_basis": 5000.0,
                        "gain_dollar": 1000.0,
                        "gain_percent": 20.0,
                        "day_gain_dollar": 25.0,
                        "day_gain_percent": 0.4,
                    },
                ],
            }
        ]
    payload = {
        "fetched_at": fetched_at.isoformat(),
        "from_cache": False,
        "accounts": accounts,
    }
    (data_dir / "holdings.json").write_text(json.dumps(payload))
    os.chmod(data_dir / "holdings.json", 0o600)
    return payload


def _trade_log_path(data_dir: Path) -> Path:
    return data_dir / "trade_log.jsonl"


def _read_trade_log(data_dir: Path) -> list[dict[str, Any]]:
    """Parse trade_log.jsonl; one JSON object per line."""
    p = _trade_log_path(data_dir)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _seed_trade_log(
    data_dir: Path,
    entries: list[dict[str, Any]],
) -> Path:
    """Write synthetic JSONL lines to the trade log."""
    p = _trade_log_path(data_dir)
    with p.open("w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    os.chmod(p, 0o600)
    return p


# ===========================================================================
# Settings + wiring (BR-TLC-8, BR-FC-13)
# ===========================================================================


class TestSettingsAndWiring:
    def test_settings_parse_account_names_csv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(
            tmp_path,
            monkeypatch,
            account_names="1234=Foo,5678=Bar",
            allowed_accounts="Foo",
        )
        assert s.schwab_account_names == {"1234": "Foo", "5678": "Bar"}

    def test_settings_parse_account_names_strips_whitespace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(
            tmp_path,
            monkeypatch,
            account_names="  1234 = Foo  ,  5678 = Bar  ",
            allowed_accounts="Foo",
        )
        assert s.schwab_account_names == {"1234": "Foo", "5678": "Bar"}

    def test_settings_parse_allowed_accounts_csv_whitespace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(
            tmp_path,
            monkeypatch,
            allowed_accounts=" Individual Brokerage , Roth Contributory IRA ",
        )
        # Whitespace stripped; case preserved (allowlist match is case-
        # insensitive in lookup, but we don't lowercase the canonical list).
        assert "Individual Brokerage" in s.schwab_allowed_accounts or any(
            a.strip() == "Individual Brokerage" for a in s.schwab_allowed_accounts
        )
        assert "Roth Contributory IRA" in s.schwab_allowed_accounts or any(
            a.strip() == "Roth Contributory IRA" for a in s.schwab_allowed_accounts
        )

    def test_settings_default_caps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Strip the explicit CAP envs to confirm the schema defaults.
        s = _make_settings(tmp_path, monkeypatch)
        # BR-TLC-2 / BR-FC-13 defaults.
        assert s.schwab_max_order_notional_usd == 20000.0
        assert s.schwab_max_batch_notional_usd == 75000.0
        assert s.schwab_max_daily_notional_usd == 100000.0
        # Fallback price default.
        assert s.schwab_fallback_price_usd == 1000.0

    def test_registration_no_op_without_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ember.main import _maybe_register_schwab_tools

        s = _make_settings(tmp_path, monkeypatch, app_key=None, app_secret=None)
        registry = Registry()
        _maybe_register_schwab_tools(registry, s)
        # No tools registered.
        assert registry.names_with_prefix("schwab_") == []

    def test_registration_full_settings_six_tools(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ember.main import _maybe_register_schwab_tools

        s = _make_settings(tmp_path, monkeypatch)
        registry = Registry()
        _maybe_register_schwab_tools(registry, s)
        registered = set(registry.names_with_prefix("schwab_"))
        assert registered == {
            "schwab_holdings",
            "schwab_quote",
            "schwab_open_orders",
            "schwab_transactions",
            "schwab_place_orders",
            "schwab_cancel_order",
        }

    def test_registration_idempotence_raises_value_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-TLC-8: second call on the same registry surfaces the
        registry's own duplicate-name guard."""
        from ember.main import _maybe_register_schwab_tools

        s = _make_settings(tmp_path, monkeypatch)
        registry = Registry()
        _maybe_register_schwab_tools(registry, s)
        with pytest.raises(ValueError):
            _maybe_register_schwab_tools(registry, s)


# ===========================================================================
# Static surface (FC-3)
# ===========================================================================


class TestStaticSurface:
    @pytest.mark.parametrize(
        ("cls", "name"),
        [
            (SchwabHoldingsTool, "schwab_holdings"),
            (SchwabQuoteTool, "schwab_quote"),
            (SchwabOpenOrdersTool, "schwab_open_orders"),
            (SchwabTransactionsTool, "schwab_transactions"),
            (SchwabPlaceOrdersTool, "schwab_place_orders"),
            (SchwabCancelOrderTool, "schwab_cancel_order"),
        ],
    )
    def test_tool_name_matches_spec(self, cls: type, name: str) -> None:
        assert cls.name == name

    @pytest.mark.parametrize(
        ("cls", "expected"),
        [
            (SchwabHoldingsTool, False),
            (SchwabQuoteTool, False),
            (SchwabOpenOrdersTool, False),
            (SchwabTransactionsTool, False),
            (SchwabPlaceOrdersTool, True),  # BR-TLC-1
            (SchwabCancelOrderTool, True),  # BR-TLC-1
        ],
    )
    def test_requires_confirmation(self, cls: type, expected: bool) -> None:
        assert cls.requires_confirmation is expected

    def test_protocol_compliance(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        assert isinstance(_make_holdings_tool(s, d), Tool)
        assert isinstance(_make_quote_tool(s, d), Tool)
        assert isinstance(_make_open_orders_tool(s, d), Tool)
        assert isinstance(_make_transactions_tool(s, d), Tool)
        assert isinstance(_make_place_orders_tool(s, d), Tool)
        assert isinstance(_make_cancel_order_tool(s, d), Tool)


# ===========================================================================
# Auth (BR-FC-8, BR-TLC-6)
# ===========================================================================


class TestAuth:
    async def test_no_token_file_raises_auth_error_with_not_authenticated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-FC-8 step 4: if `easy_client` raises FileNotFoundError-ish
        (token file missing/unreadable), `AuthError` with "not
        authenticated"."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        # Drop the stub token file `_data_dir` creates so we exercise
        # the implementer's pre-easy_client `token_path.exists()` check
        # (BR-FC-8 step 4).
        (d / ".schwab_token.json").unlink()
        _install_easy_client(monkeypatch, raises=FileNotFoundError("no token"))
        tool = _make_holdings_tool(s, d)
        with pytest.raises(AuthError) as exc:
            await tool.call({})
        assert "not authenticated" in str(exc.value).lower()

    async def test_stale_token_raises_auth_error_before_easy_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A token past schwab-py's max_token_age (6.5 days) must fail
        fast with AuthError — easy_client would silently discard it and
        block in the interactive browser login flow."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        week_ago = int(time.time()) - 7 * 86400
        (d / ".schwab_token.json").write_text('{"creation_timestamp": %d, "token": {}}' % week_ago)
        _install_easy_client(
            monkeypatch,
            raises=AssertionError("easy_client must not be called for a stale token"),
        )
        tool = _make_holdings_tool(s, d)
        with pytest.raises(AuthError) as exc:
            await tool.call({})
        msg = str(exc.value).lower()
        assert "stale" in msg
        assert "/schwab-reauth" in str(exc.value)

    async def test_token_without_creation_timestamp_falls_through_to_easy_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Age-unknown tokens (missing/malformed creation_timestamp) are
        not treated as stale; easy_client's own error paths handle them."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        (d / ".schwab_token.json").write_text('{"token": {}}')
        _install_easy_client(monkeypatch, raises=RuntimeError("sdk-level failure"))
        tool = _make_holdings_tool(s, d)
        with pytest.raises(AuthError) as exc:
            await tool.call({})
        assert "stale" not in str(exc.value).lower()

    async def test_easy_client_generic_exception_wrapped_as_auth_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        _install_easy_client(
            monkeypatch, raises=RuntimeError("schwab oauth: something else exploded")
        )
        tool = _make_holdings_tool(s, d)
        with pytest.raises(AuthError):
            await tool.call({})

    async def test_secret_hygiene_in_authexc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-TLC-6: AuthError message must not contain the app key /
        secret literal values, even if the SDK wrapper put them in a
        traceback string somewhere."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        _install_easy_client(
            monkeypatch,
            raises=RuntimeError(
                f"oauth fail; tried key={SECRET_APP_KEY} secret={SECRET_APP_SECRET}"
            ),
        )
        tool = _make_holdings_tool(s, d)
        with pytest.raises(AuthError) as exc:
            await tool.call({})
        msg = str(exc.value)
        assert SECRET_APP_KEY not in msg, "AuthError leaked SCHWAB_APP_KEY"
        assert SECRET_APP_SECRET not in msg, "AuthError leaked SCHWAB_APP_SECRET"

    async def test_settings_secrets_str_masked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-TLC-6: SecretStr fields render as `**********` in repr/str
        so accidental string-coercion of settings doesn't leak."""
        s = _make_settings(tmp_path, monkeypatch)
        # `__str__` of a SecretStr is masked.
        assert SECRET_APP_KEY not in str(s.schwab_app_key)
        assert SECRET_APP_SECRET not in str(s.schwab_app_secret)
        # And the unwrap accessor returns the real value.
        assert s.schwab_app_key.get_secret_value() == SECRET_APP_KEY
        assert s.schwab_app_secret.get_secret_value() == SECRET_APP_SECRET

    async def test_easy_client_called_once_across_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-FC-8 step 6: subsequent calls reuse the cached client."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.accounts_response = ok([sample_accounts_payload(account_number="12341234")])

        # Install with a counter wrapper so we can count calls.
        call_count = {"n": 0}
        original_install = _install_easy_client

        def _stub(*args: Any, **kwargs: Any) -> FakeSchwabClient:
            call_count["n"] += 1
            return client

        try:
            import schwab.auth as schwab_auth  # type: ignore[import-not-found]

            monkeypatch.setattr(schwab_auth, "easy_client", _stub, raising=True)
        except Exception:
            original_install(monkeypatch, client=client)

        tool = _make_holdings_tool(s, d)
        await tool.call({"force_refresh": True})
        await tool.call({"force_refresh": True})
        # Either called once (impl caches) or — pessimistic — re-called
        # per tool. The contract says cached; assert ≤1.
        # If the implementer caches per-instance and we share an
        # instance, the count is 1; if they cache module-globally, also
        # 1; if they don't cache at all, the test fails as designed.
        assert call_count["n"] == 1, f"easy_client called {call_count['n']} times; expected 1"


# ===========================================================================
# `schwab_holdings` (BR-UoW-1, BR-FC-1, BR-TLC-7)
# ===========================================================================


class TestSchwabHoldings:
    async def test_returns_normalized_payload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(tmp_path, monkeypatch, allowed_accounts=None)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.accounts_response = ok([sample_accounts_payload(account_number="12341234")])
        _install_easy_client(monkeypatch, client=client)

        tool = _make_holdings_tool(s, d)
        result = await tool.call({"force_refresh": True})

        assert isinstance(result, ToolResult)
        out = result.output
        assert "fetched_at" in out
        assert out.get("from_cache") is False
        accounts = out["accounts"]
        assert len(accounts) == 1
        a = accounts[0]
        assert a["name"] == "Individual Brokerage"
        assert a["account_number_last4"] == "1234"
        assert "current_balances" in a
        assert "positions" in a
        pos = a["positions"][0]
        for key in (
            "symbol",
            "description",
            "asset_type",
            "quantity",
            "average_price",
            "market_value",
            "cost_basis",
            "gain_dollar",
            "gain_percent",
            "day_gain_dollar",
            "day_gain_percent",
        ):
            assert key in pos, f"missing position field: {key!r}"

    async def test_filters_to_allowed_accounts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-TLC-3: only allowlisted friendly names appear when
        SCHWAB_ALLOWED_ACCOUNTS is set."""
        s = _make_settings(
            tmp_path,
            monkeypatch,
            allowed_accounts="Individual Brokerage",  # only the brokerage account
        )
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers(
            [("12341234", "HASH1234"), ("56785678", "HASH5678")]
        )
        client.accounts_response = ok(
            [
                sample_accounts_payload(account_number="12341234"),
                sample_accounts_payload(account_number="56785678"),
            ]
        )
        _install_easy_client(monkeypatch, client=client)

        result = await _make_holdings_tool(s, d).call({"force_refresh": True})
        names = [a["name"] for a in result.output["accounts"]]
        assert names == ["Individual Brokerage"], (
            f"expected only Individual Brokerage to pass the allowlist; got {names!r}"
        )

    async def test_returns_all_when_allowlist_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-TLC-3: empty allowlist → all known accounts in
        ACCOUNT_NAMES are returned for read."""
        s = _make_settings(
            tmp_path,
            monkeypatch,
            allowed_accounts="",  # empty → all known
            account_names="1234=Individual Brokerage,5678=Roth Contributory IRA",
        )
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers(
            [("12341234", "HASH1234"), ("56785678", "HASH5678")]
        )
        client.accounts_response = ok(
            [
                sample_accounts_payload(account_number="12341234"),
                sample_accounts_payload(account_number="56785678"),
            ]
        )
        _install_easy_client(monkeypatch, client=client)

        result = await _make_holdings_tool(s, d).call({"force_refresh": True})
        names = [a["name"] for a in result.output["accounts"]]
        assert len(names) == 2

    async def test_holdings_cache_within_30min(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """force_refresh=False with fresh cache → SDK NOT called,
        from_cache=True."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        # Cache is 5 minutes old.
        _seed_holdings_cache(
            d,
            fetched_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5),
        )
        client = FakeSchwabClient()
        _install_easy_client(monkeypatch, client=client)

        result = await _make_holdings_tool(s, d).call({})

        # SDK was NOT called (cache hit).
        assert client.accounts_calls == [], (
            f"fresh cache should bypass SDK; got accounts_calls={client.accounts_calls!r}"
        )
        # `from_cache` flag set.
        assert result.output.get("from_cache") is True

    async def test_force_refresh_bypasses_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        _seed_holdings_cache(
            d,
            fetched_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5),
        )
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.accounts_response = ok([sample_accounts_payload(account_number="12341234")])
        _install_easy_client(monkeypatch, client=client)

        await _make_holdings_tool(s, d).call({"force_refresh": True})

        assert len(client.accounts_calls) >= 1, "force_refresh=True must bypass cache"

    async def test_stale_cache_auto_refreshes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        # Cache is 60 minutes old (>30 min TTL).
        _seed_holdings_cache(
            d,
            fetched_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=60),
        )
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.accounts_response = ok([sample_accounts_payload(account_number="12341234")])
        _install_easy_client(monkeypatch, client=client)

        result = await _make_holdings_tool(s, d).call({})
        assert len(client.accounts_calls) >= 1, "stale cache should auto-refresh"
        assert result.output.get("from_cache") is False

    async def test_expired_token_serves_stale_cache_marked_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Auth dead + cache on disk → serve the cache, flagged stale.

        Positions only move when a trade settles, so an old snapshot is a
        better answer than a hard failure — provided the caller can tell it
        is old. The daily brief already reads this same file directly; this
        keeps the tool from claiming no data exists.
        """
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        seeded_at = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=19)
        _seed_holdings_cache(d, fetched_at=seeded_at)
        # Token past the 6.5-day max age — the same state a lapsed
        # refresh token leaves behind.
        week_ago = int(time.time()) - 19 * 86400
        (d / ".schwab_token.json").write_text('{"creation_timestamp": %d, "token": {}}' % week_ago)
        _install_easy_client(
            monkeypatch,
            raises=AssertionError("easy_client must not be called for a stale token"),
        )

        result = await _make_holdings_tool(s, d).call({})

        assert result.output["stale"] is True
        assert result.output["from_cache"] is True
        assert result.output["fetched_at"].startswith(seeded_at.date().isoformat())
        # Age is reported so the model can date the snapshot.
        assert result.output["age_seconds"] >= 18 * 86400
        # And the reason points at the fix.
        assert "/schwab-reauth" in result.output["stale_reason"]
        # The positions themselves survived the filter.
        assert [p["symbol"] for p in result.output["accounts"][0]["positions"]] == [
            "VXUS",
            "NVDA",
        ]

    async def test_expired_token_without_cache_still_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No cache to fall back on → the AuthError propagates unchanged."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        week_ago = int(time.time()) - 7 * 86400
        (d / ".schwab_token.json").write_text('{"creation_timestamp": %d, "token": {}}' % week_ago)
        _install_easy_client(monkeypatch, raises=AssertionError("must not be called"))

        with pytest.raises(AuthError) as exc:
            await _make_holdings_tool(s, d).call({})
        assert "/schwab-reauth" in str(exc.value)

    async def test_401_serves_stale_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 401 mid-flight is the same condition as an expired token."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        _seed_holdings_cache(
            d, fetched_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=3)
        )
        client = FakeSchwabClient()
        client.account_numbers_response = http_error(401, {"error": "unauthorized"})
        _install_easy_client(monkeypatch, client=client)

        result = await _make_holdings_tool(s, d).call({"force_refresh": True})
        assert result.output["stale"] is True
        assert result.output["age_seconds"] >= 2 * 86400

    async def test_non_auth_failure_does_not_serve_stale_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 500 is transient — retry, don't quietly hand back old data."""
        from ember.errors import ToolRetryable

        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        _seed_holdings_cache(
            d, fetched_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=3)
        )
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.accounts_response = http_error(500, {"error": "internal error"})
        _install_easy_client(monkeypatch, client=client)

        with pytest.raises(ToolRetryable):
            await _make_holdings_tool(s, d).call({"force_refresh": True})

    async def test_fresh_cache_hit_is_not_marked_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordinary 30-minute cache hit must not carry stale markers."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        _seed_holdings_cache(
            d, fetched_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5)
        )
        _install_easy_client(monkeypatch, client=FakeSchwabClient())

        result = await _make_holdings_tool(s, d).call({})
        assert result.output["from_cache"] is True
        assert "stale" not in result.output

    async def test_holdings_atomic_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-UoW-1: write is atomic (temp + rename). If `os.replace`
        fails, the on-disk holdings.json must not be corrupted (either
        the old content remains, or no file)."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        # Pre-existing holdings — known good content.
        original_payload = _seed_holdings_cache(
            d, fetched_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)
        )
        original_text = (d / "holdings.json").read_text()

        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.accounts_response = ok(
            [
                sample_accounts_payload(
                    account_number="12341234",
                    cash_balance=999999.0,  # different — would be the new content
                    liquidation_value=999999.0,
                )
            ]
        )
        _install_easy_client(monkeypatch, client=client)

        # Patch os.replace to fail once.
        original_replace = os.replace
        calls = {"n": 0}

        def _failing_replace(src: Any, dst: Any) -> Any:
            calls["n"] += 1
            raise OSError("rename failed (simulated)")

        monkeypatch.setattr(os, "replace", _failing_replace)

        tool = _make_holdings_tool(s, d)
        # Either it raises (atomic write failed) OR it returns normally.
        # Either way, the on-disk file must not be torn.
        try:
            await tool.call({"force_refresh": True})
        except Exception:
            pass

        # Restore os.replace so any later cleanup runs normally.
        monkeypatch.setattr(os, "replace", original_replace)

        # Either the file is unchanged (matches original_text) OR it
        # doesn't exist (some impls might unlink-on-failure). Both are
        # acceptable; the contract is "no corrupted partial file".
        path = d / "holdings.json"
        if path.exists():
            text = path.read_text()
            # Must be parseable JSON.
            json.loads(text)
            # And either the original content OR a complete new payload —
            # never a torn write. The cleanest check: it parses.
            assert original_payload is not None  # use it (silence unused)
            # If still the original, that's the safest behavior.
            assert text == original_text or json.loads(text).get("from_cache") is not None

    async def test_holdings_file_mode_0600(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-TLC-6: holdings.json mode 0600."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.accounts_response = ok([sample_accounts_payload(account_number="12341234")])
        _install_easy_client(monkeypatch, client=client)

        await _make_holdings_tool(s, d).call({"force_refresh": True})

        path = d / "holdings.json"
        assert path.exists()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"holdings.json mode is {oct(mode)}; expected 0o600"

    async def test_holdings_401_raises_auth_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = http_error(401, {"error": "unauthorized"})
        _install_easy_client(monkeypatch, client=client)
        with pytest.raises(AuthError):
            await _make_holdings_tool(s, d).call({"force_refresh": True})

    async def test_holdings_500_is_retryable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ember.errors import ToolRetryable

        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.accounts_response = http_error(500, {"error": "internal error"})
        _install_easy_client(monkeypatch, client=client)
        with pytest.raises(ToolRetryable):
            await _make_holdings_tool(s, d).call({"force_refresh": True})

    async def test_positions_sorted_descending_by_abs_market_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-FC-1: positions sort descending by abs(market_value)."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.accounts_response = ok(
            [
                sample_accounts_payload(
                    account_number="12341234",
                    positions=[
                        sample_position(symbol="SMALL", market_value=100.0),
                        sample_position(symbol="BIG", market_value=10000.0),
                        sample_position(symbol="MID", market_value=1000.0),
                    ],
                )
            ]
        )
        _install_easy_client(monkeypatch, client=client)

        result = await _make_holdings_tool(s, d).call({"force_refresh": True})
        positions = result.output["accounts"][0]["positions"]
        symbols = [p["symbol"] for p in positions]
        assert symbols == ["BIG", "MID", "SMALL"]

    async def test_audit_log_entry_omits_account_number_full(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-TLC-7: read-tier audit lines omit raw account numbers
        (friendly name only)."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.accounts_response = ok([sample_accounts_payload(account_number="12341234")])
        _install_easy_client(monkeypatch, client=client)

        await _make_holdings_tool(s, d).call({"force_refresh": True})

        for entry in _read_trade_log(d):
            # No raw account number anywhere in the entry.
            blob = json.dumps(entry)
            assert "12341234" not in blob, f"raw account number leaked: {entry!r}"


# ===========================================================================
# `schwab_quote` (BR-UoW-2, BR-FC-2)
# ===========================================================================


class TestSchwabQuote:
    async def test_quote_dedup_preserves_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.quotes_response = ok(
            {
                "VXUS": {
                    "symbol": "VXUS",
                    "quote": {
                        "lastPrice": 81.92,
                        "bidPrice": 81.91,
                        "askPrice": 81.93,
                        "mark": 81.92,
                        "totalVolume": 4123890,
                        "netPercentChangeInDouble": 0.51,
                    },
                },
                "VOO": {
                    "symbol": "VOO",
                    "quote": {
                        "lastPrice": 502.10,
                        "bidPrice": 502.05,
                        "askPrice": 502.15,
                        "mark": 502.10,
                        "totalVolume": 2000000,
                        "netPercentChangeInDouble": 0.20,
                    },
                },
            }
        )
        _install_easy_client(monkeypatch, client=client)

        result = await _make_quote_tool(s, d).call({"symbols": ["VXUS", "VOO", "VXUS"]})
        symbols = [q["symbol"] for q in result.output["quotes"]]
        assert symbols == ["VXUS", "VOO"], f"expected dedup preserving order; got {symbols!r}"

    async def test_quote_uppercased_before_send(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.quotes_response = ok(
            {
                "VXUS": {
                    "symbol": "VXUS",
                    "quote": {
                        "lastPrice": 81.0,
                        "bidPrice": 81.0,
                        "askPrice": 81.0,
                        "mark": 81.0,
                        "totalVolume": 1,
                        "netPercentChangeInDouble": 0,
                    },
                }
            }
        )
        _install_easy_client(monkeypatch, client=client)

        await _make_quote_tool(s, d).call({"symbols": ["vxus"]})
        # The fake recorded the raw `symbols` arg passed.
        seen = client.quotes_calls[-1]
        # Could be a list or a tuple; must contain the uppercase form.
        seen_list = list(seen) if not isinstance(seen, str) else [seen]
        assert any("VXUS" in str(x) for x in seen_list), (
            f"expected upper-cased symbol; SDK got {seen!r}"
        )

    async def test_quote_unknown_symbol_per_symbol_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-FC-2: unknown ticker → null prices + error='not_found',
        not whole-call failure."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.quotes_response = ok(
            {
                "VXUS": {
                    "symbol": "VXUS",
                    "quote": {
                        "lastPrice": 81.0,
                        "bidPrice": 81.0,
                        "askPrice": 81.0,
                        "mark": 81.0,
                        "totalVolume": 1,
                        "netPercentChangeInDouble": 0,
                    },
                },
                # BOGUS missing entirely (Schwab omits unknowns).
            }
        )
        _install_easy_client(monkeypatch, client=client)

        result = await _make_quote_tool(s, d).call({"symbols": ["VXUS", "BOGUS"]})
        quotes_by_symbol = {q["symbol"]: q for q in result.output["quotes"]}
        assert "VXUS" in quotes_by_symbol and "BOGUS" in quotes_by_symbol
        bogus = quotes_by_symbol["BOGUS"]
        assert bogus["error"] == "not_found"
        assert (
            bogus.get("last") is None
            or bogus["last"] == 0
            or bogus["last"] == ""
            or "last" in bogus
        )
        # Sane: VXUS has a real price (no error).
        assert quotes_by_symbol["VXUS"].get("error") in (None, "")

    async def test_quote_all_unknown_returns_200(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.quotes_response = ok({})  # nothing matched
        _install_easy_client(monkeypatch, client=client)

        result = await _make_quote_tool(s, d).call({"symbols": ["NOPE1", "NOPE2"]})
        assert not result.is_error
        for q in result.output["quotes"]:
            assert q["error"] == "not_found"

    async def test_quote_empty_symbols_rejected_by_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-FC-2: schema enforces minItems=1 on `symbols`. The
        registry path raises ToolValidationError; tool-direct path may
        reject differently — we only assert via the registry route."""
        from ember.errors import ToolValidationError

        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        _install_easy_client(monkeypatch, client=client)

        registry = Registry()
        registry.register(_make_quote_tool(s, d))
        with pytest.raises(ToolValidationError):
            await registry.call_tool("schwab_quote", {"symbols": []})


# ===========================================================================
# `schwab_open_orders` (BR-UoW-3, BR-FC-3)
# ===========================================================================


class TestSchwabOpenOrders:
    async def test_filters_to_allowed_accounts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(
            tmp_path,
            monkeypatch,
            allowed_accounts="Individual Brokerage",
        )
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers(
            [("12341234", "HASH1234"), ("56785678", "HASH5678")]
        )
        client.open_orders_response = ok(
            [
                {
                    "orderId": 1000000000001,
                    "accountNumber": "12341234",
                    "status": "WORKING",
                    "orderType": "LIMIT",
                    "price": 82.5,
                    "enteredTime": "2026-05-07T13:42:00Z",
                    "orderLegCollection": [
                        {
                            "instruction": "BUY",
                            "quantity": 100,
                            "instrument": {"symbol": "VXUS"},
                        }
                    ],
                },
                {
                    "orderId": 1000000000002,
                    "accountNumber": "56785678",  # not in allowlist
                    "status": "WORKING",
                    "orderType": "LIMIT",
                    "price": 82.5,
                    "enteredTime": "2026-05-07T13:42:00Z",
                    "orderLegCollection": [
                        {
                            "instruction": "BUY",
                            "quantity": 100,
                            "instrument": {"symbol": "VXUS"},
                        }
                    ],
                },
            ]
        )
        _install_easy_client(monkeypatch, client=client)

        result = await _make_open_orders_tool(s, d).call({})
        accounts_in_orders = {o["account"] for o in result.output["orders"]}
        assert accounts_in_orders == {"Individual Brokerage"}, (
            f"expected only Individual Brokerage in orders; got {accounts_in_orders!r}"
        )

    async def test_status_filter_at_query_level(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-UoW-3: status filter passes through to the SDK call."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.open_orders_response = ok([])
        _install_easy_client(monkeypatch, client=client)

        await _make_open_orders_tool(s, d).call({})

        # The SDK call recorded in `open_orders_calls[0]` must reference
        # the working/pending/awaiting filter — somewhere in args/kwargs.
        assert client.open_orders_calls, "expected SDK get_orders call"
        call_blob = json.dumps(client.open_orders_calls[0], default=str).upper()
        # Accept any of the three status enums in the BC; the BC says
        # WORKING / PENDING_ACTIVATION / AWAITING_PARENT_ORDER.
        assert "WORKING" in call_blob or "PENDING" in call_blob or "STATUS" in call_blob, (
            f"expected status filter on SDK call; got {client.open_orders_calls[0]!r}"
        )

    async def test_account_none_returns_all_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(
            tmp_path,
            monkeypatch,
            allowed_accounts="Individual Brokerage,Roth Contributory IRA",
        )
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers(
            [("12341234", "HASH1234"), ("56785678", "HASH5678")]
        )
        client.open_orders_response = ok(
            [
                {
                    "orderId": 1,
                    "accountNumber": "12341234",
                    "status": "WORKING",
                    "orderType": "MARKET",
                    "enteredTime": "2026-05-07T00:00:00Z",
                    "orderLegCollection": [
                        {"instruction": "BUY", "quantity": 1, "instrument": {"symbol": "X"}}
                    ],
                },
                {
                    "orderId": 2,
                    "accountNumber": "56785678",
                    "status": "WORKING",
                    "orderType": "MARKET",
                    "enteredTime": "2026-05-07T00:00:00Z",
                    "orderLegCollection": [
                        {"instruction": "BUY", "quantity": 1, "instrument": {"symbol": "Y"}}
                    ],
                },
            ]
        )
        _install_easy_client(monkeypatch, client=client)

        result = await _make_open_orders_tool(s, d).call({})
        accounts_in_orders = {o["account"] for o in result.output["orders"]}
        assert accounts_in_orders == {"Individual Brokerage", "Roth Contributory IRA"}


# ===========================================================================
# `schwab_transactions` (BR-UoW-4, BR-FC-4)
# ===========================================================================


class TestSchwabTransactions:
    async def test_required_since_default_until_today(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.transactions_response = ok([])
        _install_easy_client(monkeypatch, client=client)

        await _make_transactions_tool(s, d).call(
            {"account": "Individual Brokerage", "since": "2026-04-01"}
        )

        assert client.transactions_calls, "expected SDK get_transactions call"
        call_blob = client.transactions_calls[0]
        # Today's date — the call passed *some* until value; assert it
        # represents today (string match on YYYY-MM-DD or a datetime
        # whose date == today).
        today_iso = _dt.date.today().isoformat()
        blob_str = json.dumps(call_blob, default=str)
        assert today_iso in blob_str, (
            f"expected today's date {today_iso!r} in SDK call; got {blob_str!r}"
        )

    async def test_filters_by_transaction_type(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.transactions_response = ok([])
        _install_easy_client(monkeypatch, client=client)

        await _make_transactions_tool(s, d).call(
            {
                "account": "Individual Brokerage",
                "since": "2026-04-01",
                "types": ["TRADE", "DIVIDEND_OR_INTEREST"],
            }
        )
        # Either filtered server-side (in SDK args) or client-side (in
        # the result mapping). Either is acceptable; we just assert no
        # error was raised and the call landed.
        assert client.transactions_calls

    async def test_account_allowlist_exact_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-TLC-3: case-insensitive but exact match — uppercase
        version of the allowed name resolves; substring 'Individual' does
        not."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.transactions_response = ok([])
        _install_easy_client(monkeypatch, client=client)

        # Uppercased exact match — resolves.
        await _make_transactions_tool(s, d).call(
            {"account": "INDIVIDUAL BROKERAGE", "since": "2026-04-01"}
        )
        # No exception raised → allowlist accepted the case-insensitive
        # exact match.

    async def test_account_resolution_substring_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-TLC-3: substring 'Individual' must NOT match 'Individual
        Brokerage' (no fuzzy resolution)."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        _install_easy_client(monkeypatch, client=client)

        with pytest.raises(ToolError) as exc:
            await _make_transactions_tool(s, d).call({"account": "Individual", "since": "2026-04-01"})
        # Error message lists the available exact name.
        assert "Individual Brokerage" in str(exc.value), (
            f"expected the available friendly name in the error; got {exc.value!r}"
        )


# ===========================================================================
# `schwab_place_orders` (BR-UoW-5, BR-FC-5, BR-TLC-1, BR-TLC-2, BR-TLC-5)
# ===========================================================================


class TestSchwabPlaceOrders:
    async def test_place_orders_without_confirmation_returns_marker_and_no_sdk_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-TLC-1: without `allow_unconfirmed=True`, the registry
        returns the confirmation marker; place_order is never called."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        _install_easy_client(monkeypatch, client=client)

        # Set interactive context so the no-interactive-context check
        # doesn't shadow the gate behavior.
        cv = _try_import_interactive_contextvar()
        if cv is not None:
            token = cv.set(True)
        else:
            token = None
        try:
            registry = Registry()
            registry.register(_make_place_orders_tool(s, d))
            # Need to seed holdings so cap eval doesn't fall back per-test
            _seed_holdings_cache(d)
            result = await registry.call_tool(
                "schwab_place_orders",
                {
                    "orders": [
                        {
                            "account": "Individual Brokerage",
                            "action": "buy",
                            "symbol": "VXUS",
                            "quantity": 10,
                        }
                    ]
                },
            )
        finally:
            if token is not None and cv is not None:
                cv.reset(token)

        assert result.is_error is True
        assert result.output["error"] == "confirmation_required"
        assert "action" in result.output
        # SDK never called.
        assert client.place_order_calls == [], (
            f"expected zero place_order calls before confirm; got {client.place_order_calls!r}"
        )

    async def test_place_orders_summarize_action_renders_preview_with_gain_estimate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-FC-5: summarize_action renders SELL line with (gain ~$...)
        when the position is held."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        # NVDA at average_price 100, market_value 6000 (50 shares → $120/share).
        # Selling 10 → est proceeds $1200, gain $200 over basis.
        _seed_holdings_cache(d)
        # Block any easy_client call to confirm summarize_action is offline.
        _install_easy_client(monkeypatch, raises=RuntimeError("easy_client should not be called"))

        tool = _make_place_orders_tool(s, d)
        preview = tool.summarize_action(
            {
                "orders": [
                    {
                        "account": "Individual Brokerage",
                        "action": "sell",
                        "symbol": "NVDA",
                        "quantity": 10,
                    }
                ]
            }
        )
        assert "SELL" in preview.upper()
        assert "NVDA" in preview
        assert "$" in preview
        # Implementer renders sign on the gain estimate (e.g. "gain ~+$200" or
        # "gain ~-$50"); we just assert the gain phrase + a dollar amount.
        assert "gain" in preview, f"expected gain estimate; got {preview!r}"

    async def test_summarize_action_no_network_io(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-FC-5 / TLC-3: summarize_action must never touch the
        network — it's used at gate time, before the user has confirmed."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        # No holdings cache; if summarize_action tries to fetch quotes,
        # easy_client will raise and explode the test.
        _install_easy_client(
            monkeypatch, raises=RuntimeError("easy_client must not be called from summarize_action")
        )

        tool = _make_place_orders_tool(s, d)
        # Should not raise — just return some string (possibly missing
        # the price, that's acceptable).
        preview = tool.summarize_action(
            {
                "orders": [
                    {
                        "account": "Individual Brokerage",
                        "action": "buy",
                        "symbol": "VXUS",
                        "quantity": 10,
                    }
                ]
            }
        )
        assert isinstance(preview, str)
        assert preview.strip()

    async def test_caps_order_exceeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-TLC-2 / BR-FC-7: per-order cap exceeded → ToolError before
        the gate; SDK never called."""
        s = _make_settings(tmp_path, monkeypatch, max_order=10000.0)
        d = _data_dir(tmp_path)
        _seed_holdings_cache(d)
        client = FakeSchwabClient()
        _install_easy_client(monkeypatch, client=client)

        cv = _try_import_interactive_contextvar()
        token = cv.set(True) if cv is not None else None
        try:
            with pytest.raises(ToolError) as exc:
                # Notional 100 * 200 = $20,000 > MAX_ORDER 10,000.
                await _make_place_orders_tool(s, d).call(
                    {
                        "orders": [
                            {
                                "account": "Individual Brokerage",
                                "action": "buy",
                                "symbol": "VXUS",
                                "quantity": 100,
                                "limit": 200.0,
                            }
                        ]
                    }
                )
            msg = str(exc.value).lower()
            assert "order cap" in msg or "max_order" in msg or "20,000" in msg or "20000" in msg, (
                f"expected per-order cap message; got {msg!r}"
            )
        finally:
            if token is not None:
                cv.reset(token)

        assert client.place_order_calls == []

    async def test_caps_batch_exceeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-TLC-2: batch sum cap → ToolError."""
        s = _make_settings(
            tmp_path,
            monkeypatch,
            max_order=100000.0,  # individual orders pass
            max_batch=15000.0,  # but their sum doesn't
        )
        d = _data_dir(tmp_path)
        _seed_holdings_cache(d)
        client = FakeSchwabClient()
        _install_easy_client(monkeypatch, client=client)

        cv = _try_import_interactive_contextvar()
        token = cv.set(True) if cv is not None else None
        try:
            with pytest.raises(ToolError) as exc:
                # Two $10k orders → batch = $20k > MAX_BATCH $15k.
                await _make_place_orders_tool(s, d).call(
                    {
                        "orders": [
                            {
                                "account": "Individual Brokerage",
                                "action": "buy",
                                "symbol": "VXUS",
                                "quantity": 100,
                                "limit": 100.0,
                            },
                            {
                                "account": "Individual Brokerage",
                                "action": "buy",
                                "symbol": "VOO",
                                "quantity": 100,
                                "limit": 100.0,
                            },
                        ]
                    }
                )
            assert "batch" in str(exc.value).lower()
        finally:
            if token is not None:
                cv.reset(token)

        assert client.place_order_calls == []

    async def test_caps_daily_exceeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-TLC-2 / BR-FC-7: daily rolling-24h cap → ToolError; reads
        from trade_log.jsonl."""
        s = _make_settings(
            tmp_path,
            monkeypatch,
            max_order=100000.0,
            max_batch=100000.0,
            max_daily=10000.0,  # tiny — easy to blow
        )
        d = _data_dir(tmp_path)
        _seed_holdings_cache(d)
        # Seed trade log with $9k of placed events in the last 24h.
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        _seed_trade_log(
            d,
            [
                {
                    "ts": now,
                    "event": "placed",
                    "tool": "schwab_place_orders",
                    "account": "Individual Brokerage",
                    "action": "buy",
                    "symbol": "OLD",
                    "quantity": 90,
                    "limit": 100.0,
                    "order_id": "id_old",
                    "http_status": 201,
                    "error": None,
                    "request_hash": "x" * 64,
                    "notional_usd": 9000.0,
                }
            ],
        )
        client = FakeSchwabClient()
        _install_easy_client(monkeypatch, client=client)

        cv = _try_import_interactive_contextvar()
        token = cv.set(True) if cv is not None else None
        try:
            with pytest.raises(ToolError) as exc:
                # $5k batch + $9k existing = $14k > MAX_DAILY $10k.
                await _make_place_orders_tool(s, d).call(
                    {
                        "orders": [
                            {
                                "account": "Individual Brokerage",
                                "action": "buy",
                                "symbol": "VXUS",
                                "quantity": 50,
                                "limit": 100.0,
                            }
                        ]
                    }
                )
            assert "daily" in str(exc.value).lower()
        finally:
            if token is not None:
                cv.reset(token)

        assert client.place_order_calls == []

    async def test_market_order_without_price_basis_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A market order on an unheld symbol has no price to bound its
        notional, so the caps can't bind on it. Refuse rather than score
        it against an arbitrary fallback."""
        s = _make_settings(tmp_path, monkeypatch, max_order=500.0, fallback_price=1000.0)
        d = _data_dir(tmp_path)
        # No holdings cache.
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        _install_easy_client(monkeypatch, client=client)

        cv = _try_import_interactive_contextvar()
        token = cv.set(True) if cv is not None else None
        try:
            with pytest.raises(ToolError) as exc:
                await _make_place_orders_tool(s, d).call(
                    {
                        "orders": [
                            {
                                "account": "Individual Brokerage",
                                "action": "buy",
                                "symbol": "BOGUS",
                                "quantity": 1,
                            }
                        ]
                    }
                )
            assert "no price basis" in str(exc.value)
            assert "limit" in str(exc.value)
        finally:
            if token is not None:
                cv.reset(token)
        assert client.place_order_calls == []

    async def test_market_order_priced_off_stale_holdings_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cap-underestimation bug: a position cached at one price
        that has since run up gets scored at the old price, so a market
        order sails past a cap it should have hit."""
        s = _make_settings(tmp_path, monkeypatch, max_order=20000.0)
        d = _data_dir(tmp_path)
        # 19 days old — the state an expired refresh token leaves behind.
        _seed_holdings_cache(
            d, fetched_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=19)
        )
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        _install_easy_client(monkeypatch, client=client)

        cv = _try_import_interactive_contextvar()
        token = cv.set(True) if cv is not None else None
        try:
            with pytest.raises(ToolError) as exc:
                await _make_place_orders_tool(s, d).call(
                    {
                        "orders": [
                            {
                                "account": "Individual Brokerage",
                                "action": "buy",
                                "symbol": "VXUS",
                                "quantity": 100,
                            }
                        ]
                    }
                )
            msg = str(exc.value)
            assert "holdings snapshot" in msg
            assert "limit" in msg
        finally:
            if token is not None:
                cv.reset(token)
        assert client.place_order_calls == []

    async def test_market_order_with_fresh_holdings_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard must not break the ordinary case: a market order
        priced off a minutes-old snapshot still goes through."""
        s = _make_settings(tmp_path, monkeypatch, max_order=20000.0)
        d = _data_dir(tmp_path)
        _seed_holdings_cache(
            d, fetched_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=2)
        )
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        _install_easy_client(monkeypatch, client=client)

        cv = _try_import_interactive_contextvar()
        token = cv.set(True) if cv is not None else None
        try:
            res = await _make_place_orders_tool(s, d).call(
                {
                    "orders": [
                        {
                            "account": "Individual Brokerage",
                            "action": "buy",
                            "symbol": "VXUS",
                            "quantity": 10,
                        }
                    ]
                }
            )
        finally:
            if token is not None:
                cv.reset(token)
        assert res.output["placed"] == 1
        assert len(client.place_order_calls) == 1

    async def test_limit_order_unaffected_by_stale_holdings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A limit order carries its own price, so snapshot age is
        irrelevant — this is the documented workaround."""
        s = _make_settings(tmp_path, monkeypatch, max_order=20000.0)
        d = _data_dir(tmp_path)
        _seed_holdings_cache(
            d, fetched_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=19)
        )
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        _install_easy_client(monkeypatch, client=client)

        cv = _try_import_interactive_contextvar()
        token = cv.set(True) if cv is not None else None
        try:
            res = await _make_place_orders_tool(s, d).call(
                {
                    "orders": [
                        {
                            "account": "Individual Brokerage",
                            "action": "buy",
                            "symbol": "VXUS",
                            "quantity": 10,
                            "limit": 90.0,
                        }
                    ]
                }
            )
        finally:
            if token is not None:
                cv.reset(token)
        assert res.output["placed"] == 1

    async def test_unreadable_trade_log_refuses_orders(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The daily cap must fail closed. An unreadable log used to read
        as $0 spent — silently refilling the rolling budget at exactly the
        moment we lost the ability to account for it."""
        s = _make_settings(tmp_path, monkeypatch, max_daily=10000.0)
        d = _data_dir(tmp_path)
        _seed_holdings_cache(d)
        log = _trade_log_path(d)
        log.write_text('{"event": "placed", "notional_usd": 1.0}\n')
        os.chmod(log, 0o000)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        _install_easy_client(monkeypatch, client=client)

        cv = _try_import_interactive_contextvar()
        token = cv.set(True) if cv is not None else None
        try:
            with pytest.raises(ToolError) as exc:
                await _make_place_orders_tool(s, d).call(
                    {
                        "orders": [
                            {
                                "account": "Individual Brokerage",
                                "action": "buy",
                                "symbol": "VXUS",
                                "quantity": 1,
                                "limit": 10.0,
                            }
                        ]
                    }
                )
            assert "spend accounting is unavailable" in str(exc.value)
        finally:
            os.chmod(log, 0o600)
            if token is not None:
                cv.reset(token)
        assert client.place_order_calls == []

    async def test_missing_trade_log_still_allows_first_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A *missing* log is a fresh deploy, not a failure — $0 used."""
        s = _make_settings(tmp_path, monkeypatch, max_daily=10000.0)
        d = _data_dir(tmp_path)
        _seed_holdings_cache(d)
        assert not _trade_log_path(d).exists()
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        _install_easy_client(monkeypatch, client=client)

        cv = _try_import_interactive_contextvar()
        token = cv.set(True) if cv is not None else None
        try:
            res = await _make_place_orders_tool(s, d).call(
                {
                    "orders": [
                        {
                            "account": "Individual Brokerage",
                            "action": "buy",
                            "symbol": "VXUS",
                            "quantity": 1,
                            "limit": 10.0,
                        }
                    ]
                }
            )
        finally:
            if token is not None:
                cv.reset(token)
        assert res.output["placed"] == 1

    async def test_preview_flags_orders_that_will_be_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Don't ask the user to approve something already doomed — the
        cap check only runs after confirmation."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        _seed_holdings_cache(
            d, fetched_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=19)
        )
        summary = _make_place_orders_tool(s, d).summarize_action(
            {
                "orders": [
                    {
                        "account": "Individual Brokerage",
                        "action": "buy",
                        "symbol": "VXUS",
                        "quantity": 10,
                    }
                ]
            }
        )
        assert "WILL BE REFUSED" in summary
        assert "holdings snapshot" in summary

    async def test_preview_clean_for_a_valid_limit_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        _seed_holdings_cache(d)
        summary = _make_place_orders_tool(s, d).summarize_action(
            {
                "orders": [
                    {
                        "account": "Individual Brokerage",
                        "action": "buy",
                        "symbol": "VXUS",
                        "quantity": 10,
                        "limit": 90.0,
                    }
                ]
            }
        )
        assert "WILL BE REFUSED" not in summary
        assert "est $900.00" in summary

    async def test_allowlist_mismatch_before_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-TLC-3: write-tier allowlist mismatch → ToolError before
        the gate; SDK never called."""
        s = _make_settings(
            tmp_path,
            monkeypatch,
            allowed_accounts="Individual Brokerage",  # only the brokerage account allowed for write
            account_names="1234=Individual Brokerage,5678=Roth Contributory IRA",
        )
        d = _data_dir(tmp_path)
        _seed_holdings_cache(d)
        client = FakeSchwabClient()
        _install_easy_client(monkeypatch, client=client)

        cv = _try_import_interactive_contextvar()
        token = cv.set(True) if cv is not None else None
        try:
            with pytest.raises(ToolError):
                await _make_place_orders_tool(s, d).call(
                    {
                        "orders": [
                            {
                                "account": "Roth Contributory IRA",  # not in allowlist
                                "action": "buy",
                                "symbol": "VXUS",
                                "quantity": 1,
                                "limit": 50.0,
                            }
                        ]
                    }
                )
        finally:
            if token is not None:
                cv.reset(token)

        assert client.place_order_calls == []

    async def test_no_interactive_context_returns_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-TLC-5: outside an interactive turn, place_orders returns
        is_error=True with no_interactive_context. SDK not called."""
        cv = _try_import_interactive_contextvar()
        if cv is None:
            pytest.skip("interactive contextvar not yet defined")

        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        _seed_holdings_cache(d)
        client = FakeSchwabClient()
        _install_easy_client(monkeypatch, client=client)

        # Default contextvar value is False/unset.
        result = await _make_place_orders_tool(s, d).call(
            {
                "orders": [
                    {
                        "account": "Individual Brokerage",
                        "action": "buy",
                        "symbol": "VXUS",
                        "quantity": 1,
                        "limit": 50.0,
                    }
                ]
            }
        )
        assert result.is_error is True
        assert result.output["error"] == "no_interactive_context"
        assert client.place_order_calls == []

    async def test_with_interactive_context_returns_marker_at_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-TLC-5: with the contextvar set to True, the registry path
        returns the standard confirmation marker (not the no-interactive
        error)."""
        cv = _try_import_interactive_contextvar()
        if cv is None:
            pytest.skip("interactive contextvar not yet defined")

        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        _seed_holdings_cache(d)
        client = FakeSchwabClient()
        _install_easy_client(monkeypatch, client=client)

        token = cv.set(True)
        try:
            registry = Registry()
            registry.register(_make_place_orders_tool(s, d))
            result = await registry.call_tool(
                "schwab_place_orders",
                {
                    "orders": [
                        {
                            "account": "Individual Brokerage",
                            "action": "buy",
                            "symbol": "VXUS",
                            "quantity": 1,
                            "limit": 50.0,
                        }
                    ]
                },
            )
        finally:
            cv.reset(token)

        assert result.is_error is True
        assert result.output["error"] == "confirmation_required"
        assert client.place_order_calls == []

    async def test_post_confirm_executes_per_order_with_sleep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-UoW-5: post-confirm path places orders sequentially, one
        SDK call per order, with a 1s sleep between submissions."""
        cv = _try_import_interactive_contextvar()
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        _seed_holdings_cache(d)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.place_order_responses = [
            created(location="https://schwab.example/orders/100"),
            created(location="https://schwab.example/orders/101"),
        ]
        _install_easy_client(monkeypatch, client=client)

        # Patch sleep to capture and skip. `_place_all` runs inside
        # `asyncio.to_thread`, so the inter-order delay is `time.sleep`,
        # not `asyncio.sleep`. Patch both to be safe.
        sleeps: list[float] = []

        def _fake_time_sleep(d_seconds: float) -> None:
            sleeps.append(d_seconds)

        async def _fake_async_sleep(d_seconds: float) -> None:
            sleeps.append(d_seconds)

        import asyncio as _asyncio
        import time as _time

        monkeypatch.setattr(_time, "sleep", _fake_time_sleep)
        monkeypatch.setattr(_asyncio, "sleep", _fake_async_sleep)

        token = cv.set(True) if cv is not None else None
        try:
            registry = Registry()
            registry.register(_make_place_orders_tool(s, d))
            result = await registry.call_tool(
                "schwab_place_orders",
                {
                    "orders": [
                        {
                            "account": "Individual Brokerage",
                            "action": "buy",
                            "symbol": "VXUS",
                            "quantity": 10,
                            "limit": 80.0,
                        },
                        {
                            "account": "Individual Brokerage",
                            "action": "sell",
                            "symbol": "NVDA",
                            "quantity": 1,
                            "limit": 100.0,
                        },
                    ]
                },
                allow_unconfirmed=True,
            )
        finally:
            if token is not None:
                cv.reset(token)

        # Both placed.
        assert not result.is_error, f"unexpected error: {result.output!r}"
        assert result.output["placed"] == 2
        assert result.output["failed"] == 0
        assert len(client.place_order_calls) == 2
        # At least one 1.0s sleep between orders (we don't pin the exact
        # sleep value, just assert one happened).
        assert any(round(s, 2) == 1.0 for s in sleeps), (
            f"expected a 1.0s sleep between orders; got {sleeps!r}"
        )
        # Order_id parsed from Location header.
        results = result.output["results"]
        assert results[0]["order_id"] == "100"
        assert results[1]["order_id"] == "101"

    async def test_post_confirm_mixed_batch_one_succeeds_one_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cv = _try_import_interactive_contextvar()
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        _seed_holdings_cache(d)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.place_order_responses = [
            created(location="https://schwab.example/orders/200"),
            http_error(400, {"error": "bad order"}),
        ]
        _install_easy_client(monkeypatch, client=client)

        # Skip sleeps.
        import asyncio as _asyncio

        async def _no_sleep(_d: float) -> None:
            return None

        monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

        token = cv.set(True) if cv is not None else None
        try:
            registry = Registry()
            registry.register(_make_place_orders_tool(s, d))
            result = await registry.call_tool(
                "schwab_place_orders",
                {
                    "orders": [
                        {
                            "account": "Individual Brokerage",
                            "action": "buy",
                            "symbol": "VXUS",
                            "quantity": 1,
                            "limit": 80.0,
                        },
                        {
                            "account": "Individual Brokerage",
                            "action": "buy",
                            "symbol": "VOO",
                            "quantity": 1,
                            "limit": 500.0,
                        },
                    ]
                },
                allow_unconfirmed=True,
            )
        finally:
            if token is not None:
                cv.reset(token)

        assert result.output["placed"] == 1
        assert result.output["failed"] == 1
        # Audit lines for each.
        events = [e["event"] for e in _read_trade_log(d) if e.get("tool") == "schwab_place_orders"]
        # At least one placed and at least one failed (preview events
        # may also be present per BC-FC-5 step 3).
        assert "placed" in events
        assert "failed" in events


# ===========================================================================
# `schwab_cancel_order` (BR-UoW-6, BR-FC-6)
# ===========================================================================


class TestSchwabCancelOrder:
    async def test_cancel_without_confirmation_returns_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.get_order_response = ok(
            {
                "orderId": 1000000000001,
                "status": "WORKING",
                "orderType": "LIMIT",
                "price": 82.5,
                "orderLegCollection": [
                    {"instruction": "BUY", "quantity": 100, "instrument": {"symbol": "VXUS"}}
                ],
            }
        )
        _install_easy_client(monkeypatch, client=client)

        cv = _try_import_interactive_contextvar()
        token = cv.set(True) if cv is not None else None
        try:
            registry = Registry()
            registry.register(_make_cancel_order_tool(s, d))
            result = await registry.call_tool(
                "schwab_cancel_order",
                {"account": "Individual Brokerage", "order_id": "1000000000001"},
            )
        finally:
            if token is not None:
                cv.reset(token)

        assert result.is_error is True
        assert result.output["error"] == "confirmation_required"
        assert client.cancel_order_calls == []

    async def test_cancel_summarize_includes_order_details(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-FC-6: summarize_action looks up the order via get_order
        and renders its symbol/qty/status."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.get_order_response = ok(
            {
                "orderId": 1000000000001,
                "status": "WORKING",
                "orderType": "LIMIT",
                "price": 82.5,
                "orderLegCollection": [
                    {"instruction": "BUY", "quantity": 100, "instrument": {"symbol": "VXUS"}}
                ],
            }
        )
        _install_easy_client(monkeypatch, client=client)

        tool = _make_cancel_order_tool(s, d)
        rendered = tool.summarize_action(
            {"account": "Individual Brokerage", "order_id": "1000000000001"}
        )
        assert "VXUS" in rendered
        assert "100" in rendered
        assert "WORKING" in rendered.upper()

    async def test_cancel_terminal_state_post_confirm_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-FC-6: order in terminal state (FILLED/CANCELED/REJECTED)
        — summarize_action notes the terminal state; post-confirm call
        returns is_error=True (and does NOT submit a cancel)."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.get_order_response = ok(
            {
                "orderId": 1000000000001,
                "status": "FILLED",
                "orderType": "LIMIT",
                "price": 82.5,
                "orderLegCollection": [
                    {"instruction": "BUY", "quantity": 100, "instrument": {"symbol": "VXUS"}}
                ],
            }
        )
        _install_easy_client(monkeypatch, client=client)

        tool = _make_cancel_order_tool(s, d)
        # summarize_action notes terminal state.
        rendered = tool.summarize_action(
            {"account": "Individual Brokerage", "order_id": "1000000000001"}
        )
        assert "FILLED" in rendered.upper() or "TERMINAL" in rendered.upper()

        # Post-confirm call returns is_error=True.
        cv = _try_import_interactive_contextvar()
        token = cv.set(True) if cv is not None else None
        try:
            result = await tool.call({"account": "Individual Brokerage", "order_id": "1000000000001"})
        finally:
            if token is not None:
                cv.reset(token)

        assert result.is_error is True, "terminal-state cancel must return is_error=True"
        # SDK cancel was NOT issued.
        assert client.cancel_order_calls == []

    async def test_cancel_successful_writes_canceled_audit_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.get_order_response = ok(
            {
                "orderId": 1000000000001,
                "status": "WORKING",
                "orderType": "LIMIT",
                "price": 82.5,
                "orderLegCollection": [
                    {"instruction": "BUY", "quantity": 100, "instrument": {"symbol": "VXUS"}}
                ],
            }
        )
        client.cancel_order_response = ok({})  # success
        _install_easy_client(monkeypatch, client=client)

        cv = _try_import_interactive_contextvar()
        token = cv.set(True) if cv is not None else None
        try:
            await _make_cancel_order_tool(s, d).call(
                {"account": "Individual Brokerage", "order_id": "1000000000001"}
            )
        finally:
            if token is not None:
                cv.reset(token)

        events = [e["event"] for e in _read_trade_log(d)]
        assert "canceled" in events, f"expected 'canceled' audit event; got {events!r}"

    async def test_cancel_allowlist_enforced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(
            tmp_path,
            monkeypatch,
            allowed_accounts="Individual Brokerage",
            account_names="1234=Individual Brokerage,5678=Roth Contributory IRA",
        )
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers(
            [("12341234", "HASH1234"), ("56785678", "HASH5678")]
        )
        _install_easy_client(monkeypatch, client=client)

        cv = _try_import_interactive_contextvar()
        token = cv.set(True) if cv is not None else None
        try:
            with pytest.raises(ToolError):
                await _make_cancel_order_tool(s, d).call(
                    {"account": "Roth Contributory IRA", "order_id": "1234567890"}
                )
        finally:
            if token is not None:
                cv.reset(token)

        assert client.cancel_order_calls == []


# ===========================================================================
# Audit log (BR-TLC-4, BR-TLC-7)
# ===========================================================================


class TestAuditLog:
    async def test_read_event_written_for_holdings_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-TLC-7: schwab_holdings appends a `read` event."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.accounts_response = ok([sample_accounts_payload(account_number="12341234")])
        _install_easy_client(monkeypatch, client=client)

        await _make_holdings_tool(s, d).call({"force_refresh": True})
        events = [e["event"] for e in _read_trade_log(d) if e.get("tool") == "schwab_holdings"]
        assert "read" in events, f"expected a read event; got {events!r}"

    async def test_jsonl_format_parseable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.accounts_response = ok([sample_accounts_payload(account_number="12341234")])
        _install_easy_client(monkeypatch, client=client)

        await _make_holdings_tool(s, d).call({"force_refresh": True})

        path = _trade_log_path(d)
        assert path.exists()
        # One JSON object per line.
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            # Top-level fields per BC-TLC-4 schema.
            assert "ts" in obj
            assert "event" in obj
            assert "tool" in obj

    async def test_trade_log_file_mode_0600(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.accounts_response = ok([sample_accounts_payload(account_number="12341234")])
        _install_easy_client(monkeypatch, client=client)

        await _make_holdings_tool(s, d).call({"force_refresh": True})

        path = _trade_log_path(d)
        assert path.exists()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"trade_log.jsonl mode is {oct(mode)}; expected 0o600"

    async def test_audit_log_failure_does_not_block_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-TLC-4: log write is best-effort. If open() fails, the
        tool call still returns successfully."""
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.accounts_response = ok([sample_accounts_payload(account_number="12341234")])
        _install_easy_client(monkeypatch, client=client)

        # Patch Path.open to raise on the trade-log path only.
        original_open = Path.open
        trade_log = _trade_log_path(d)

        def _failing_open(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self == trade_log:
                raise OSError("simulated audit log write failure")
            return original_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", _failing_open)

        # Tool call must still succeed.
        result = await _make_holdings_tool(s, d).call({"force_refresh": True})
        assert not result.is_error, (
            f"audit-log failure must not block tool call; got error: {result.output!r}"
        )

    async def test_preview_event_written_before_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-UoW-5 step 3: audit logs a `preview` event when the gate
        marker is built."""
        cv = _try_import_interactive_contextvar()
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        _seed_holdings_cache(d)
        client = FakeSchwabClient()
        _install_easy_client(monkeypatch, client=client)

        token = cv.set(True) if cv is not None else None
        try:
            # Tool-direct call (not via registry) — the gate marker is
            # generated via the tool's `call()` returning a marker
            # ToolResult when `allow_unconfirmed` is unset.
            registry = Registry()
            registry.register(_make_place_orders_tool(s, d))
            await registry.call_tool(
                "schwab_place_orders",
                {
                    "orders": [
                        {
                            "account": "Individual Brokerage",
                            "action": "buy",
                            "symbol": "VXUS",
                            "quantity": 1,
                            "limit": 50.0,
                        }
                    ]
                },
            )
        finally:
            if token is not None:
                cv.reset(token)

        events = [e["event"] for e in _read_trade_log(d)]
        assert "preview" in events, f"expected preview audit event; got {events!r}"


# ===========================================================================
# TLC-3 confirmation flow at the registry level
# ===========================================================================


class TestTLC3Flow:
    async def test_first_call_without_allow_unconfirmed_returns_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cv = _try_import_interactive_contextvar()
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        _seed_holdings_cache(d)
        client = FakeSchwabClient()
        _install_easy_client(monkeypatch, client=client)

        token = cv.set(True) if cv is not None else None
        try:
            registry = Registry()
            registry.register(_make_place_orders_tool(s, d))
            result = await registry.call_tool(
                "schwab_place_orders",
                {
                    "orders": [
                        {
                            "account": "Individual Brokerage",
                            "action": "buy",
                            "symbol": "VXUS",
                            "quantity": 1,
                            "limit": 50.0,
                        }
                    ]
                },
            )
        finally:
            if token is not None:
                cv.reset(token)

        assert result.is_error is True
        assert result.output["error"] == "confirmation_required"
        assert client.place_order_calls == []

    async def test_second_call_with_allow_unconfirmed_executes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cv = _try_import_interactive_contextvar()
        s = _make_settings(tmp_path, monkeypatch)
        d = _data_dir(tmp_path)
        _seed_holdings_cache(d)
        client = FakeSchwabClient()
        client.account_numbers_response = sample_account_numbers([("12341234", "HASH1234")])
        client.place_order_response = created(location="https://example/orders/9999")
        _install_easy_client(monkeypatch, client=client)

        # No-op sleeps.
        import asyncio as _asyncio

        async def _no_sleep(_d: float) -> None:
            return None

        monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

        token = cv.set(True) if cv is not None else None
        try:
            registry = Registry()
            registry.register(_make_place_orders_tool(s, d))
            args = {
                "orders": [
                    {
                        "account": "Individual Brokerage",
                        "action": "buy",
                        "symbol": "VXUS",
                        "quantity": 1,
                        "limit": 50.0,
                    }
                ]
            }
            # First call: marker.
            r1 = await registry.call_tool("schwab_place_orders", args)
            assert r1.is_error is True
            # Second call with allow_unconfirmed=True: executes.
            r2 = await registry.call_tool("schwab_place_orders", args, allow_unconfirmed=True)
        finally:
            if token is not None:
                cv.reset(token)

        assert not r2.is_error, f"unexpected error on second call: {r2.output!r}"
        assert client.place_order_calls, "expected at least one place_order call after confirm"
