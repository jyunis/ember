"""Schema migration v3 → v4 (FC-7, UoW-12, UoW-8).

The v4 schema adds:
- `messages.compacted` boolean column (default 0).
- `conversations.pending_profile_proposal` TEXT, nullable JSON (UoW-8).
- `summaries(id, conversation_id FK CASCADE, start_message_id,
  end_message_id, summary_text, created_at)` (UoW-12).
- `pinned(id, conversation_id FK CASCADE, content, created_at)`.
- `runtime_config(key TEXT PRIMARY KEY, value TEXT NOT NULL,
  updated_at TEXT)`.
- `schema_version` row 4.

This test reproduces a v3 database by hand (the v3 schema as it stood
when SCHEMA_VERSION was 3), populates it with sample rows
(1 conversation, 5 messages, 1 scheduled_run), opens the Store, and
asserts:
- No error on open.
- Pre-existing rows are intact.
- New columns exist with correct defaults.
- New tables exist.
- `schema_version` contains 1, 2, 3, AND 4.
- The migrated store is functional via the public repo surface (you
  can append, summarize, pin, set runtime config without raising).

Pairs with `test_schema_migration_v1_to_v2.py` and
`test_schema_migration_v2_to_v3.py` — same shape, same conventions.
This file pokes at storage internals (PRAGMA inspections), per the
contracts doc note that schema migrations need internal-knowledge
tests; we treat the schema file itself as the contract.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from ember.providers.base import Usage
from ember.store.repo import SCHEMA_VERSION, Store


# v3 schema text — what `schema.sql` looked like when SCHEMA_VERSION=3.
# Hand-copied so this test never depends on the live schema file (which
# evolves toward v4 and beyond).
_V3_SCHEMA = """
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

INSERT INTO schema_version (version) VALUES (1);
INSERT INTO schema_version (version) VALUES (2);
INSERT INTO schema_version (version) VALUES (3);
"""


async def _build_v3_db(db_path: Path) -> None:
    """Write a fresh v3 SQLite DB at `db_path`, populated with sample
    rows so the migration's row-preservation contract is observable."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.executescript(_V3_SCHEMA)

        # 1 conversation.
        await conn.execute(
            """
            INSERT INTO conversations (channel, channel_conversation_key, name)
            VALUES ('discord', 'thread-1', 'pre-migration')
            """
        )

        # 5 messages on it.
        for role, content in (
            ("user", "u1"),
            ("assistant", "a1"),
            ("user", "u2"),
            ("assistant", "a2"),
            ("user", "u3"),
        ):
            await conn.execute(
                """
                INSERT INTO messages (conversation_id, role, content)
                VALUES (1, ?, ?)
                """,
                (role, content),
            )

        # 1 scheduled_run row, terminal-ok.
        await conn.execute(
            """
            INSERT INTO scheduled_runs (
                skill, function, run_name, scheduled_for, started_at,
                finished_at, status, result_json, error_text
            ) VALUES ('demo', 'tick', 'tick',
                      '2026-04-26T05:00:00Z', '2026-04-26T05:00:01Z',
                      '2026-04-26T05:00:02Z', 'ok', '{"n": 1}', NULL)
            """
        )

        await conn.commit()
    finally:
        await conn.close()


# --- Migration applies cleanly -------------------------------------------


class TestMigrationApplies:
    async def test_schema_version_is_at_least_4(self) -> None:
        assert SCHEMA_VERSION >= 4

    async def test_open_v3_db_runs_migration_without_error(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v3_db(db_path)

        store = Store(db_path)
        await store.open()
        await store.close()

    async def test_post_migration_messages_compacted_column(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v3_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("PRAGMA table_info(messages)")
            rows = await cur.fetchall()
            await cur.close()
            col_names = [r[1] for r in rows]
            assert "compacted" in col_names
        finally:
            await store.close()

    async def test_post_migration_pending_profile_proposal_column(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v3_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("PRAGMA table_info(conversations)")
            rows = await cur.fetchall()
            await cur.close()
            col_names = [r[1] for r in rows]
            assert "pending_profile_proposal" in col_names
        finally:
            await store.close()

    async def test_post_migration_summaries_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v3_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("PRAGMA table_info(summaries)")
            rows = await cur.fetchall()
            await cur.close()
            assert rows, "summaries table missing after migration"
            col_names = [r[1] for r in rows]
            for col in (
                "id",
                "conversation_id",
                "start_message_id",
                "end_message_id",
                "summary_text",
                "created_at",
            ):
                assert col in col_names, f"summaries missing column {col!r}"
        finally:
            await store.close()

    async def test_post_migration_pinned_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v3_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("PRAGMA table_info(pinned)")
            rows = await cur.fetchall()
            await cur.close()
            assert rows, "pinned table missing after migration"
            col_names = [r[1] for r in rows]
            for col in ("id", "conversation_id", "content", "created_at"):
                assert col in col_names, f"pinned missing column {col!r}"
        finally:
            await store.close()

    async def test_post_migration_runtime_config_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v3_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("PRAGMA table_info(runtime_config)")
            rows = await cur.fetchall()
            await cur.close()
            assert rows, "runtime_config table missing after migration"
            col_names = [r[1] for r in rows]
            for col in ("key", "value", "updated_at"):
                assert col in col_names, f"runtime_config missing column {col!r}"
        finally:
            await store.close()

    async def test_schema_version_table_has_versions_1_through_4(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v3_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("SELECT version FROM schema_version ORDER BY version ASC")
            rows = await cur.fetchall()
            await cur.close()
            versions = [r[0] for r in rows]
            for v in (1, 2, 3, 4):
                assert v in versions, f"missing schema_version row {v}"
        finally:
            await store.close()


# --- Pre-existing rows survive the migration ----------------------------


class TestRowsPreserved:
    async def test_conversation_row_intact(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v3_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            again = await store.get_or_create_conversation("discord", "thread-1")
            # Same row id (it was pre-populated as id=1).
            assert again.id == 1
            assert again.name == "pre-migration"
            assert again.channel == "discord"
        finally:
            await store.close()

    async def test_messages_intact_and_uncompacted_by_default(self, tmp_path: Path) -> None:
        # All 5 pre-migration messages must be visible via recent_messages
        # (the new compacted column should default to 0 for existing rows).
        db_path = tmp_path / "conversations.db"
        await _build_v3_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            msgs = await store.recent_messages(1, limit=20)
            contents = [m.content for m in msgs]
            assert contents == ["u1", "a1", "u2", "a2", "u3"]
        finally:
            await store.close()

    async def test_scheduled_run_row_intact(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v3_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            runs = await store.list_runs("demo", limit=10)
            assert len(runs) == 1
            assert runs[0].status == "ok"
            assert runs[0].run_name == "tick"
            assert runs[0].result_json == '{"n": 1}'
        finally:
            await store.close()

    async def test_pending_profile_proposal_defaults_null(self, tmp_path: Path) -> None:
        # Pre-existing conversations should have NULL for the new
        # column, observable as None via consume_pending_proposal.
        db_path = tmp_path / "conversations.db"
        await _build_v3_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            out = await store.consume_pending_proposal(1)
            assert out is None
        finally:
            await store.close()


# --- Migrated store is fully functional ----------------------------------


class TestMigratedStoreUsable:
    async def test_can_append_compact_summarize_pin(self, tmp_path: Path) -> None:
        # End-to-end smoke: post-migration, you can use every new method
        # without raising.
        db_path = tmp_path / "conversations.db"
        await _build_v3_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            # Append a new (post-migration) message.
            new_id = await store.append_message(
                1,
                "assistant",
                "post-migration",
                usage=Usage(input_tokens=1, output_tokens=2, subscription=True),
            )
            assert new_id > 0

            # Mark some pre-migration messages compacted, summarize them.
            old_msgs = await store.recent_messages(1, limit=20, include_compacted=True)
            old_ids = [m.id for m in old_msgs if m.content in ("u1", "a1", "u2")]
            await store.mark_messages_compacted(old_ids)

            sid = await store.insert_summary(
                1,
                start_message_id=min(old_ids),
                end_message_id=max(old_ids),
                summary_text="pre-migration history",
            )
            assert sid > 0

            summaries = await store.list_summaries(1)
            assert len(summaries) == 1

            # Pin survives.
            await store.add_pin(1, "user prefers metric")
            pins = await store.list_pins(1)
            assert len(pins) == 1

            # Runtime config works.
            await store.set_runtime_config("cheap_tier", "google:gemini-2.5-flash")
            assert await store.get_runtime_config("cheap_tier") == "google:gemini-2.5-flash"
        finally:
            await store.close()
