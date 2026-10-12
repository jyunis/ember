# Brokerage — Behavioral Contracts

**Status:** Live behavioral spec for the Schwab brokerage integration in Ember; paired with `Ember — Architecture` (#6) and `Ember — Contracts` (#7).
**Date:** 2026-05-07.
**Codename:** Brokerage.

This document covers steps 1–8 from the planning conversation:

| # | Surface |
|---|---|
| 1 | `src/ember/tools/schwab.py` — six tools |
| 2 | `_maybe_register_schwab_tools` in `src/ember/main.py` |
| 3 | Settings + `.env.example` additions |
| 4 | `data/schwab/` scaffolding + initial browser auth |
| 5 | `data/skills/portfolio-review/SKILL.md` |
| 6 | `data/skills/daily-portfolio-brief/SKILL.md` + APScheduler entry |
| 7 | `/schwab-reauth` Discord command + pre-handler (on-demand, no schedule) |
| 8 | Seed `/finance/thesis.md` and `/finance/watchlist.md` in documents MCP |

Steps 1–4 are the **read-only-verifiable phase** that ship first. 5–8 follow once the smoke test is green.

The structure mirrors `Ember — Contracts`:
1. **Top-level contracts (BR-TLC):** cross-cutting invariants for the brokerage subsystem.
2. **Units of work (BR-UoW):** discrete operations with pre/post conditions and failure semantics.
3. **Feature contracts (BR-FC):** per-component obligations, including canonical input schemas and result shapes.

If a piece of code can violate one of these, that's a bug.

---

## Glossary

| Term | Meaning |
|---|---|
| `schwab-py` | Python SDK [https://github.com/alexgolec/schwab-py](https://github.com/alexgolec/schwab-py) — the only path the agent uses to talk to Schwab. |
| Account hash | Opaque per-account identifier returned by `client.get_account_numbers()`. The Trader API requires this hash in URL paths; the raw account number is never used. |
| Friendly name | Human-readable account name from `SCHWAB_ACCOUNT_NAMES` (e.g. `Individual Brokerage`). |
| Notional | `quantity * price`. For limit orders, the limit price is used; for market orders, the latest known mark or last fetched holdings price. |
| Gate | The TLC-3 confirmation flow — registry returns `confirmation_required` marker, agent persists `pending_tool_confirmation`, user replies "yes" before the call runs. |
| Cap refusal | Notional cap exceeded → `ToolError` raised before reaching the gate. The user cannot override mid-conversation; the cap is config in `.env`. |

---

## Top-level contracts

### BR-TLC-1: Confirmation gate on all write actions
Every tool that places, modifies, or cancels an order at Schwab declares `requires_confirmation = True`. The registry's TLC-3 gate is the only path through which write actions reach the network.

**Rationale:** matches `profile_update`. Mistaken or hallucinated trades must be impossible without an explicit user "yes".
**Enforcement:** unit-tested by asserting `requires_confirmation is True` on `SchwabPlaceOrdersTool` and `SchwabCancelOrderTool`.
**Test:** invoke either tool via `registry.call_tool(...)` without `allow_unconfirmed=True` → returns `ToolResult(is_error=True, output={"error": "confirmation_required", "action": <summary>})`. The Schwab client must NOT have been called.

### BR-TLC-2: Notional caps are hard refusals
`SCHWAB_MAX_ORDER_NOTIONAL_USD`, `SCHWAB_MAX_BATCH_NOTIONAL_USD`, and `SCHWAB_MAX_DAILY_NOTIONAL_USD` are evaluated **before** the gate. If any cap would be exceeded, the tool raises `ToolError` with a message naming the cap and the calculated notional. The user cannot override in chat — they must edit `.env` and restart Ember.

**Rationale:** the gate is a human-confirmation surface; caps are a safety surface. They serve different failure modes (mistake vs. attack-or-runaway-loop) and must not collapse into one.
**Defaults:** `MAX_ORDER=20000`, `MAX_BATCH=75000`, `MAX_DAILY=100000` (USD).
**Test:** order whose notional exceeds the per-order cap → `ToolError` raised, gate never triggered, no log entry written.

### BR-TLC-3: Account allowlist via friendly-name exact match
Account resolution uses `SCHWAB_ACCOUNT_NAMES` (last-4 → friendly name) and `SCHWAB_ALLOWED_ACCOUNTS` (comma-separated friendly names). If `SCHWAB_ALLOWED_ACCOUNTS` is empty, **all** accounts known to `SCHWAB_ACCOUNT_NAMES` are allowed for read; **none** are allowed for write. Match is case-insensitive but exact (no fuzzy "Individual" → "Individual Brokerage").

**Rationale:** the existing portfolio-advisor uses fuzzy-substring matching, which is fine for an interactive shell where the user can see what got resolved. An LLM-driven flow is more brittle to silent fallthrough; an unrecognized name must fail loudly.
**Test:** request `account="Individual"` → `ToolError` listing the available exact names. Request `account="INDIVIDUAL BROKERAGE"` → resolves correctly.

### BR-TLC-4: Append-only audit
Every read tool, every preview, every confirmed call, and every Schwab response (success or HTTP error) appends a single JSON line to `<data_dir>/schwab/trade_log.jsonl`. The format is:

```json
{
  "ts": "2026-05-07T22:01:13.412Z",
  "event": "preview" | "confirmed" | "placed" | "failed" | "canceled" | "read",
  "tool": "schwab_place_orders",
  "account": "Individual Brokerage",
  "action": "buy" | "sell" | null,
  "symbol": "VXUS",
  "quantity": 100,
  "limit": 82.5,
  "order_id": "abc123" | null,
  "http_status": 201 | null,
  "error": null,
  "request_hash": "<sha256 of canonical args, hex>",
  "notional_usd": 8250.00
}
```

The log file is created with mode 0600 if absent. Writes are best-effort — log failures must not block the tool call (log a warning to `_log` and continue).

**Rationale:** post-hoc reconciliation must work even if everything else burns down.
**Test:** synthesize a `preview` and a successful `placed` event; assert both lines exist and parse as JSON; assert the file mode is 0600.

### BR-TLC-5: No execution from automated context
Scheduled and proactive code paths (daily brief, health check, scheduler-fired skills) **must not** call `schwab_place_orders` or `schwab_cancel_order`. The runtime check: these tools, when invoked outside an active conversation context (i.e., when the agent's stream observer is not present), return `ToolResult(is_error=True, output={"error": "no_interactive_context"})` instead of producing a confirmation-required marker.

**Rationale:** the gate is a UX construct on top of a conversation. A scheduled job has no "next user turn" to consume the gate. Without this rule, a malformed scheduler entry could in principle stage a trade with an automated "yes". This rule closes that hole at the tool level.
**Implementation hint:** the tool reads a contextvar `ember.agent.context.in_interactive_turn` (already set by the agent loop). If unset/False → fail closed. Tester: verify both branches.
**Test:** call `schwab_place_orders` with no contextvar set → `is_error=True, output["error"] == "no_interactive_context"`. With contextvar set to True → returns the standard confirmation marker.

### BR-TLC-6: Token + secret hygiene
- App key/secret are `SecretStr` in settings — `__str__` returns `"**********"`. Never logged.
- The OAuth token JSON at `<data_dir>/schwab/.schwab_token.json` is created/maintained at mode 0600.
- The `<data_dir>/schwab/` directory is mode 0700.
- Holdings JSON cache and trade log are mode 0600.
- On any auth failure (HTTP 401 from Schwab), the tool raises `AuthError` with a message **that does not include the token contents**.

**Test:** start with no token file → tool returns `AuthError`. Inspect raised exception message → does not contain the app key or app secret literals.

### BR-TLC-7: Read-only tier never gated, never logs PII beyond ticker/qty/$
`schwab_holdings`, `schwab_quote`, `schwab_open_orders`, `schwab_transactions` declare `requires_confirmation = False`. Their audit-log entries use `event="read"` and **omit** raw account numbers. The friendly name is logged, but never the underlying account number string.

**Test:** call `schwab_holdings` → assert no `account_number_full` field anywhere in trade_log entry; assert friendly name present.

### BR-TLC-8: Idempotent registration
`_maybe_register_schwab_tools(registry, settings)` is safe to call multiple times in tests: if `SCHWAB_APP_KEY` or `SCHWAB_APP_SECRET` is unset, it logs and returns without raising. With both set, it registers six tools; calling again raises `ValueError("tool 'schwab_holdings' is already registered")` from the registry — that's the registry's invariant, the wiring function does not silently re-register.

**Test:** call wiring with unset secrets → no tools registered; call with full settings on a fresh registry → six tools present; call again → `ValueError`.

---

## Units of work

### BR-UoW-1: Refresh holdings
`schwab_holdings(force_refresh=False)` returns positions and balances for every allowed account.

**Pre:** valid token; allowed-accounts list non-empty.
**Behavior:**
- If `force_refresh=False` and `<data_dir>/schwab/holdings.json` exists with `fetched_at` < 30 min old → return the cached file content.
- Otherwise: call `client.get_account_numbers()` and `client.get_accounts(fields=[POSITIONS])`, normalize per the schema below, write atomically to `holdings.json`, return the same payload.
- Filter accounts to those in the allowlist (or all known if allowlist empty).
- **Stale fallback:** if the live path raises `AuthError` (expired refresh token, revoked grant, 401) *and* `holdings.json` exists, return it regardless of age with `stale: true`, `age_seconds: <int>`, `stale_reason: <the AuthError text>`. With no cache on disk the `AuthError` propagates. Only `AuthError` triggers this — a 5xx stays `ToolRetryable`, since retrying is the right move and old data would mask a transient blip.
**Post:** `holdings.json` exists with mode 0600 and `fetched_at` = now (unmodified on the stale-fallback path).
**Failure modes:** auth → `AuthError` *(only when no cache exists; see stale fallback)*; transient HTTP → `ToolRetryable`; other 4xx → `ToolError`; 5xx → `ToolRetryable`; OSError → `TransportError`.

Rationale for the fallback: positions and cost basis only change when a trade settles, so a weeks-old snapshot still answers "what do I hold?" — and `daily_portfolio_brief` already reads this exact file as the declared portfolio. Before this, the tool reported a hard auth failure while the brief priced the same data nightly. The `stale`/`age_seconds` markers are load-bearing: they are what stops an old snapshot from being read as live, so they must survive account filtering.

### BR-UoW-2: Get quote(s)
`schwab_quote(symbols=["VXUS","VOO"])` returns current pricing for one or more symbols.

**Pre:** valid token. `symbols` non-empty list of uppercase tickers, ≤ 50 entries.
**Behavior:** delegates to `client.get_quotes(symbols)`. Normalizes to `{symbol, last, bid, ask, mark, volume, day_change_pct, asof}` per ticker. Unknown tickers appear with `null` price fields and `error: "not_found"`.
**Failure modes:** as BR-UoW-1.

### BR-UoW-3: List open orders
`schwab_open_orders(account=None, max_age_days=30)` returns pending orders.

**Pre:** valid token.
**Behavior:** delegates to `client.get_orders_for_all_linked_accounts(...)` with statuses filter `[WORKING, PENDING_ACTIVATION, AWAITING_PARENT_ORDER]`. Filters to allowed accounts. Normalizes per the order schema below.

### BR-UoW-4: Fetch transactions
`schwab_transactions(account="Individual Brokerage", since="2026-04-01", until=None, types=None)` returns historical fills.

**Pre:** valid token; account must resolve via BR-TLC-3.
**Behavior:** delegates to `client.get_transactions(...)`. Returns normalized list of `{ts, type, action, symbol, quantity, price, amount, fees, order_id}`. `since` is required; `until` defaults to today. Inclusive on both bounds.

### BR-UoW-5: Place batch orders
`schwab_place_orders(orders=[...])` previews, validates, gates, places.

**Pre:**
1. Each order resolves to an allowed account (BR-TLC-3).
2. Each order's notional ≤ `MAX_ORDER` (BR-TLC-2).
3. Sum of notionals across orders ≤ `MAX_BATCH` (BR-TLC-2).
4. Sum of notionals across orders + rolling-24h notional from trade_log ≤ `MAX_DAILY` (BR-TLC-2).
5. Interactive context (BR-TLC-5).
**Behavior:**
1. Resolve every account hash up front. If any fails, `ToolError` and abort the whole batch — no partial submission.
2. Compute estimated price per order (limit if set, else last quote). Build the preview block.
3. If gate not yet satisfied → return confirmation marker via the registry (no Schwab calls made yet). Audit log: one `preview` line per order.
4. On post-confirmation call, place each order sequentially with a 1s sleep between submissions. For each: append `placed` (with order_id) or `failed` (with http_status + truncated body) to the trade log.
**Returns:** `{placed: int, failed: int, results: [{symbol, action, quantity, status, order_id?, error?}]}`.
**Atomicity:** none — if order #3 of 5 fails, orders 1-2 are placed and orders 4-5 still attempt. The result clearly reports per-order status.
**Test:** see test matrix.

### BR-UoW-6: Cancel pending order
`schwab_cancel_order(account, order_id)` cancels a single pending order.

**Pre:** account is allowed; interactive context (BR-TLC-5).
**Behavior:** preview shows the existing order details (looked up via `client.get_order(order_id, account_hash)`); gate; on confirm `client.cancel_order(order_id, account_hash)`. Audit log `canceled` or `failed`.

### BR-UoW-7: On-demand re-auth via Discord
A user-initiated two-message handshake refreshes the Schwab token. No
schedule, no listener, no SSH tunnel — the user is the data path. Triggered
by the `/schwab-reauth` slash command (BR-FC-11):

1. **Step 1 — auth URL:** Ember calls `schwab.auth.get_auth_context(api_key, callback_url)`,
   serializes the resulting OAuth state token into the conversation row as
   `pending_schwab_reauth = {state, callback_url, expires_at}` with a 5-minute TTL,
   and replies with the auth URL. The user opens it in any browser, completes
   Schwab login + Allow.
2. **Step 2 — paste back:** Schwab redirects to `https://127.0.0.1:8182/?code=...&state=...`.
   The browser shows an error page (no listener) — that's expected. The user
   copies the URL from the address bar and pastes it as their next Discord
   message. A pre-handler in the agent loop (mirroring the
   `pending_tool_confirmation` path) detects the redirect-URL shape, validates
   `state` matches the persisted one, then calls
   `schwab.auth.client_from_received_url(api_key, app_secret, ...)`. Token writes
   to `<data_dir>/schwab/.schwab_token.json`. Pending state cleared. Ember replies
   with refresh status.

**Failure modes:**
- TTL expired → reply "re-auth expired; run `/schwab-reauth` again", clear state.
- State mismatch → reply with the mismatch + restart instructions.
- Code expired (Schwab gives ~30s after issuance) → reply "Schwab code expired,
  run `/schwab-reauth` again — paste the URL within 30s next time."
- Schwab token-exchange HTTP error → surface `error_description` from response,
  do not log the code or token contents.

There is no scheduled health check. If a tool call hits `AuthError` because the
refresh token is dead, the tool surfaces the error to the LLM, which can suggest
the user run `/schwab-reauth`. The LLM never starts the flow on its own — only
the slash command initiates step 1.

### BR-UoW-8: Daily portfolio brief
At 17:00 America/Chicago, Mon–Fri, the daily-brief skill (step 6) composes a
narrative email and sends via the existing `gmail_send` tool. Composition uses
`schwab_holdings`, `schwab_quote` (for SPY/QQQ benchmarks), `web_fetch` for
headlines, and reads `/finance/thesis.md` + `/finance/watchlist.md` from
documents MCP. The brief contains no executable trade language and no draft
order JSON.

**Stale-data handling:** `schwab_holdings(force_refresh=True)` may raise
`AuthError` if the refresh token expired. The brief catches this and falls back
to the most recent cached `holdings.json`, prepending an "as-of" line to the
email body and a footer line: *"Run `/schwab-reauth` in Discord to refresh."*
A failed refresh never blocks the brief from sending — stale data is more useful
than no brief.

---

## Feature contracts

### BR-FC-1: `schwab_holdings`

```python
class SchwabHoldingsTool:
    name = "schwab_holdings"
    description = (
        "Fetch and return positions + balances for all allowed Schwab "
        "accounts. By default returns cached holdings if < 30 minutes "
        "old; pass force_refresh=true to bypass the cache. Returns "
        "per-account positions with symbol, quantity, average_price, "
        "market_value, cost_basis, gain_dollar, gain_percent, "
        "day_gain_dollar, day_gain_percent, asset_type plus account "
        "balances."
    )
    requires_confirmation = False
    input_schema = {
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
                    "Friendly account names to filter to. Default: all "
                    "allowed accounts."
                ),
            },
        },
        "additionalProperties": False,
    }
```

**Output schema:** (`stale`, `age_seconds`, `stale_reason` appear only on the stale-fallback path — see BR-UoW-1)
```json
{
  "fetched_at": "2026-05-07T22:01:13.412Z",
  "from_cache": false,
  "accounts": [
    {
      "name": "Individual Brokerage",
      "account_number_last4": "1234",
      "type": "CASH",
      "current_balances": {
        "cash_balance": 12345.67,
        "liquidation_value": 234567.89
      },
      "positions": [
        {
          "symbol": "VXUS",
          "description": "Vanguard Total International Stock ETF",
          "asset_type": "EQUITY",
          "quantity": 520.0,
          "average_price": 79.4231,
          "market_value": 42500.00,
          "cost_basis": 41300.00,
          "gain_dollar": 1200.00,
          "gain_percent": 2.91,
          "day_gain_dollar": 215.40,
          "day_gain_percent": 0.51
        }
      ]
    }
  ]
}
```

Positions sort descending by `abs(market_value)`. Accounts sort ascending by `name`.

### BR-FC-2: `schwab_quote`

```python
class SchwabQuoteTool:
    name = "schwab_quote"
    description = (
        "Get current quotes for one or more symbols. Returns last/bid/"
        "ask/mark, day change percent, and volume per ticker. Unknown "
        "tickers are returned with null prices and error='not_found'."
    )
    requires_confirmation = False
    input_schema = {
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
```

**Output:**
```json
{
  "asof": "2026-05-07T22:01:13Z",
  "quotes": [
    {
      "symbol": "VXUS",
      "last": 81.92,
      "bid": 81.91,
      "ask": 81.93,
      "mark": 81.92,
      "volume": 4123890,
      "day_change_pct": 0.51,
      "asof": "2026-05-07T22:01:00Z",
      "error": null
    }
  ]
}
```

Symbols are uppercased before sending. Duplicates de-duplicated. Order in `quotes` matches the de-duplicated input order.

### BR-FC-3: `schwab_open_orders`

```python
class SchwabOpenOrdersTool:
    name = "schwab_open_orders"
    description = (
        "List pending (working / pending-activation / awaiting-parent) "
        "orders across allowed Schwab accounts. Returns order id, "
        "account, symbol, action, quantity, order_type, limit_price, "
        "entered_at, status."
    )
    requires_confirmation = False
    input_schema = {
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
```

**Output:**
```json
{
  "orders": [
    {
      "order_id": "1000000000001",
      "account": "Individual Brokerage",
      "symbol": "VXUS",
      "action": "buy",
      "quantity": 100,
      "order_type": "LIMIT",
      "limit_price": 82.50,
      "entered_at": "2026-05-07T13:42:00Z",
      "status": "WORKING"
    }
  ]
}
```

### BR-FC-4: `schwab_transactions`

```python
class SchwabTransactionsTool:
    name = "schwab_transactions"
    description = (
        "Return executed transactions for an account in [since, until]. "
        "Useful for trade reconciliation and tax-lot tracking."
    )
    requires_confirmation = False
    input_schema = {
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
```

**Output:**
```json
{
  "account": "Individual Brokerage",
  "since": "2026-04-01",
  "until": "2026-05-07",
  "transactions": [
    {
      "ts": "2026-04-15T14:32:00Z",
      "type": "TRADE",
      "action": "buy",
      "symbol": "VXUS",
      "quantity": 100,
      "price": 81.05,
      "amount": -8105.00,
      "fees": 0.00,
      "order_id": "1004233123456"
    }
  ]
}
```

### BR-FC-5: `schwab_place_orders` (gated)

```python
class SchwabPlaceOrdersTool:
    name = "schwab_place_orders"
    description = (
        "Place a batch of equity orders across one or more Schwab "
        "accounts. Each order: action (buy|sell), symbol, quantity, "
        "optional limit price, account (friendly name). Always preview "
        "first; the system shows a confirmation prompt with the full "
        "preview block before any order is placed. Caps and account "
        "allowlist are enforced before the gate."
    )
    requires_confirmation = True
    input_schema = {
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

    def summarize_action(self, args: dict[str, Any]) -> str:
        ...
```

**summarize_action() output (the user-facing 🔒 prompt):**
```
schwab_place_orders — 3 order(s)

  BUY  100 VXUS @ limit $82.50  in Individual Brokerage  est $8,250.00
  SELL  10 NVDA @ market         in Individual Brokerage  est $1,234.50  (gain ~$432, tax ~$108)
  BUY  300 ICLN @ market         in Roth Contributory IRA  est $4,500.00

Total notional: $13,984.50
Caps: order $20,000 · batch $75,000 · daily $100,000 (used $0 in last 24h)
```

For sells: include estimated gain/loss using `holdings.json` average_price. For limits: use the limit price; for markets: use last known mark from holdings or a freshly fetched quote (best-effort; fall back to "—" if unavailable). Do not block the preview on quote-fetch failure.

**Output (post-confirm):**
```json
{
  "placed": 2,
  "failed": 1,
  "results": [
    {"symbol":"VXUS","action":"buy","quantity":100,"account":"Individual Brokerage","status":"placed","order_id":"1004..."},
    {"symbol":"NVDA","action":"sell","quantity":10,"account":"Individual Brokerage","status":"placed","order_id":"1005..."},
    {"symbol":"ICLN","action":"buy","quantity":300,"account":"Roth Contributory IRA","status":"failed","error":"HTTP 400: ..."}
  ]
}
```

### BR-FC-6: `schwab_cancel_order` (gated)

```python
class SchwabCancelOrderTool:
    name = "schwab_cancel_order"
    description = (
        "Cancel a single pending Schwab order. The confirmation prompt "
        "shows the order's symbol, action, quantity, and status before "
        "you approve."
    )
    requires_confirmation = True
    input_schema = {
        "type": "object",
        "properties": {
            "account": {"type": "string"},
            "order_id": {"type": "string", "minLength": 1},
        },
        "required": ["account", "order_id"],
        "additionalProperties": False,
    }
```

**summarize_action():** look up the order, render `schwab_cancel_order: BUY 100 VXUS @ $82.50 (WORKING) in Individual Brokerage — order 1004...`. If the order isn't found or is already in a terminal state, the gate-time `summarize_action` returns a message to that effect and the post-confirm call returns `is_error=True`.

### BR-FC-7: Cap enforcement

A single helper `_enforce_caps(orders, settings, audit_log_path) -> None` runs **before** the gate decision (i.e. inside `call(args)` but as the first thing after schema validation, before assembling the confirmation marker).

Compute:
- `per_order = max(quantity * (limit or last_known_price), 0)`
- `batch = sum(per_order)`
- `daily_used = sum(notional_usd from trade_log for entries with event in ('placed') in last 24h)`
- `daily_total = daily_used + batch`

Raise `ToolError` with one of:
- `"order cap exceeded: VXUS notional $24,500.00 > MAX_ORDER $20,000.00"`
- `"batch cap exceeded: $80,000.00 > MAX_BATCH $75,000.00"`
- `"daily cap exceeded: $105,000.00 > MAX_DAILY $100,000.00 (used $25,000.00 in last 24h)"`

**Market orders require a fresh price basis (hardened 2026-08-04).** A limit order carries its own price, so its notional is exact. A market order does not, and was previously scored against whatever `holdings.json` last said with no age check — so a position cached at $50 that had since run to $500 was scored at a tenth of its true cost and passed a cap that should have stopped it. Market orders now require:

1. the symbol to be present in the cached holdings for that account (otherwise `ToolError: "…has no price basis…"`), **and**
2. that snapshot to be no older than `SCHWAB_MARKET_PRICE_MAX_AGE_SECONDS` (default 900 = 15 min), otherwise `ToolError: "…would be priced off a holdings snapshot 19.0d old…"`.

Both errors name the workaround: pass an explicit `limit`, which bounds the notional exactly *and* bounds the fill. `summarize_action` pre-flights both checks and prepends a `⚠️ WILL BE REFUSED on approval:` block, so the confirmation prompt never asks the user to approve an order that cannot execute. `SCHWAB_FALLBACK_PRICE_USD` consequently no longer participates in the market-order path; it remains only for an unparseable `limit`, which schema validation already prevents.

**The daily cap fails closed (hardened 2026-08-04).** `daily_used` is derived by reading back `placed` events, so the cap is only as good as the log:
- log *missing* → `0.0` (a fresh deploy must not hit a false cap);
- log present but *unreadable* (`OSError`) → `ToolError`, batch refused. It previously returned `0.0` here too, which silently reset the rolling budget at exactly the moment accounting was lost.
- `_audit` returns `bool` and fsyncs. A failed `placed` write cannot be undone — the order is real — so the tool surfaces `audit_write_failed` + a `warning` in its output and logs at ERROR, rather than letting the budget quietly refill.

### BR-FC-8: Auth + token lifecycle

The tools share a single lazily-constructed `schwab.client.Client` per process (cached at module-or-tool-instance level — implementer's choice; tester does not pin which). On first use:

1. Read `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET` from settings.
2. Read `SCHWAB_CALLBACK_URL` (default `https://127.0.0.1:8182`).
3. Call `schwab.auth.easy_client(api_key=..., app_secret=..., callback_url=..., token_path=str(token_path), interactive=False)`.
4. If the token file is missing or unreadable → `AuthError("schwab: not authenticated; run uv run --with schwab-py --with python-dotenv python -m ember.tools.schwab_auth").`. (The auth bootstrap is a separate one-time script — implementer creates a small `__main__` entrypoint or a docs note; the actual one-time auth is run by the user on the host, not the agent.)
5. If `easy_client` raises any other exception → wrap as `AuthError`.
6. Subsequent calls reuse the cached client.

**Test:** invoke any tool with `SCHWAB_APP_KEY=""` → `AuthError`; invoke with valid keys but no token file → `AuthError` with "not authenticated" string. Tester monkeypatches `schwab.auth.easy_client`.

### BR-FC-9: portfolio-review skill (step 5 — DEFER for steps 1-4 build)
`/data/skills/portfolio-review/SKILL.md` reproduces the conversational checkpoint loop from `~/repos/portfolio-advisor/CLAUDE.md`, lightly adapted to call `schwab_*` tools rather than shelling out. **Not implemented in steps 1-4.** The contract here exists so step-5 work can begin without further design.

### BR-FC-10: daily-portfolio-brief skill (step 6 — DEFER)
`/data/skills/daily-portfolio-brief/SKILL.md` defines the email composition. The skill prompt:
1. Calls `schwab_holdings(force_refresh=true)`.
2. Reads `/finance/thesis.md` and `/finance/watchlist.md` via `documents_get_document` / `documents_search_by_tag`.
3. Calls `web_fetch` for 2-3 macro headlines (Bloomberg / Reuters / WSJ headlines page).
4. Composes a markdown brief with sections: Totals, Movers, Crypto Exposure, Macro, Watchlist Deltas, Action Ideas (narrative only).
5. Calls `gmail_send` to `me@example.org` with subject `Portfolio brief — YYYY-MM-DD`.

The skill **must not** include the `schwab_place_orders` tool in its allowed-tools list.

### BR-FC-11: `/schwab-reauth` Discord command (step 7 — DEFER)
A new slash command in `src/ember/commands/schwab_reauth.py`, mirroring the
existing command pattern (`/clear`, `/compact`, `/model`). Two responsibilities:

1. **Command handler** — on `/schwab-reauth`:
   - Calls `schwab.auth.get_auth_context(api_key, callback_url)` (sync, fast).
   - Persists `pending_schwab_reauth = {state, callback_url, expires_at: now+300}`
     onto the conversation row in the `store` (mirror the `pending_tool_confirmation`
     persistence path).
   - Replies with: the auth URL on its own line; a one-line note that the
     redirect page will look broken; a 30-second timing reminder.

2. **Pre-handler in agent loop** — on every inbound message, before invoking the
   provider:
   - If the conversation has `pending_schwab_reauth`, check TTL. If expired,
     clear the state and let the message flow normally.
   - If the message body matches the redirect-URL shape
     (`https://127.0.0.1:8182/?code=...&state=...`), intercept it: call
     `schwab.auth.client_from_received_url(...)` with the persisted
     `auth_context`, write the resulting token to disk, clear the pending
     state, reply with refresh status (token expiry date), and return
     **without** invoking the provider.
   - Otherwise, the message flows to the provider normally.

**Persistence shape:** add a nullable `pending_schwab_reauth` JSON column to the
conversations table, parallel to `pending_tool_confirmation`. Schema migration
in the v9 migration system. Stored fields: `{state: str, callback_url: str,
expires_at: int (unix), pkce_verifier: str | null}`. The `auth_context` returned
by `get_auth_context` is reconstructible from these fields — no need to pickle
the schwab-py object.

**Authorization (TLC-1):** the command + pre-handler obey the same Discord
allowlist as everything else. A non-allowlisted user cannot trigger or
complete a re-auth.

**LLM visibility:** the command is NOT registered as a tool — it's user-only.
The LLM may suggest the user run `/schwab-reauth` in its reply text after seeing
an `AuthError` in a tool result, but it cannot trigger the flow itself.

### BR-FC-12: docs MCP seeds (step 8 — DEFER)
Initial creation of `/finance/thesis.md` (seeded from `~/repos/portfolio-advisor/thesis.md`) and `/finance/watchlist.md` (seeded from `~/repos/portfolio-advisor/watchlist.md`) under tags `finance, thesis` and `finance, watchlist` respectively. One-shot script or manual `mcp__documents__create_document` calls — not application code.

---

## Module / file layout (steps 1-4)

```
src/ember/
  config.py              # add 8 new settings (BR-FC-13)
  main.py                # add _maybe_register_schwab_tools (BR-TLC-8)
  tools/
    schwab.py            # NEW — six tool classes + helpers
    schwab_auth.py       # NEW — optional one-shot CLI for browser auth bootstrap
.env.example             # add 8 new keys (commented)
pyproject.toml           # add schwab-py>=1.5 to dependencies
data/schwab/             # mode 0700, created at first tool use, .gitignored
  .schwab_token.json     # mode 0600
  holdings.json          # mode 0600
  trade_log.jsonl        # mode 0600
tests/
  integration/
    test_tool_schwab.py  # NEW — primary test surface (mirrors test_tool_tiller.py)
  fakes/
    fake_schwab.py       # NEW — fake schwab.client.Client + easy_client
docs/
  brokerage-contracts.md # NEW — mirror of this doc

# step 7 (Discord re-auth) adds:
src/ember/
  commands/
    schwab_reauth.py     # /schwab-reauth slash command
  agent.py               # pre-handler hook for redirect-URL paste detection
  store/
    migrations/v??.sql   # add pending_schwab_reauth column to conversations
tests/integration/
  test_schwab_reauth.py  # command + pre-handler + TTL + state validation
```

---

## Settings additions (BR-FC-13)

Append to `src/ember/config.py` `Settings` class:

```python
    # --- Schwab (steps 1-4) ----------------------------------------------
    # Tools register only when both SCHWAB_APP_KEY and SCHWAB_APP_SECRET
    # are set. Token bootstrap is a one-time browser auth performed on the
    # host (see docs/brokerage-contracts.md "Auth bootstrap"). Subsequent
    # token refreshes happen silently in-process; refresh tokens last ~7
    # days, after which any tool call surfaces AuthError to the LLM and
    # the user runs `/schwab-reauth` in Discord (BR-FC-11) to refresh.
    schwab_app_key: SecretStr | None = None
    schwab_app_secret: SecretStr | None = None
    schwab_callback_url: str = "https://127.0.0.1:8182"
    # Last-4 → friendly name. Format: "1234=Individual Brokerage,5678=Roth"
    schwab_account_names: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)
    # Friendly names allowed for write operations. Empty = read-only on all,
    # write on none.
    schwab_allowed_accounts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Notional caps (USD).
    schwab_max_order_notional_usd: float = 20000.0
    schwab_max_batch_notional_usd: float = 75000.0
    schwab_max_daily_notional_usd: float = 100000.0
    # Conservative fallback when no quote/holdings price is available
    # for a market order during cap evaluation.
    schwab_fallback_price_usd: float = 1000.0
    # Max age of the holdings snapshot a *market* order may be priced
    # from during cap evaluation (see BR-FC-7).
    schwab_market_price_max_age_seconds: int = 900
    # Holdings cache TTL.
    schwab_holdings_cache_seconds: int = 1800
```

`SCHWAB_ACCOUNT_NAMES` is parsed via a `field_validator("schwab_account_names", mode="before")` that splits on `,` and `=` (mirroring the existing portfolio-advisor format and the Discord-allowed-IDs validator pattern). `SCHWAB_ALLOWED_ACCOUNTS` is parsed by a list-from-csv validator (mirroring `gmail_send_allowed_recipients`).

`.env.example` additions:

```dotenv
# --- Schwab brokerage (optional) -------------------------------------------
# Tools register only when both SCHWAB_APP_KEY and SCHWAB_APP_SECRET are set.
# SCHWAB_APP_KEY=
# SCHWAB_APP_SECRET=
# SCHWAB_CALLBACK_URL=https://127.0.0.1:8182
# Comma-separated last4=name pairs.
# SCHWAB_ACCOUNT_NAMES=1234=Individual Brokerage,5678=Roth Contributory IRA
# Comma-separated friendly names allowed for write. Empty = read-only.
# SCHWAB_ALLOWED_ACCOUNTS=Individual Brokerage,Roth Contributory IRA
# Notional caps in USD.
# SCHWAB_MAX_ORDER_NOTIONAL_USD=20000
# SCHWAB_MAX_BATCH_NOTIONAL_USD=75000
# SCHWAB_MAX_DAILY_NOTIONAL_USD=100000
```

---

## Wiring (BR-TLC-8) — `_maybe_register_schwab_tools`

Append to `src/ember/main.py`. Mirrors `_maybe_register_tiller_tools`.

```python
def _maybe_register_schwab_tools(registry: Registry, settings: Settings) -> None:
    if settings.schwab_app_key is None or settings.schwab_app_secret is None:
        _log.info("SCHWAB_APP_KEY/SECRET not set; schwab tools disabled")
        return

    data_dir = settings.data_dir / "schwab"
    data_dir.mkdir(mode=0o700, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
    except OSError:
        pass

    common = {
        "settings": settings,
        "data_dir": data_dir,
    }

    registry.register(SchwabHoldingsTool(**common))
    registry.register(SchwabQuoteTool(**common))
    registry.register(SchwabOpenOrdersTool(**common))
    registry.register(SchwabTransactionsTool(**common))
    registry.register(SchwabPlaceOrdersTool(**common))
    registry.register(SchwabCancelOrderTool(**common))

    _log.info(
        "schwab tools registered (accounts=%d, allowed_writes=%d)",
        len(settings.schwab_account_names),
        len(settings.schwab_allowed_accounts),
    )
```

And add `_maybe_register_schwab_tools(tool_registry, settings)` next to the existing tiller call.

---

## Error mapping

| Schwab/SDK condition | Ember exception |
|---|---|
| HTTP 401 | `AuthError` |
| HTTP 403 | `AuthError` |
| HTTP 404 (specific resource) | `ToolError` |
| HTTP 429 | `ToolRetryable` |
| HTTP 4xx (other) | `ToolError(f"schwab: HTTP {status}: {body[:200]}")` |
| HTTP 5xx | `ToolRetryable` |
| `httpx.ConnectError` / OSError | `TransportError` |
| `httpx.ReadTimeout` | `ToolRetryable` |
| schwab-py raises (other) | `ToolError` |

---

## Test matrix

Tester writes integration tests in `tests/integration/test_tool_schwab.py`. Use the existing `test_tool_tiller.py` mocking strategy: patch the SDK surface, not the tool internals.

Required cases:

### Settings + wiring
- [ ] `_maybe_register_schwab_tools` no-ops with absent app key
- [ ] Registers six tools with full settings
- [ ] Re-registration raises `ValueError` (registry invariant)
- [ ] Settings parser handles `SCHWAB_ACCOUNT_NAMES="1234=Foo,5678=Bar"` correctly
- [ ] Settings parser handles `SCHWAB_ALLOWED_ACCOUNTS=" Foo, Bar "` (whitespace, case)

### Auth
- [ ] No token file → `AuthError` containing "not authenticated"
- [ ] `easy_client` raises generic Exception → wrapped as `AuthError`
- [ ] `AuthError` message does NOT contain `SCHWAB_APP_KEY` or `SCHWAB_APP_SECRET` literal values
- [ ] After successful first call, `easy_client` not called again on second call

### `schwab_holdings`
- [ ] Returns normalized payload matching FC-1 schema
- [ ] Filters to `SCHWAB_ALLOWED_ACCOUNTS` when set; returns all when empty
- [ ] `force_refresh=False` returns from cache when `holdings.json` exists < 30 min old
- [ ] `force_refresh=True` bypasses cache
- [ ] Stale cache (> 30 min) auto-refreshes
- [ ] Atomic write — partial-failure mid-write does not corrupt `holdings.json` (write to temp + rename)
- [ ] File mode after write = 0600
- [ ] HTTP 401 → `AuthError`; HTTP 500 → `ToolRetryable`
- [ ] Expired token + cache present → cache served with `stale: true`, `age_seconds`, `stale_reason`
- [ ] Expired token + no cache → `AuthError` propagates
- [ ] HTTP 401 + cache present → stale cache served (same condition as an expired token)
- [ ] HTTP 500 + cache present → `ToolRetryable`, cache NOT served
- [ ] Ordinary < 30 min cache hit carries no `stale` key

### `schwab_quote`
- [ ] Multiple symbols, including dupes (de-dupe + preserve order)
- [ ] Unknown symbol returns `error: "not_found"` with null prices
- [ ] All-unknown still returns 200 (per-symbol error, not whole-call error)
- [ ] Schema validation rejects `symbols: []`

### `schwab_open_orders`
- [ ] Filters to allowed accounts
- [ ] Status filter applied at query level
- [ ] `account=None` → all allowed accounts

### `schwab_transactions`
- [ ] Required `since`; default `until` = today
- [ ] Filters by transaction type when provided
- [ ] Account allowlist applied (BR-TLC-3 — exact match)
- [ ] Fuzzy account name → `ToolError` listing available names

### `schwab_place_orders`
- [ ] **Without confirmation** → registry returns `confirmation_required` marker; SDK never called
- [ ] **`summarize_action`** renders the format from FC-5 with sells showing gain estimate from holdings
- [ ] Cap exceeded — single order — `ToolError` before gate; SDK never called
- [ ] Cap exceeded — batch sum — `ToolError` before gate
- [ ] Cap exceeded — daily rolling sum — `ToolError`; reads from trade_log.jsonl
- [ ] Market order on an unheld symbol → refused (no price basis)
- [ ] Market order priced off a snapshot older than `SCHWAB_MARKET_PRICE_MAX_AGE_SECONDS` → refused
- [ ] Market order on a fresh snapshot → allowed
- [ ] Limit order with a stale snapshot → allowed (price is explicit)
- [ ] Unreadable trade log → orders refused (daily cap fails closed)
- [ ] Missing trade log → first order allowed ($0 used)
- [ ] Preview flags orders that will be refused before the user approves
- [ ] Allowlist mismatch on any order → `ToolError` before gate
- [ ] Non-interactive context → `is_error=True, output["error"]=="no_interactive_context"`; SDK never called
- [ ] Successful confirm path → SDK `place_order` called once per order with 1s sleep between; results match FC-5 output
- [ ] Mixed batch (one order succeeds, one fails) → returns per-order status; both audit lines written
- [ ] Each `place_order` call audit-logged with `placed` or `failed` and the order_id parsed from `Location` header

### `schwab_cancel_order`
- [ ] Without confirmation → marker
- [ ] `summarize_action` includes the existing order details
- [ ] Order in terminal state → `summarize_action` reports it; post-confirm call returns `is_error=True`
- [ ] Successful cancel writes `canceled` audit entry
- [ ] Allowlist enforced

### Audit log
- [ ] `read` event written for `schwab_holdings` call (no PII beyond friendly name + ticker + qty)
- [ ] `preview` event written before gate (for write tools)
- [ ] `placed`/`failed`/`canceled` events written post-call
- [ ] Trade log file mode = 0600
- [ ] Best-effort: log write failure does not block the tool call (mock open() to raise)

### TLC-3 confirmation flow
- [ ] First call to `schwab_place_orders` without `allow_unconfirmed=True` → marker
- [ ] Second call with `allow_unconfirmed=True` and same args → executes
- [ ] `summarize_action` does not perform any network I/O (uses cached holdings if present, no `easy_client` call)

---

## Conventions for both agents

1. **Match existing style.** Tiller is the closest analog. When in doubt, mirror it.
2. **Lazy imports** for `schwab` and `httpx` — not at module top level. Match the tiller pattern.
3. **No `print()` in tool code** — use `_log = logging.getLogger("ember.tools.schwab")`.
4. **Type hints required** — `from __future__ import annotations` at the top of every new file. `dict[str, Any]`, not `Dict[str, Any]`.
5. **Docstrings for every public class** — describe behavior, not just structure.
6. **No premature abstraction.** Six tools, one helper module if needed (`_caps.py` or inline). Don't extract a base class until two tools clearly share infrastructure (probably client construction — fine to extract).
7. **Atomic file writes** — write to `<path>.tmp` then `os.replace`.
8. **Test fakes go in `tests/fakes/fake_schwab.py`** if reusable; otherwise inline in `test_tool_schwab.py`.
9. **No `requirements.txt`** — the project uses `pyproject.toml` + `uv.lock`. Add `schwab-py>=1.5` to dependencies.
10. **Format with ruff** — line length 100, target py312. No need to run formatter; the pre-commit hook handles it. Just don't write code that obviously violates it.

---

## Out of scope for steps 1-4

- The `portfolio-review` SKILL.md (step 5)
- The `daily-portfolio-brief` SKILL.md + scheduler entry (step 6)
- `/schwab-reauth` Discord command + redirect-URL pre-handler (step 7)
- Documents-MCP seeding (step 8)
- Live network test against Schwab — that's the smoke test that gates 5-8
- Options orders (only equity buy/sell limit/market in v1)
- Fractional shares (Schwab supports them but we keep v1 to whole-share quantities)
- Saved orders / multi-leg orders / OCO / OTOCO
- Streaming quotes (REST polling only)
- Multi-currency (USD only)
- Margin / shorting (long-only equity in v1)

---

## Open ambiguities flagged for the implementer

The implementer may resolve these silently or surface them in the PR description. Either way, the resolution is binding for steps 5-8.

1. **Where does the auth-bootstrap script live?** Options: `src/ember/tools/schwab_auth.py` with `if __name__ == "__main__"`, or a standalone `scripts/schwab_auth.py`. Implementer's call. The `AuthError` message must reference whatever path they pick.
2. **Holdings cache invalidation after a successful write.** When `schwab_place_orders` confirms, the cached `holdings.json` becomes stale immediately. Two options: (a) delete the cache file post-confirm so the next read forces refresh; (b) leave it stale and rely on the 30-min TTL. Recommendation: (a). Tester does not require this behavior; implementer may pick.
3. **Concurrent registration** — if two tests register tools on the same registry, the second raises. The tester's fixtures should construct fresh `Registry()` per test; the implementer doesn't need to handle this.
4. **`schwab.client.Client` async vs sync** — schwab-py exposes both. Use sync (`schwab.auth.easy_client(...)` returns a sync `Client`) and wrap blocking calls with `asyncio.to_thread`, mirroring the tiller pattern. The async `AsyncClient` is faster but adds a different lifecycle to manage.

---

## Acceptance checklist for steps 1-4 smoke test

After implementer + tester merge:

- [ ] `uv run pytest tests/integration/test_tool_schwab.py -v` passes
- [ ] `uv run pytest tests/integration/test_config.py` still passes (settings parsing didn't break)
- [ ] `ruff check src/ember/tools/schwab.py src/ember/main.py src/ember/config.py` clean
- [ ] `python -c "from ember.tools.schwab import SchwabHoldingsTool, SchwabQuoteTool, SchwabOpenOrdersTool, SchwabTransactionsTool, SchwabPlaceOrdersTool, SchwabCancelOrderTool"` imports cleanly
- [ ] On the host, with real Schwab keys filled into `.env`: run the auth bootstrap once → token file appears at `data/schwab/.schwab_token.json` mode 0600
- [ ] `uv run python -c "import asyncio; from ember.config import Settings; from ember.tools.schwab import SchwabHoldingsTool; t = SchwabHoldingsTool(settings=Settings(), data_dir=Path('data/schwab')); print(asyncio.run(t.call({})))"` returns real holdings JSON

If any of the above fail, fix before proceeding to steps 5-8.
