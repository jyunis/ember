"""Fakes for the `schwab-py` SDK used by `ember.tools.schwab`.

The implementer is expected to:

  - `import schwab` (or `from schwab.auth import easy_client`) and use
    `schwab.auth.easy_client(api_key=..., app_secret=..., callback_url=...,
    token_path=..., interactive=False)` to construct a sync `Client`.
  - Call `client.get_account_numbers()`, `client.get_accounts(fields=...)`,
    `client.get_quotes(symbols)`,
    `client.get_orders_for_all_linked_accounts(...)`,
    `client.get_transactions(...)`, `client.get_order(order_id, account_hash)`,
    `client.place_order(account_hash, order)`,
    `client.cancel_order(order_id, account_hash)`.
  - Each method returns an httpx.Response-shaped object: the fake exposes
    `.status_code`, `.json()`, `.headers`, `.text`.

These fakes mirror that surface area liberally — the tester can't see
the implementer's code so the contract is what matters; minor method
renames will be harmonized at merge time.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# httpx.Response-shaped object
# ---------------------------------------------------------------------------


@dataclass
class FakeResponse:
    """Mimics the relevant httpx.Response surface."""

    status_code: int = 200
    _json_body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""

    def json(self) -> Any:
        if self._json_body is None and self.text:
            return _json.loads(self.text)
        return self._json_body


def ok(body: Any, *, headers: dict[str, str] | None = None) -> FakeResponse:
    """Helper: build a 200/201-style success response."""
    return FakeResponse(
        status_code=200,
        _json_body=body,
        headers=dict(headers or {}),
        text=_json.dumps(body) if body is not None else "",
    )


def created(*, location: str | None = None) -> FakeResponse:
    """201 Created. The Schwab Trader API returns the new order's URL in
    the `Location` header; the implementer parses the order_id from the
    last path segment."""
    headers = {}
    if location:
        headers["location"] = location
    return FakeResponse(status_code=201, _json_body={}, headers=headers, text="")


def http_error(status: int, body: Any | None = None) -> FakeResponse:
    """Synthesize a non-success HTTP response for error-mapping tests."""
    text = _json.dumps(body) if body is not None else ""
    return FakeResponse(
        status_code=status,
        _json_body=body if body is not None else {"error": f"HTTP {status}"},
        headers={},
        text=text,
    )


# ---------------------------------------------------------------------------
# FakeSchwabClient — the object the patched easy_client returns
# ---------------------------------------------------------------------------


@dataclass
class FakeSchwabClient:
    """Stands in for `schwab.client.Client` (sync).

    The instance records every call into `*_calls` lists so tests can
    assert on dispatch shape (allowlist filtering, status filter on
    open_orders, default until=today on transactions).

    Each method returns whatever Response-shaped object the test queued
    via the corresponding `*_response` field. If a test raises is set,
    raise instead.
    """

    # Pre-canned responses (set by tests). All default to 200 OK with
    # an empty payload so the implementer's "happy path" doesn't crash
    # before the test reaches the assertion it cares about.
    account_numbers_response: FakeResponse | None = None
    accounts_response: FakeResponse | None = None
    quotes_response: FakeResponse | None = None
    open_orders_response: FakeResponse | None = None
    transactions_response: FakeResponse | None = None
    get_order_response: FakeResponse | None = None
    place_order_response: FakeResponse | None = None
    cancel_order_response: FakeResponse | None = None

    # Or queued sequences (FIFO) — useful for batch place_orders where
    # successive calls return different responses (placed, placed,
    # failed).
    place_order_responses: list[FakeResponse] = field(default_factory=list)

    # Optional exceptions the methods raise instead of returning.
    account_numbers_raises: BaseException | None = None
    accounts_raises: BaseException | None = None
    quotes_raises: BaseException | None = None
    open_orders_raises: BaseException | None = None
    transactions_raises: BaseException | None = None
    get_order_raises: BaseException | None = None
    place_order_raises: BaseException | None = None
    cancel_order_raises: BaseException | None = None

    # Call recording.
    account_numbers_calls: list[dict[str, Any]] = field(default_factory=list)
    accounts_calls: list[dict[str, Any]] = field(default_factory=list)
    quotes_calls: list[Any] = field(default_factory=list)
    open_orders_calls: list[dict[str, Any]] = field(default_factory=list)
    transactions_calls: list[dict[str, Any]] = field(default_factory=list)
    get_order_calls: list[tuple[Any, Any]] = field(default_factory=list)
    place_order_calls: list[tuple[Any, Any]] = field(default_factory=list)
    cancel_order_calls: list[tuple[Any, Any]] = field(default_factory=list)

    # ----- methods -----

    def get_account_numbers(self, *args: Any, **kwargs: Any) -> FakeResponse:
        self.account_numbers_calls.append({"args": args, "kwargs": kwargs})
        if self.account_numbers_raises is not None:
            raise self.account_numbers_raises
        return self.account_numbers_response or ok([])

    def get_accounts(self, *args: Any, **kwargs: Any) -> FakeResponse:
        self.accounts_calls.append({"args": args, "kwargs": kwargs})
        if self.accounts_raises is not None:
            raise self.accounts_raises
        return self.accounts_response or ok([])

    def get_quotes(self, symbols: Any, *args: Any, **kwargs: Any) -> FakeResponse:
        self.quotes_calls.append(symbols)
        if self.quotes_raises is not None:
            raise self.quotes_raises
        return self.quotes_response or ok({})

    def get_orders_for_all_linked_accounts(self, *args: Any, **kwargs: Any) -> FakeResponse:
        self.open_orders_calls.append({"args": args, "kwargs": kwargs})
        if self.open_orders_raises is not None:
            raise self.open_orders_raises
        return self.open_orders_response or ok([])

    def get_transactions(self, *args: Any, **kwargs: Any) -> FakeResponse:
        self.transactions_calls.append({"args": args, "kwargs": kwargs})
        if self.transactions_raises is not None:
            raise self.transactions_raises
        return self.transactions_response or ok([])

    def get_order(
        self, order_id: Any, account_hash: Any, *args: Any, **kwargs: Any
    ) -> FakeResponse:
        self.get_order_calls.append((order_id, account_hash))
        if self.get_order_raises is not None:
            raise self.get_order_raises
        return self.get_order_response or ok({})

    def place_order(self, account_hash: Any, order: Any, *args: Any, **kwargs: Any) -> FakeResponse:
        self.place_order_calls.append((account_hash, order))
        if self.place_order_raises is not None:
            raise self.place_order_raises
        if self.place_order_responses:
            return self.place_order_responses.pop(0)
        return self.place_order_response or created(location="https://example/orders/1")

    def cancel_order(
        self, order_id: Any, account_hash: Any, *args: Any, **kwargs: Any
    ) -> FakeResponse:
        self.cancel_order_calls.append((order_id, account_hash))
        if self.cancel_order_raises is not None:
            raise self.cancel_order_raises
        return self.cancel_order_response or ok({})


# ---------------------------------------------------------------------------
# Schwab Account.Fields enum stub — matches schwab-py's enum.
# ---------------------------------------------------------------------------


class _AccountFields:
    """Stand-in for `schwab.client.Client.Account.Fields`.

    Real schwab-py exposes `Client.Account.Fields.POSITIONS` as an enum
    member. We expose a string-valued sentinel so the implementer's
    `fields=[POSITIONS]` arg makes it through the fake.
    """

    POSITIONS = "POSITIONS"


# ---------------------------------------------------------------------------
# Sample response payloads
# ---------------------------------------------------------------------------


def sample_account_numbers(
    accounts: list[tuple[str, str]] | None = None,
) -> FakeResponse:
    """Build a get_account_numbers response.

    `accounts` is a list of (account_number, hash_value) tuples.
    """
    if accounts is None:
        accounts = [
            ("12341234", "HASH1234"),
            ("56785678", "HASH5678"),
        ]
    return ok([{"accountNumber": num, "hashValue": h} for num, h in accounts])


def sample_position(
    *,
    symbol: str = "VXUS",
    description: str = "Vanguard Total International Stock ETF",
    asset_type: str = "EQUITY",
    long_quantity: float = 100.0,
    short_quantity: float = 0.0,
    average_price: float = 80.0,
    market_value: float = 8500.0,
    current_day_pl: float = 50.0,
    current_day_pl_pct: float = 0.5,
) -> dict[str, Any]:
    return {
        "instrument": {
            "symbol": symbol,
            "description": description,
            "assetType": asset_type,
        },
        "longQuantity": long_quantity,
        "shortQuantity": short_quantity,
        "averagePrice": average_price,
        "marketValue": market_value,
        "currentDayProfitLoss": current_day_pl,
        "currentDayProfitLossPercentage": current_day_pl_pct,
    }


def sample_accounts_payload(
    *,
    account_number: str = "12341234",
    account_type: str = "CASH",
    cash_balance: float = 5000.0,
    liquidation_value: float = 13500.0,
    positions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if positions is None:
        positions = [sample_position()]
    return {
        "securitiesAccount": {
            "accountNumber": account_number,
            "type": account_type,
            "currentBalances": {
                "cashBalance": cash_balance,
                "liquidationValue": liquidation_value,
            },
            "positions": positions,
        }
    }


__all__ = [
    "FakeResponse",
    "FakeSchwabClient",
    "_AccountFields",
    "created",
    "http_error",
    "ok",
    "sample_account_numbers",
    "sample_accounts_payload",
    "sample_position",
]
