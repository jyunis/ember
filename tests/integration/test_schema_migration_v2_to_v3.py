"""Schema migration v2 → v3 (FC-7, UoW-5).

The v3 schema adds:
- `scheduled_runs` table (UoW-5 audit), with a CHECK constraint on
  `status` ∈ {ok, error, timeout, missed}.
- `scheduled_jobs_state` table (per `(skill, run_name)` enable/disable
  pin that survives reload).
- `schema_version` row 3.

This test reproduces a v2 database by hand (v1 + ALTER for failover +
version=2 row), opens the Store, and asserts:
- No error on open.
- `scheduled_runs` exists with the right columns and CHECK constraint.
- `scheduled_jobs_state` exists.
- `schema_version` contains 1, 2, AND 3.

This file is paired with `test_schema_migration_v1_to_v2.py`. Both
"poke at storage internals" per the FC-7 contract — we treat the
schema file itself as the contract.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from ember.errors import StoreError
from ember.store.repo import SCHEMA_VERSION, Store


# v2 schema text — v1 schema + the failover column on messages.
# Copied from `test_schema_migration_v1_to_v2.py` plus the v1→v2
# migration's effect (failover column + version=2 row).
_V2_SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE conversations (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel                         TEXT NOT NULL,
    channel_conversation_key        TEXT NOT NULL,
    name                            TEXT,
    created_at                      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_active                     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    provider_route                  TEXT NOT NULL DEFAULT 'claude_cli',
    token_budget                    INTEGER NOT NULL DEFAULT 150000,
    claude_cli_session_id           TEXT,
    auto_failover_on_rate_limit     INTEGER NOT NULL DEFAULT 0,
    active_skill                    TEXT,
    UNIQUE (channel, channel_conversation_key)
);

CREATE INDEX idx_conversations_last_active
    ON conversations (last_active DESC);

CREATE TABLE messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id     INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role                TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    content             TEXT NOT NULL,
    provider_route      TEXT,
    tokens_in           INTEGER,
    tokens_out          INTEGER,
    cached_tokens       INTEGER,
    cost_usd            REAL,
    subscription_turn   INTEGER NOT NULL DEFAULT 0,
    failover            INTEGER NOT NULL DEFAULT 0,
    tool_calls_json     TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_messages_conversation_created
    ON messages (conversation_id, created_at);

INSERT INTO schema_version (version) VALUES (1);
INSERT INTO schema_version (version) VALUES (2);
"""


async def _build_v2_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.executescript(_V2_SCHEMA)
        await conn.commit()
    finally:
        await conn.close()


# --- Migration applies ----------------------------------------------------


class TestMigrationApplies:
    async def test_schema_version_is_at_least_3(self) -> None:
        assert SCHEMA_VERSION >= 3

    async def test_open_v2_db_runs_migration_without_error(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v2_db(db_path)

        store = Store(db_path)
        await store.open()
        await store.close()

    async def test_post_migration_scheduled_runs_table_shape(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v2_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("PRAGMA table_info(scheduled_runs)")
            rows = await cur.fetchall()
            await cur.close()
            assert rows, "scheduled_runs table missing after migration"

            col_names = [r[1] for r in rows]
            for col in (
                "id",
                "skill",
                "function",
                "run_name",
                "scheduled_for",
                "started_at",
                "finished_at",
                "status",
                "result_json",
                "error_text",
            ):
                assert col in col_names, f"missing column {col!r}"
        finally:
            await store.close()

    async def test_post_migration_status_check_constraint(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v2_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            # Inserting a bogus status value must fail per the CHECK
            # constraint declared in schema.sql.
            with pytest.raises((aiosqlite.IntegrityError, StoreError)):
                await store._c.execute(
                    "INSERT INTO scheduled_runs "
                    "(skill, function, run_name, status) "
                    "VALUES (?, ?, ?, ?)",
                    ("demo", "tick", "tick", "wat"),
                )
                await store._c.commit()
            # Rollback any aborted transaction so subsequent ops work.
            try:
                await store._c.rollback()
            except Exception:
                pass
        finally:
            await store.close()

    async def test_post_migration_scheduled_jobs_state_table_shape(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v2_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("PRAGMA table_info(scheduled_jobs_state)")
            rows = await cur.fetchall()
            await cur.close()
            assert rows, "scheduled_jobs_state table missing after migration"
            col_names = [r[1] for r in rows]
            assert "skill" in col_names
            assert "run_name" in col_names
            assert "enabled" in col_names
        finally:
            await store.close()

    async def test_schema_version_table_has_versions_1_2_3(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v2_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("SELECT version FROM schema_version ORDER BY version ASC")
            rows = await cur.fetchall()
            await cur.close()
            versions = [r[0] for r in rows]
            assert 1 in versions
            assert 2 in versions
            assert 3 in versions
        finally:
            await store.close()
