"""Store v=5 — pending tool confirmation (TLC-3 enforcement).

These tests exercise the new schema/repo additions for v11 (TLC-3,
FC-3, TLC-9):

- `conversations.pending_tool_confirmation TEXT NULL` — JSON of the
  paused-tool-call payload `{tool_name, args, action, set_at_iso}`.
- Forward migration v=4 → v=5 (FC-7).
- `Store.set_pending_tool_confirmation` / `consume_pending_tool_confirmation`
  (atomic read+clear, mirroring `set_pending_proposal` /
  `consume_pending_proposal`).
- `Store.clear_conversation` clears `pending_tool_confirmation` along
  with `pending_profile_proposal` (existing /clear semantics).

Tester barrier: we read interface files only — `repo.py`, `types.py`,
`schema.sql`. The new methods do NOT exist yet; ImportError /
AttributeError on first run is expected.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from ember.store.repo import SCHEMA_VERSION, Store


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path):
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


# v=4 schema. Hand-copied from schema.sql at v=4 (i.e. no
# `pending_tool_confirmation` column). The migration test crafts a v=4
# DB with this script and asserts opening it via Store applies the
# v=4 → v=5 migration. We DON'T reach into _MIGRATIONS — we exercise the
# observable migration behavior only.
_V4_SCHEMA = """
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

INSERT INTO schema_version (version) VALUES (4);
"""


async def _build_v4_db(db_path: Path) -> None:
    """Write a fresh v=4 SQLite DB at `db_path` (no
    pending_tool_confirmation column; schema_version=4 row only)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.executescript(_V4_SCHEMA)
        await conn.commit()
    finally:
        await conn.close()


# --- Fresh-init at v=5 has the new column ---------------------------------


class TestFreshV5Schema:
    async def test_schema_version_constant_is_at_least_5(self) -> None:
        # The constant has to advance to 5 (or higher) for the new
        # column to land in fresh installs.
        assert SCHEMA_VERSION >= 5, f"SCHEMA_VERSION must bump to >=5 for v11; got {SCHEMA_VERSION}"

    async def test_conversations_has_pending_tool_confirmation_column(self, store: Store) -> None:
        cur = await store._c.execute("PRAGMA table_info(conversations)")
        rows = await cur.fetchall()
        await cur.close()
        col_names = [r[1] for r in rows]
        assert "pending_tool_confirmation" in col_names, (
            f"conversations is missing pending_tool_confirmation column; got {col_names!r}"
        )

    async def test_pending_tool_confirmation_default_null(self, store: Store) -> None:
        # Newly-inserted conversations must have a NULL
        # pending_tool_confirmation column. (Mirrors how
        # pending_profile_proposal defaults to NULL.)
        c = await store.get_or_create_conversation("discord", "t")
        cur = await store._c.execute(
            "SELECT pending_tool_confirmation FROM conversations WHERE id = ?",
            (c.id,),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None
        assert row[0] is None


# --- Migration v=4 → v=5 ---------------------------------------------------


class TestMigrationV4ToV5:
    async def test_open_v4_db_runs_migration_without_error(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v4_db(db_path)

        store = Store(db_path)
        # Must not raise SchemaVersionError or any other.
        await store.open()
        await store.close()

    async def test_post_migration_pending_tool_confirmation_column_exists(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v4_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("PRAGMA table_info(conversations)")
            rows = await cur.fetchall()
            await cur.close()
            col_names = [r[1] for r in rows]
            assert "pending_tool_confirmation" in col_names
        finally:
            await store.close()

    async def test_post_migration_schema_version_table_records_v5(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v4_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("SELECT version FROM schema_version ORDER BY version ASC")
            rows = await cur.fetchall()
            await cur.close()
            versions = [r[0] for r in rows]
            assert 4 in versions
            assert 5 in versions
        finally:
            await store.close()

    async def test_existing_v4_data_survives_migration(self, tmp_path: Path) -> None:
        # Pre-populate a v=4 DB with a conversation row + a pending
        # profile proposal — neither should be touched by the
        # v=4 → v=5 migration.
        db_path = tmp_path / "conversations.db"
        await _build_v4_db(db_path)

        # Drop one row in directly so we can verify it survives.
        conn = await aiosqlite.connect(db_path)
        try:
            await conn.execute(
                "INSERT INTO conversations (channel, channel_conversation_key, "
                "pending_profile_proposal) VALUES (?, ?, ?)",
                ("discord", "preexisting", '{"keep": true}'),
            )
            await conn.commit()
        finally:
            await conn.close()

        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute(
                "SELECT pending_profile_proposal, pending_tool_confirmation "
                "FROM conversations WHERE channel_conversation_key = ?",
                ("preexisting",),
            )
            row = await cur.fetchone()
            await cur.close()
            assert row is not None
            assert row[0] == '{"keep": true}'
            assert row[1] is None  # new column defaults NULL on existing rows
        finally:
            await store.close()


# --- set/consume round-trip -----------------------------------------------


class TestPendingToolConfirmation:
    async def test_set_and_consume_round_trip(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        payload = (
            '{"tool_name": "destructive", "args": {"to": "x@y"}, '
            '"action": "destructive(to=...)", "set_at_iso": "2026-04-27T12:00:00Z"}'
        )
        await store.set_pending_tool_confirmation(c.id, payload)

        out = await store.consume_pending_tool_confirmation(c.id)
        assert out == payload

    async def test_consume_clears_in_one_shot(self, store: Store) -> None:
        # consume_pending_tool_confirmation returns the prior value AND
        # clears it in the same call (atomic read+clear). A second
        # consume must therefore see no value.
        c = await store.get_or_create_conversation("discord", "t")
        await store.set_pending_tool_confirmation(c.id, '{"x": 1}')

        first = await store.consume_pending_tool_confirmation(c.id)
        assert first == '{"x": 1}'

        second = await store.consume_pending_tool_confirmation(c.id)
        assert second is None

    async def test_consume_on_empty_returns_none(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        out = await store.consume_pending_tool_confirmation(c.id)
        assert out is None

    async def test_set_none_clears(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        await store.set_pending_tool_confirmation(c.id, '{"x": 1}')
        await store.set_pending_tool_confirmation(c.id, None)
        out = await store.consume_pending_tool_confirmation(c.id)
        assert out is None

    async def test_set_overwrites_prior(self, store: Store) -> None:
        c = await store.get_or_create_conversation("discord", "t")
        await store.set_pending_tool_confirmation(c.id, '{"a": 1}')
        await store.set_pending_tool_confirmation(c.id, '{"b": 2}')
        out = await store.consume_pending_tool_confirmation(c.id)
        assert out == '{"b": 2}'

    async def test_isolated_per_conversation(self, store: Store) -> None:
        c1 = await store.get_or_create_conversation("discord", "a")
        c2 = await store.get_or_create_conversation("discord", "b")
        await store.set_pending_tool_confirmation(c1.id, '{"who": "c1"}')
        # c2 should be unaffected.
        out2 = await store.consume_pending_tool_confirmation(c2.id)
        assert out2 is None
        out1 = await store.consume_pending_tool_confirmation(c1.id)
        assert out1 == '{"who": "c1"}'

    async def test_pending_tool_confirmation_independent_of_pending_proposal(
        self, store: Store
    ) -> None:
        # Setting one must not affect the other — they're independent
        # rows on the same conversation. (Coexistence is documented in
        # the spec; the agent's pre-turn handler runs profile-proposal
        # first, tool-confirmation second.)
        c = await store.get_or_create_conversation("discord", "t")
        await store.set_pending_proposal(c.id, '{"profile": "p"}')
        await store.set_pending_tool_confirmation(c.id, '{"tool": "t"}')

        # Consuming one leaves the other intact.
        prof = await store.consume_pending_proposal(c.id)
        assert prof == '{"profile": "p"}'
        # tool-confirmation still set.
        cur = await store._c.execute(
            "SELECT pending_tool_confirmation FROM conversations WHERE id = ?",
            (c.id,),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None
        assert row[0] == '{"tool": "t"}'

        tool = await store.consume_pending_tool_confirmation(c.id)
        assert tool == '{"tool": "t"}'


# --- clear_conversation also clears pending_tool_confirmation -------------


class TestClearConversationClearsToolConfirmation:
    async def test_clear_conversation_clears_pending_tool_confirmation(self, store: Store) -> None:
        # Existing /clear semantics extend to the new column: a /clear
        # must wipe pending_tool_confirmation alongside
        # pending_profile_proposal.
        c = await store.get_or_create_conversation("discord", "t")
        await store.append_message(c.id, "user", "hi")
        await store.set_pending_proposal(c.id, '{"x": 1}')
        await store.set_pending_tool_confirmation(c.id, '{"y": 2}')

        await store.clear_conversation(c.id)

        # Both pending columns are now NULL.
        out_prof = await store.consume_pending_proposal(c.id)
        assert out_prof is None
        out_tool = await store.consume_pending_tool_confirmation(c.id)
        assert out_tool is None

    async def test_clear_conversation_with_only_tool_confirmation_set(self, store: Store) -> None:
        # Defensive: a /clear with ONLY a pending tool confirmation set
        # (no proposal) should still wipe it cleanly.
        c = await store.get_or_create_conversation("discord", "t")
        await store.append_message(c.id, "user", "hi")
        await store.set_pending_tool_confirmation(c.id, '{"y": 2}')

        await store.clear_conversation(c.id)

        out_tool = await store.consume_pending_tool_confirmation(c.id)
        assert out_tool is None
