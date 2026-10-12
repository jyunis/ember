"""Tiller tool — read-only SQL over the tiller-sync SQLite snapshot.

The tiller-sync service (its own repo, deployed at /srv/tiller-sync)
snapshots the three Tiller Google Sheets (personal, list_armor, wild_north)
into SQLite twice daily; the DB is mounted read-only into this container.
Since 2026-09-16 it also carries the Holdings / Holdings History tabs that
Tiller's Investments feed installs (personal sheet only so far).
`tiller_query` runs a single SELECT/WITH/EXPLAIN against it.

The former live-sheet tools (tiller_list_tabs / tiller_read_tab) were removed:
they could only see the personal sheet, and every question they answered is
cheaper and broader in SQL. Sync freshness is itself queryable via the
sync_runs table.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ember.errors import ToolError, TransportError
from ember.tools.base import ToolResult

_QUERY_SCHEMA_DOC = (
    "Tables (every PK starts with `sheet`, one of 'personal', 'list_armor', "
    "'wild_north' — always filter or group by it): "
    "transactions(sheet, transaction_id, date, description, category, "
    "amount_cents, account, account_num, account_id, tags, institution, "
    "check_number, full_description, date_added, categorized_date, "
    "category_hint, categorized_by, source); "
    "balance_history(sheet, balance_id, date, time, account, account_num, "
    "account_id, institution, balance_cents, type, class, account_status, "
    "date_added, source); "
    'categories(sheet, category, "group", type, hide_from_reports); '
    "budgets(sheet, category, month, amount_cents) — month is 'YYYY-MM'; "
    "accounts(sheet, account_id, account, account_num, institution, type, "
    'class_bh, class, "group", hide, last_update, last_balance_cents); '
    "autocat(sheet, rule_order, category, ...) — categorization rules; "
    "holdings(sheet, holding_id, entry_id, date, time, description, symbol, "
    "quantity, price, value_cents, cost_basis_cents, institution, account, "
    "account_num, account_id, account_type, account_class, holding_type, "
    "source, date_added, date_updated) — current investment positions from "
    "Tiller's Investments feed, one row per holding (personal sheet only); "
    "holdings_history(same columns minus date_updated) — one dated row per "
    "holding per feed run, join to holdings on (sheet, holding_id). Tiller's "
    "feed appends duplicate history rows with identical date/time, so "
    "SELECT DISTINCT or GROUP BY (holding_id, date, time) before counting "
    "or summing; "
    "sync_runs(run_at, sheet, tab, rows_fetched, upserted, deleted, status, "
    "error) — sync audit log, check for freshness. "
    "Money columns are integer cents (divide by 100.0); expenses are "
    "negative amounts. `quantity` (shares) and `price` (dollars per unit) "
    "are REAL, not cents. Dates are ISO 'YYYY-MM-DD' text — SQLite date "
    'functions work on them. `"group"` needs the double quotes.'
)


class TillerQueryTool:
    """Read-only SQL over the tiller-sync SQLite snapshot.

    The DB is produced twice daily (02:00, 11:00) by the tiller-sync
    service and mounted read-only into the container; this tool is the
    way to answer financial questions. Guardrails: read-only connection +
    `query_only` pragma, single SELECT/WITH/EXPLAIN statement only, row
    cap, and a progress-handler abort so a runaway cartesian join can't
    wedge the agent loop.
    """

    name: str = "tiller_query"
    requires_confirmation: bool = False

    def __init__(
        self,
        *,
        db_path: Path,
        default_limit: int = 200,
        max_limit: int = 2000,
    ) -> None:
        self._db_path = db_path
        self._default_limit = default_limit
        self._max_limit = max_limit
        self.description = (
            "Run one read-only SQL statement (SELECT/WITH/EXPLAIN) against the "
            "local Tiller finance database — transaction + balance history for "
            "all three Tiller spreadsheets, synced twice daily (02:00, 11:00). "
            "Use this for any financial question. " + _QUERY_SCHEMA_DOC
        )
        self.input_schema = {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "A single SELECT/WITH/EXPLAIN statement.",
                },
                "row_limit": {
                    "type": "integer",
                    "default": default_limit,
                    "minimum": 1,
                    "maximum": max_limit,
                    "description": f"Max rows to return (1-{max_limit}).",
                },
            },
            "required": ["sql"],
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(db_path={self._db_path!r})"

    __str__ = __repr__

    async def call(self, args: dict[str, Any]) -> ToolResult:
        sql = str(args["sql"])
        limit = int(args.get("row_limit", self._default_limit))
        if limit < 1 or limit > self._max_limit:
            raise ToolError(f"tiller_query: row_limit must be between 1 and {self._max_limit}")
        return await asyncio.to_thread(self._query, sql, limit)

    def _query(self, sql: str, limit: int) -> ToolResult:
        import sqlite3

        # One trailing semicolon is tolerated; sqlite3's execute() itself
        # rejects multi-statement strings.
        stmt = sql.strip().rstrip(";").strip()
        head = stmt.split(None, 1)[0].lower() if stmt else ""
        if head not in ("select", "with", "explain"):
            raise ToolError(
                "tiller_query: only SELECT/WITH/EXPLAIN statements are allowed "
                f"(got {head or 'empty statement'!r})"
            )

        if not self._db_path.exists():
            raise ToolError(f"tiller_query: database not found at {self._db_path}")

        try:
            conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True, timeout=10)
        except sqlite3.Error as e:
            raise TransportError(f"tiller_query: cannot open database: {e}") from None
        try:
            conn.execute("PRAGMA query_only = ON")
            # Abort after ~100M VM steps: generous for real aggregates over
            # ~25k rows, fatal for accidental cartesian products.
            budget = 10_000

            def _guard() -> int:
                nonlocal budget
                budget -= 1
                return 1 if budget <= 0 else 0

            conn.set_progress_handler(_guard, 10_000)

            cur = conn.execute(stmt)
            columns = [d[0] for d in cur.description] if cur.description else []
            fetched = cur.fetchmany(limit + 1)
            truncated = len(fetched) > limit
            rows = [list(r) for r in fetched[:limit]]
        except sqlite3.OperationalError as e:
            if "interrupt" in str(e).lower():
                raise ToolError(
                    "tiller_query: query aborted — it exceeded the execution "
                    "budget (likely an unconstrained join); add filters"
                ) from None
            raise ToolError(f"tiller_query: {e}") from None
        except (sqlite3.Error, sqlite3.Warning) as e:
            raise ToolError(f"tiller_query: {e}") from None
        finally:
            conn.close()

        return ToolResult(
            output={
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": truncated,
            }
        )
