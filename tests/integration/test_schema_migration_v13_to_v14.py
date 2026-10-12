"""Schema migration v13 → v14 (FC-7, doc #54: archival + history FTS).

The v14 schema adds:
- `conversations.archived_at` TEXT NULL (NULL = live).
- `messages_fts` — FTS5 external-content table over `messages.content`
  for user/assistant rows, kept in sync by triggers.
- Backfill of pre-existing user/assistant rows into the index.
- `schema_version` row 14.

This test reproduces a v13 database by hand (the schema as it stood
when SCHEMA_VERSION was 13, hand-copied so this test never depends on
the live schema file), populates it with sample rows (1 conversation;
user, assistant, AND tool messages), opens the Store, and asserts:

- No error on open; `schema_version` contains 14.
- `archived_at` column exists and is NULL for pre-existing rows.
- FTS backfill: pre-existing user/assistant content is searchable via
  `search_history`; tool-role content is NOT.
- Messages appended AFTER migration are searchable (trigger sync).
- Pre-existing rows intact.

Pairs with `test_store_migration.py` / `test_schema_migration_v2_to_v3.py`
— same shape, same conventions. Migration internals are barrier-exempt
(doc #7 precedent), but assertions go through the public Store API where
possible.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from ember.store.repo import SCHEMA_VERSION, Store


# v13 schema text — what `schema.sql` looked like when SCHEMA_VERSION=13.
# Hand-copied so this test never depends on the live schema file (which
# evolves toward v14 and beyond).
_V13_SCHEMA = """
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
"""


async def _build_v13_db(db_path: Path) -> None:
    """Write a fresh v13 SQLite DB at `db_path`, populated with sample
    rows so the migration's backfill + row-preservation contracts are
    observable. Content terms are chosen unique-per-role so search
    results identify which rows got indexed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.executescript(_V13_SCHEMA)

        await conn.execute(
            """
            INSERT INTO conversations (channel, channel_conversation_key, name)
            VALUES ('discord', 'thread-1', 'pre-migration')
            """
        )

        for role, content in (
            ("user", "please remind me about the flamingo project deadline"),
            ("assistant", "the flamingo project deadline is next tuesday"),
            ("tool", "tool payload zebrahammer flamingo raw json blob"),
        ):
            await conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (1, ?, ?)",
                (role, content),
            )

        await conn.commit()
    finally:
        await conn.close()


# --- Migration applies cleanly -------------------------------------------


class TestMigrationApplies:
    async def test_schema_version_is_at_least_14(self) -> None:
        assert SCHEMA_VERSION >= 14

    async def test_open_v13_db_runs_migration_without_error(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v13_db(db_path)

        store = Store(db_path)
        await store.open()
        await store.close()

    async def test_schema_version_table_has_version_14(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v13_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("SELECT version FROM schema_version ORDER BY version ASC")
            rows = await cur.fetchall()
            await cur.close()
            versions = [r[0] for r in rows]
            for v in (1, 13, 14):
                assert v in versions, f"missing schema_version row {v}"
        finally:
            await store.close()

    async def test_archived_at_column_exists_and_defaults_null(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v13_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("PRAGMA table_info(conversations)")
            rows = await cur.fetchall()
            await cur.close()
            col_names = [r[1] for r in rows]
            assert "archived_at" in col_names

            # Pre-existing conversation is live (NULL stamp), observable
            # through the public surface.
            conv = await store.get_conversation(1)
            assert conv is not None
            assert conv.archived_at is None
        finally:
            await store.close()

    async def test_messages_fts_table_exists(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v13_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute(
                "SELECT name FROM sqlite_master WHERE name = 'messages_fts'"
            )
            rows = await cur.fetchall()
            await cur.close()
            assert rows, "messages_fts virtual table missing after migration"
        finally:
            await store.close()


# --- FTS backfill + trigger sync ------------------------------------------


class TestFtsBackfillAndTriggers:
    async def test_pre_existing_user_and_assistant_rows_searchable(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v13_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            hits = await store.search_history("flamingo")
            roles = sorted(h.role for h in hits)
            # Both pre-migration user and assistant rows are indexed …
            assert roles == ["assistant", "user"]
            # … and every hit belongs to the pre-migration conversation.
            assert all(h.conversation_id == 1 for h in hits)
        finally:
            await store.close()

    async def test_tool_role_content_not_searchable(self, tmp_path: Path) -> None:
        # `zebrahammer` appears only in the tool-role row (Decision #5:
        # tool/system rows are not indexed).
        db_path = tmp_path / "conversations.db"
        await _build_v13_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            assert await store.search_history("zebrahammer") == []
        finally:
            await store.close()

    async def test_message_appended_after_migration_is_searchable(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v13_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            await store.append_message(1, "user", "a fresh note about the quokka enclosure")
            hits = await store.search_history("quokka")
            assert len(hits) == 1
            assert hits[0].role == "user"
        finally:
            await store.close()

    async def test_tool_message_appended_after_migration_not_indexed(self, tmp_path: Path) -> None:
        # Trigger symmetry: post-migration tool rows must not enter the
        # index either (the insert trigger carries the same role filter).
        db_path = tmp_path / "conversations.db"
        await _build_v13_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            await store.append_message(1, "tool", "post-migration wombatgadget payload")
            assert await store.search_history("wombatgadget") == []
        finally:
            await store.close()


# --- Pre-existing rows survive the migration ------------------------------


class TestRowsPreserved:
    async def test_conversation_and_messages_intact(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v13_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            conv = await store.get_or_create_conversation("discord", "thread-1")
            assert conv.id == 1
            assert conv.name == "pre-migration"

            msgs = await store.recent_messages(1, limit=20)
            assert [m.role for m in msgs] == ["user", "assistant", "tool"]
            assert await store.message_count(1) == 3
        finally:
            await store.close()
