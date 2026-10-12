"""Store methods for `pending_schwab_reauth` + v8→v9 schema migration.

Mirrors the `pending_tool_confirmation` set/get/consume pattern that
already exists in `Store`, plus a v8-to-v9 migration test in the style
of `test_schema_migration_v2_to_v3.py`.

This file is on a test-only branch (`test/brokerage-5-7`). The
implementer is creating these symbols on a parallel branch:
- `Store.set_pending_schwab_reauth(conv_id, payload_json | None)`
- `Store.get_pending_schwab_reauth(conv_id) -> str | None`
- `Store.consume_pending_schwab_reauth(conv_id) -> str | None`
- `_MIGRATIONS[8]` = list of statements that adds
  `conversations.pending_schwab_reauth TEXT NULL` and inserts
  `schema_version=9`.
- `SCHEMA_VERSION = 9` in `ember.store.repo`.

We hand-build a v=8 DB by reproducing the schema as the running
implementation would have written it, then open the Store and assert:
 - SCHEMA_VERSION advanced to >= 9;
 - `conversations.pending_schwab_reauth` column exists;
 - schema_version row 9 inserted;
 - existing data preserved (pending_tool_confirmation still readable;
   pending_schwab_reauth is NULL on pre-existing rows).
"""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from ember.store.repo import SCHEMA_VERSION, Store


# ---------------------------------------------------------------------------
# Store method round-trip tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def opened_store(tmp_path: Path):
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


class TestPendingSchwabReauthRepo:
    async def test_pending_schwab_reauth_set_get_consume_roundtrip(
        self, opened_store: Store
    ) -> None:
        """set → get returns same blob; consume returns blob and clears;
        second consume returns None."""
        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        payload = json.dumps(
            {
                "state": "ABC",
                "callback_url": "https://127.0.0.1:8182",
                "expires_at": 1234567890,
                "pkce_verifier": None,
            }
        )
        await opened_store.set_pending_schwab_reauth(conv.id, payload)

        # Get returns the persisted blob.
        got = await opened_store.get_pending_schwab_reauth(conv.id)
        assert got == payload, f"get returned {got!r}, expected {payload!r}"

        # Consume returns the prior value AND clears.
        prior = await opened_store.consume_pending_schwab_reauth(conv.id)
        assert prior == payload, f"consume returned {prior!r}, expected {payload!r}"

        # Second consume returns None — value is gone.
        again = await opened_store.consume_pending_schwab_reauth(conv.id)
        assert again is None, f"second consume should return None; got {again!r}"

        # And get also returns None now.
        gone = await opened_store.get_pending_schwab_reauth(conv.id)
        assert gone is None, f"get after consume should return None; got {gone!r}"

    async def test_pending_schwab_reauth_clear_via_set_none(self, opened_store: Store) -> None:
        """`set_pending_schwab_reauth(conv_id, None)` clears the column.
        Mirrors `set_pending_tool_confirmation`'s carve-out."""
        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        await opened_store.set_pending_schwab_reauth(conv.id, '{"state":"X"}')
        assert (await opened_store.get_pending_schwab_reauth(conv.id)) is not None

        await opened_store.set_pending_schwab_reauth(conv.id, None)
        assert await opened_store.get_pending_schwab_reauth(conv.id) is None

    async def test_pending_schwab_reauth_consume_is_atomic(self, opened_store: Store) -> None:
        """Single-process atomicity: get observes the value before
        consume cleared it. (Not a concurrency stress test — the
        contract is that within the consume call, the read happens
        before the write, so consume's return value is the value the
        column had when entering the method.)"""
        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        payload = '{"state": "atomic_test", "expires_at": 99999}'
        await opened_store.set_pending_schwab_reauth(conv.id, payload)

        # Verify the read-then-clear within consume is atomic by:
        #  1. observing the value via `get` BEFORE consume runs.
        before = await opened_store.get_pending_schwab_reauth(conv.id)
        assert before == payload

        #  2. calling consume — receives the value then clears.
        out = await opened_store.consume_pending_schwab_reauth(conv.id)
        assert out == payload, f"consume must return the pre-clear value; got {out!r}"

        #  3. confirm clear actually happened in the same call.
        after = await opened_store.get_pending_schwab_reauth(conv.id)
        assert after is None

    async def test_pending_schwab_reauth_isolated_per_conversation(
        self, opened_store: Store
    ) -> None:
        """Setting pending state on one conversation must not affect
        another conversation's row."""
        conv1 = await opened_store.get_or_create_conversation("discord", "thread-1")
        conv2 = await opened_store.get_or_create_conversation("discord", "thread-2")

        await opened_store.set_pending_schwab_reauth(conv1.id, '{"state":"one"}')
        # conv2 untouched.
        assert await opened_store.get_pending_schwab_reauth(conv2.id) is None
        assert await opened_store.get_pending_schwab_reauth(conv1.id) == '{"state":"one"}'

        # Consuming conv1 leaves conv2 unchanged.
        await opened_store.consume_pending_schwab_reauth(conv1.id)
        # Set conv2 — conv1 still empty.
        await opened_store.set_pending_schwab_reauth(conv2.id, '{"state":"two"}')
        assert await opened_store.get_pending_schwab_reauth(conv1.id) is None
        assert await opened_store.get_pending_schwab_reauth(conv2.id) == '{"state":"two"}'


# ---------------------------------------------------------------------------
# v8 → v9 migration
# ---------------------------------------------------------------------------


# v=8 schema text — replays the schema.sql that was current on
# `feature/brokerage` BEFORE the v9 migration. Keeps the test independent
# of further changes to schema.sql (we want to assert the migration steps
# specifically, not the live schema).
_V8_SCHEMA = """
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
    pending_profile_proposal        TEXT,
    pending_tool_confirmation       TEXT,
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
    compacted           INTEGER NOT NULL DEFAULT 0,
    subagent_skill      TEXT,
    parent_message_id   INTEGER,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_messages_conversation_created
    ON messages (conversation_id, created_at);

CREATE TABLE scheduled_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    skill           TEXT NOT NULL,
    function        TEXT NOT NULL,
    run_name        TEXT NOT NULL,
    scheduled_for   TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    status          TEXT NOT NULL CHECK (status IN ('ok', 'error', 'timeout', 'missed')),
    result_json     TEXT,
    error_text      TEXT
);

CREATE INDEX idx_scheduled_runs_skill_started
    ON scheduled_runs (skill, started_at DESC);

CREATE TABLE scheduled_jobs_state (
    skill       TEXT NOT NULL,
    run_name    TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (skill, run_name)
);

CREATE TABLE summaries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id     INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    start_message_id    INTEGER NOT NULL,
    end_message_id      INTEGER NOT NULL,
    summary_text        TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_summaries_conversation_start
    ON summaries (conversation_id, start_message_id);

CREATE TABLE pinned (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id     INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    content             TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_pinned_conversation
    ON pinned (conversation_id, id);

CREATE TABLE runtime_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE connected_mcps (
    name         TEXT PRIMARY KEY,
    transport    TEXT NOT NULL CHECK (transport IN ('streamableHttp', 'sse')),
    url          TEXT NOT NULL,
    headers_json TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT INTO schema_version (version) VALUES (1);
INSERT INTO schema_version (version) VALUES (2);
INSERT INTO schema_version (version) VALUES (3);
INSERT INTO schema_version (version) VALUES (4);
INSERT INTO schema_version (version) VALUES (5);
INSERT INTO schema_version (version) VALUES (6);
INSERT INTO schema_version (version) VALUES (7);
INSERT INTO schema_version (version) VALUES (8);
"""


async def _build_v8_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.executescript(_V8_SCHEMA)
        await conn.commit()
    finally:
        await conn.close()


class TestSchemaV8ToV9Migration:
    async def test_schema_version_is_at_least_9(self) -> None:
        """The v9 migration must bump SCHEMA_VERSION."""
        assert SCHEMA_VERSION >= 9, (
            f"expected SCHEMA_VERSION >= 9 after v9 migration; got {SCHEMA_VERSION}"
        )

    async def test_open_v8_db_runs_migration_without_error(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v8_db(db_path)

        store = Store(db_path)
        await store.open()
        await store.close()

    async def test_v8_to_v9_migration_adds_pending_schwab_reauth_column(
        self, tmp_path: Path
    ) -> None:
        """After the v8→v9 migration, `conversations.pending_schwab_reauth`
        must exist (TEXT, nullable)."""
        db_path = tmp_path / "conversations.db"
        await _build_v8_db(db_path)

        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("PRAGMA table_info(conversations)")
            rows = await cur.fetchall()
            await cur.close()
            col_names = [r[1] for r in rows]
            assert "pending_schwab_reauth" in col_names, (
                f"expected `pending_schwab_reauth` column on conversations; "
                f"got columns: {col_names!r}"
            )
            # Confirm the column type is TEXT and nullable.
            for r in rows:
                if r[1] == "pending_schwab_reauth":
                    # PRAGMA table_info row: (cid, name, type, notnull, dflt_value, pk)
                    assert r[2].upper() == "TEXT", (
                        f"pending_schwab_reauth type: expected TEXT, got {r[2]!r}"
                    )
                    assert r[3] == 0, f"pending_schwab_reauth must be nullable; notnull={r[3]!r}"
        finally:
            await store.close()

    async def test_v8_to_v9_migration_records_version_9(self, tmp_path: Path) -> None:
        """`schema_version` table contains a row with version=9 after migration."""
        db_path = tmp_path / "conversations.db"
        await _build_v8_db(db_path)

        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("SELECT version FROM schema_version ORDER BY version ASC")
            rows = await cur.fetchall()
            await cur.close()
            versions = [r[0] for r in rows]
            assert 8 in versions
            assert 9 in versions, f"expected version=9 row in schema_version; got {versions!r}"
        finally:
            await store.close()

    async def test_existing_data_preserved_through_migration(self, tmp_path: Path) -> None:
        """Write a row in v8 with an existing pending_tool_confirmation
        value; migrate to v9; assert the prior data survived and
        pending_schwab_reauth is NULL on that row."""
        db_path = tmp_path / "conversations.db"
        await _build_v8_db(db_path)

        # Pre-seed v8 row with a pending_tool_confirmation value.
        conn = await aiosqlite.connect(db_path)
        try:
            await conn.execute(
                """
                INSERT INTO conversations
                  (channel, channel_conversation_key, pending_tool_confirmation)
                VALUES (?, ?, ?)
                """,
                ("discord", "thread-pre", '{"tool_name":"x","args":{}}'),
            )
            await conn.commit()
        finally:
            await conn.close()

        # Migrate by opening Store.
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute(
                """
                SELECT pending_tool_confirmation, pending_schwab_reauth
                FROM conversations
                WHERE channel_conversation_key = ?
                """,
                ("thread-pre",),
            )
            row = await cur.fetchone()
            await cur.close()
            assert row is not None, "pre-existing conversation row vanished"
            ptc, psr = row[0], row[1]
            assert ptc == '{"tool_name":"x","args":{}}', (
                f"pending_tool_confirmation must be preserved; got {ptc!r}"
            )
            assert psr is None, (
                f"pending_schwab_reauth must be NULL on pre-existing rows; got {psr!r}"
            )
        finally:
            await store.close()

    async def test_post_migration_set_get_works_on_pre_existing_row(self, tmp_path: Path) -> None:
        """Smoke test: after migration, the new repo methods work on a
        row that was inserted under v8."""
        db_path = tmp_path / "conversations.db"
        await _build_v8_db(db_path)

        conn = await aiosqlite.connect(db_path)
        try:
            await conn.execute(
                "INSERT INTO conversations (channel, channel_conversation_key) VALUES (?, ?)",
                ("discord", "thread-pre-v8"),
            )
            await conn.commit()
        finally:
            await conn.close()

        store = Store(db_path)
        await store.open()
        try:
            conv = await store.get_or_create_conversation("discord", "thread-pre-v8")
            await store.set_pending_schwab_reauth(
                conv.id, '{"state":"after_migration","expires_at":1}'
            )
            got = await store.get_pending_schwab_reauth(conv.id)
            assert got == '{"state":"after_migration","expires_at":1}'
            consumed = await store.consume_pending_schwab_reauth(conv.id)
            assert consumed == '{"state":"after_migration","expires_at":1}'
            assert await store.get_pending_schwab_reauth(conv.id) is None
        finally:
            await store.close()
