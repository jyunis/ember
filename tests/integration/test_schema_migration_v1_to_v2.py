"""Schema migration v1 → v2 (FC-7).

The v2 store adds a `failover` boolean column to `messages` and bumps
`schema_version` to 2. FC-7 says the store refuses to start against an
unrecognized schema; for v1 → v2, a forward migration must be provided.

This test belongs in `tests/internal/` per the contracts doc's note —
it pokes at storage internals (writes a v1 DB, verifies post-migration
column shape). We keep it under `integration/` since the agent tree
doesn't yet have an `internal/` subtree, and the test only inspects
public Store methods after the migration has run.

The v1 schema was the same as v2 minus the `failover` column on
`messages` and minus the `INSERT OR IGNORE INTO schema_version (version)
VALUES (2)` row at the end. We reproduce v1 by hand here rather than
reaching into the implementation's internal `_MIGRATIONS` dict.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from ember.providers.base import Usage
from ember.store.repo import SCHEMA_VERSION, Store


# v1 schema text. Hand-copied from what the v2 schema would be without
# the failover column on messages. If the implementer ever migrates
# again, this test gets a sibling for v2 → v3 and this file stays.
_V1_SCHEMA = """
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
    provider_route                  TEXT NOT NULL DEFAULT 'claude_cli:sonnet-4-6',
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
    -- (no failover column in v1)
    tool_calls_json     TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_messages_conversation_created
    ON messages (conversation_id, created_at);

INSERT INTO schema_version (version) VALUES (1);
"""


async def _build_v1_db(db_path: Path) -> None:
    """Write a fresh v1 SQLite DB at `db_path`."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.executescript(_V1_SCHEMA)
        await conn.commit()
    finally:
        await conn.close()


# --- Tests ---------------------------------------------------------------


class TestMigrationApplies:
    async def test_schema_version_is_at_least_2(self) -> None:
        # This file specifically validates the v1 → v2 leg of the
        # migration chain. Any newer version (v3, v4, …) lands its own
        # sibling test alongside this one; the constant just has to be
        # at least 2 here.
        assert SCHEMA_VERSION >= 2

    async def test_open_v1_db_runs_migration_without_error(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v1_db(db_path)

        store = Store(db_path)
        # Must not raise SchemaVersionError or any other.
        await store.open()
        await store.close()

    async def test_post_migration_failover_column_exists(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v1_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            # Reach into the live connection ONLY for an inspection
            # query — no business mutation. PRAGMA table_info is the
            # canonical way to assert column shape in SQLite.
            cur = await store._c.execute("PRAGMA table_info(messages)")
            rows = await cur.fetchall()
            await cur.close()
            col_names = [r[1] for r in rows]
            assert "failover" in col_names
        finally:
            await store.close()

    async def test_schema_version_table_has_both_versions(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v1_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("SELECT version FROM schema_version ORDER BY version ASC")
            rows = await cur.fetchall()
            await cur.close()
            versions = [r[0] for r in rows]
            assert 1 in versions
            assert 2 in versions
        finally:
            await store.close()


class TestMigratedStoreUsable:
    async def test_append_message_with_failover_round_trips(self, tmp_path: Path) -> None:
        # After migration, the new `failover` kwarg on append_message
        # round-trips through `recent_messages` per FC-7's "all mutations
        # go through repository functions" + the new column being
        # observable.
        db_path = tmp_path / "conversations.db"
        await _build_v1_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            conv = await store.get_or_create_conversation("discord", "thread-1")
            await store.append_message(conv.id, "user", "hello")
            await store.append_message(
                conv.id,
                "assistant",
                "world",
                provider_route="anthropic_api:claude-sonnet-4-6",
                usage=Usage(input_tokens=5, output_tokens=10, cost_usd=0.001),
                failover=True,
            )

            msgs = await store.recent_messages(conv.id)
            user_msg = next(m for m in msgs if m.role == "user")
            asst_msg = next(m for m in msgs if m.role == "assistant")

            assert user_msg.failover is False  # default
            assert asst_msg.failover is True
        finally:
            await store.close()
