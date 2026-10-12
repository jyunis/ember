"""Schema migration v14 → v15 (FC-7, doc #56: honest context accounting).

The v15 schema adds (FC-28):
- `messages.cache_creation_tokens` INTEGER NULL.
- `conversations.last_context_tokens` INTEGER NULL.
- `schema_version` row 15.

This test reproduces a v14 database by hand (the schema as it stood
when SCHEMA_VERSION was 14, hand-copied so this test never depends on
the live schema file), populates it with sample rows, opens the Store,
and asserts:

- No error on open; `schema_version` contains 15.
- Both new columns exist and are NULL for pre-existing rows.
- Pre-existing rows intact (conversation, messages, summary).
- The new `Store.update_last_context_tokens` single-column update
  round-trips through `Conversation.last_context_tokens`.

Pairs with `test_schema_migration_v13_to_v14.py` — same shape, same
conventions. Migration internals are barrier-exempt (doc #7 precedent),
but assertions go through the public Store API where possible.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from ember.store.repo import SCHEMA_VERSION, Store


# v14 schema text — what `schema.sql` looked like when SCHEMA_VERSION=14.
# Hand-copied so this test never depends on the live schema file (which
# evolves toward v15 and beyond).
_V14_SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS conversations (
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
    pending_schwab_reauth           TEXT,
    archived_at                     TEXT,
    UNIQUE (channel, channel_conversation_key)
);

CREATE INDEX IF NOT EXISTS idx_conversations_last_active
    ON conversations (last_active DESC);

CREATE TABLE IF NOT EXISTS messages (
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

CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
    ON messages (conversation_id, created_at);

CREATE TABLE IF NOT EXISTS scheduled_runs (
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

CREATE INDEX IF NOT EXISTS idx_scheduled_runs_skill_started
    ON scheduled_runs (skill, started_at DESC);

CREATE TABLE IF NOT EXISTS scheduled_jobs_state (
    skill       TEXT NOT NULL,
    run_name    TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (skill, run_name)
);

CREATE TABLE IF NOT EXISTS summaries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id     INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    start_message_id    INTEGER NOT NULL,
    end_message_id      INTEGER NOT NULL,
    summary_text        TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_summaries_conversation_start
    ON summaries (conversation_id, start_message_id);

CREATE TABLE IF NOT EXISTS pinned (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id     INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    content             TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_pinned_conversation
    ON pinned (conversation_id, id);

CREATE TABLE IF NOT EXISTS runtime_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS connected_mcps (
    name         TEXT PRIMARY KEY,
    transport    TEXT NOT NULL CHECK (transport IN ('streamableHttp', 'sse')),
    url          TEXT NOT NULL,
    headers_json TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS feedback (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id     INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id          INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    emoji               TEXT NOT NULL,
    sentiment           INTEGER,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    removed_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_feedback_message
    ON feedback (message_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_live_unique
    ON feedback (message_id, emoji) WHERE removed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_feedback_created
    ON feedback (created_at) WHERE removed_at IS NULL;

CREATE TABLE IF NOT EXISTS channel_message_map (
    channel             TEXT NOT NULL,
    channel_message_id  TEXT NOT NULL,
    message_id          INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (channel, channel_message_id)
);

CREATE INDEX IF NOT EXISTS idx_channel_message_map_message
    ON channel_message_map (message_id);

CREATE TABLE IF NOT EXISTS eval_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at     TEXT,
    selector        TEXT,
    fingerprint     TEXT NOT NULL,
    identity_sha    TEXT,
    skills_hash     TEXT,
    provider_route  TEXT,
    image_version   TEXT,
    total_cases     INTEGER NOT NULL DEFAULT 0,
    passed          INTEGER NOT NULL DEFAULT 0,
    failed          INTEGER NOT NULL DEFAULT 0,
    errored         INTEGER NOT NULL DEFAULT 0,
    skipped         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_started
    ON eval_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS eval_case_results (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    case_id             TEXT NOT NULL,
    kind                TEXT NOT NULL,
    trial               INTEGER NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('pass', 'fail', 'error')),
    score               INTEGER,
    judge_reason        TEXT,
    detail              TEXT,
    tokens_in           INTEGER,
    tokens_out          INTEGER,
    cached_tokens       INTEGER,
    cost_usd            REAL,
    subscription_turn   INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_eval_case_results_run
    ON eval_case_results (run_id, case_id, trial);

CREATE TABLE IF NOT EXISTS proposals (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    kind                 TEXT NOT NULL CHECK (kind IN ('profile', 'skill', 'trigger', 'lesson', 'finding')),
    summary              TEXT NOT NULL,
    payload_json         TEXT NOT NULL,
    evidence_json        TEXT NOT NULL,
    payload_hash         TEXT NOT NULL,
    reinforced_count     INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'approved', 'rejected', 'applied', 'expired')),
    resolved_at          TEXT,
    applied_eval_run_id  INTEGER REFERENCES eval_runs(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_pending_dedup
    ON proposals (kind, payload_hash) WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_proposals_status_created
    ON proposals (status, created_at);

CREATE TABLE IF NOT EXISTS lessons (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    text                TEXT NOT NULL,
    evidence_json       TEXT NOT NULL,
    dedup_key           TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_reinforced_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    reinforced_count    INTEGER NOT NULL DEFAULT 0,
    source_proposal_id  INTEGER REFERENCES proposals(id),
    removed_at          TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_lessons_live_dedup
    ON lessons (dedup_key) WHERE removed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_lessons_live_reinforced
    ON lessons (last_reinforced_at) WHERE removed_at IS NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages
WHEN new.role IN ('user', 'assistant')
BEGIN
    INSERT INTO messages_fts (rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages
WHEN old.role IN ('user', 'assistant')
BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages
WHEN old.role IN ('user', 'assistant') OR new.role IN ('user', 'assistant')
BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, content)
        SELECT 'delete', old.id, old.content WHERE old.role IN ('user', 'assistant');
    INSERT INTO messages_fts (rowid, content)
        SELECT new.id, new.content WHERE new.role IN ('user', 'assistant');
END;

INSERT OR IGNORE INTO schema_version (version) VALUES (1);
INSERT OR IGNORE INTO schema_version (version) VALUES (2);
INSERT OR IGNORE INTO schema_version (version) VALUES (3);
INSERT OR IGNORE INTO schema_version (version) VALUES (4);
INSERT OR IGNORE INTO schema_version (version) VALUES (5);
INSERT OR IGNORE INTO schema_version (version) VALUES (6);
INSERT OR IGNORE INTO schema_version (version) VALUES (7);
INSERT OR IGNORE INTO schema_version (version) VALUES (8);
INSERT OR IGNORE INTO schema_version (version) VALUES (9);
INSERT OR IGNORE INTO schema_version (version) VALUES (10);
INSERT OR IGNORE INTO schema_version (version) VALUES (11);
INSERT OR IGNORE INTO schema_version (version) VALUES (12);
INSERT OR IGNORE INTO schema_version (version) VALUES (13);
INSERT OR IGNORE INTO schema_version (version) VALUES (14);
"""


async def _build_v14_db(db_path: Path) -> None:
    """Write a fresh v14 SQLite DB at `db_path`, populated with sample
    rows so the migration's row-preservation contract is observable:
    one conversation with a session id, three messages (one with token
    usage), and a summary row."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.executescript(_V14_SCHEMA)

        await conn.execute(
            """
            INSERT INTO conversations
                (channel, channel_conversation_key, name, claude_cli_session_id)
            VALUES ('discord', 'thread-1', 'pre-migration', 'sess-old')
            """
        )

        for role, content, tin, tout in (
            ("user", "what does the porcupine eat", None, None),
            ("assistant", "mostly bark and roots", 12, 34),
            ("tool", "tool payload raw json blob", None, None),
        ):
            await conn.execute(
                """
                INSERT INTO messages (conversation_id, role, content, tokens_in, tokens_out)
                VALUES (1, ?, ?, ?, ?)
                """,
                (role, content, tin, tout),
            )

        await conn.execute(
            """
            INSERT INTO summaries (conversation_id, start_message_id, end_message_id, summary_text)
            VALUES (1, 1, 2, 'talked about porcupines')
            """
        )

        await conn.commit()
    finally:
        await conn.close()


# --- Migration applies cleanly ---------------------------------------------


class TestMigrationApplies:
    async def test_schema_version_is_at_least_15(self) -> None:
        assert SCHEMA_VERSION >= 15

    async def test_open_v14_db_runs_migration_without_error(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v14_db(db_path)

        store = Store(db_path)
        await store.open()
        await store.close()

    async def test_schema_version_table_has_version_15(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v14_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("SELECT version FROM schema_version ORDER BY version ASC")
            rows = await cur.fetchall()
            await cur.close()
            versions = [r[0] for r in rows]
            for v in (1, 14, 15):
                assert v in versions, f"missing schema_version row {v}"
        finally:
            await store.close()

    async def test_messages_cache_creation_tokens_column_exists(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v14_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("PRAGMA table_info(messages)")
            rows = await cur.fetchall()
            await cur.close()
            col_names = [r[1] for r in rows]
            assert "cache_creation_tokens" in col_names

            # NULL on pre-existing rows (never backfilled).
            cur = await store._c.execute(
                "SELECT cache_creation_tokens FROM messages WHERE conversation_id = 1"
            )
            vals = [r[0] for r in await cur.fetchall()]
            await cur.close()
            assert vals == [None, None, None]
        finally:
            await store.close()

    async def test_conversations_last_context_tokens_column_exists(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v14_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("PRAGMA table_info(conversations)")
            rows = await cur.fetchall()
            await cur.close()
            col_names = [r[1] for r in rows]
            assert "last_context_tokens" in col_names

            # Observable through the hydrated Conversation (FC-28).
            conv = await store.get_conversation(1)
            assert conv is not None
            assert conv.last_context_tokens is None
        finally:
            await store.close()


# --- Pre-existing rows survive the migration -------------------------------


class TestRowsPreserved:
    async def test_conversation_and_messages_intact(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v14_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            conv = await store.get_or_create_conversation("discord", "thread-1")
            assert conv.id == 1
            assert conv.name == "pre-migration"
            assert conv.claude_cli_session_id == "sess-old"

            msgs = await store.recent_messages(1, limit=20)
            assert [m.role for m in msgs] == ["user", "assistant", "tool"]
            assert msgs[0].content == "what does the porcupine eat"
            assert await store.message_count(1) == 3

            summaries = await store.list_summaries(1)
            assert len(summaries) == 1
            assert summaries[0].summary_text == "talked about porcupines"
        finally:
            await store.close()


# --- New single-column update round-trips ----------------------------------


class TestUpdateLastContextTokens:
    async def test_round_trip_and_clear(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v14_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            await store.update_last_context_tokens(1, 51_207)
            conv = await store.get_conversation(1)
            assert conv is not None
            assert conv.last_context_tokens == 51_207

            await store.update_last_context_tokens(1, None)
            conv2 = await store.get_conversation(1)
            assert conv2 is not None
            assert conv2.last_context_tokens is None
        finally:
            await store.close()
