"""tiller_query (FC-3, TLC-6, TLC-7) — read-only SQL over the tiller-sync DB.

Guardrail surface:
- only SELECT/WITH/EXPLAIN heads accepted (single statement, one trailing
  ';' tolerated);
- writes rejected even when smuggled past the head check (`WITH ... INSERT`
  is valid SQLite) via mode=ro + PRAGMA query_only;
- row cap with truncation flag;
- missing DB file → ToolError, not a crash.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ember.errors import ToolError
from ember.tools.base import Tool, ToolResult
from ember.tools.tiller import TillerQueryTool


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "tiller.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE transactions (
            sheet TEXT, transaction_id TEXT, date TEXT, category TEXT,
            amount_cents INTEGER, PRIMARY KEY (sheet, transaction_id)
        );
        INSERT INTO transactions VALUES
            ('personal', 't1', '2026-08-01', 'Groceries', -1500),
            ('personal', 't2', '2026-08-02', 'Groceries', -2500),
            ('wild_north', 't1', '2026-08-03', 'Supplies', -990);
        """
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def tool(db_path: Path) -> TillerQueryTool:
    return TillerQueryTool(db_path=db_path)


def test_protocol_conformance(tool: TillerQueryTool):
    assert isinstance(tool, Tool)
    assert tool.name == "tiller_query"
    assert tool.requires_confirmation is False
    assert "sql" in tool.input_schema["properties"]
    # The schema doc is the agent's only map of the DB; every synced table is named.
    for table in (
        "transactions",
        "balance_history",
        "categories",
        "budgets",
        "accounts",
        "autocat",
        "holdings",
        "holdings_history",
        "sync_runs",
    ):
        assert f"{table}(" in tool.description


async def test_select_returns_rows(tool: TillerQueryTool):
    result = await tool.call(
        {"sql": "SELECT sheet, SUM(amount_cents) AS total FROM transactions GROUP BY sheet;"}
    )
    assert isinstance(result, ToolResult)
    assert result.output["columns"] == ["sheet", "total"]
    assert sorted(result.output["rows"]) == [["personal", -4000], ["wild_north", -990]]
    assert result.output["truncated"] is False


async def test_row_limit_truncates(tool: TillerQueryTool):
    result = await tool.call({"sql": "SELECT * FROM transactions", "row_limit": 2})
    assert result.output["row_count"] == 2
    assert result.output["truncated"] is True


async def test_non_select_rejected(tool: TillerQueryTool):
    for sql in (
        "INSERT INTO transactions VALUES ('x', 'x', NULL, NULL, 0)",
        "DROP TABLE transactions",
        "PRAGMA journal_mode=DELETE",
        "ATTACH DATABASE '/tmp/x.db' AS x",
        "",
    ):
        with pytest.raises(ToolError):
            await tool.call({"sql": sql})


async def test_with_insert_blocked_by_query_only(tool: TillerQueryTool, db_path: Path):
    # Valid SQLite whose head token passes the allowlist; the read-only
    # connection must still refuse it.
    with pytest.raises(ToolError):
        await tool.call(
            {
                "sql": "WITH c AS (SELECT 1) INSERT INTO transactions SELECT 'x','x',NULL,NULL,1 FROM c"
            }
        )
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 3
    conn.close()


async def test_multi_statement_rejected(tool: TillerQueryTool):
    with pytest.raises(ToolError):
        await tool.call({"sql": "SELECT 1; SELECT 2"})


async def test_bad_row_limit_rejected(tool: TillerQueryTool):
    with pytest.raises(ToolError):
        await tool.call({"sql": "SELECT 1", "row_limit": 99999})


async def test_missing_db_is_tool_error(tmp_path: Path):
    tool = TillerQueryTool(db_path=tmp_path / "nope.db")
    with pytest.raises(ToolError):
        await tool.call({"sql": "SELECT 1"})
