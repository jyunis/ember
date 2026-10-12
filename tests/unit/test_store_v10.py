"""Store v=10 — learning loop Layer 1 (feedback capture, UoW-20 / FC-20).

Exercises the new schema/repo additions:

- `feedback` table — explicit reaction signal on agent messages, with a
  `removed_at` soft-delete marker (TLC-8) and one live row per
  `(message_id, emoji)`.
- `channel_message_map` — native channel message id → `messages.id`, so a
  reaction resolves to what it's reacting to (the UoW-20 resolution path).
- `Store.record_feedback` / `remove_feedback` / `feedback_in_window` /
  `feedback_counts` and `map_channel_messages` /
  `message_id_for_channel_message`.
- Forward migration v=9 → v=10 (FC-7).

These are the mechanical store surfaces (feedback upserts, the map): the
design's test posture allows single-author here (the implementer/tester
barrier is reserved for TLC-19/20/21a/FC-23, which land in v12+).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from ember.errors import StoreError
from ember.store.repo import SCHEMA_VERSION, Store

THUMBS_UP = "\U0001f44d"  # 👍
THUMBS_DOWN = "\U0001f44e"  # 👎
THINKING = "\U0001f914"  # 🤔


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path):
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


async def _msg(store: Store, key: str = "t") -> tuple[int, int]:
    """Create a conversation + one assistant message; return
    `(conversation_id, message_id)`."""
    conv = await store.get_or_create_conversation("discord", key)
    mid = await store.append_message(conv.id, "assistant", "hello")
    return conv.id, mid


# v=9 schema, trimmed to what the v=9 → v=10 migration depends on
# (conversations + messages + schema_version). The migration only CREATEs
# the new `feedback` / `channel_message_map` tables, so unrelated v=9
# tables aren't needed to observe migration behavior. We never reach into
# `_MIGRATIONS` — only observable behavior is asserted.
_V9_SCHEMA = """
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
    pending_schwab_reauth           TEXT,
    UNIQUE (channel, channel_conversation_key)
);

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

INSERT INTO schema_version (version) VALUES (4);
INSERT INTO schema_version (version) VALUES (5);
INSERT INTO schema_version (version) VALUES (6);
INSERT INTO schema_version (version) VALUES (7);
INSERT INTO schema_version (version) VALUES (8);
INSERT INTO schema_version (version) VALUES (9);
"""


async def _build_v9_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.executescript(_V9_SCHEMA)
        await conn.commit()
    finally:
        await conn.close()


# --- Fresh-init at v=10 ---------------------------------------------------


class TestFreshV10Schema:
    async def test_schema_version_constant_is_at_least_10(self) -> None:
        assert SCHEMA_VERSION >= 10, (
            f"SCHEMA_VERSION must bump to >=10 for v10; got {SCHEMA_VERSION}"
        )

    async def test_feedback_and_map_tables_exist(self, store: Store) -> None:
        cur = await store._c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('feedback', 'channel_message_map')"
        )
        rows = await cur.fetchall()
        await cur.close()
        names = {r[0] for r in rows}
        assert names == {"feedback", "channel_message_map"}


# --- channel_message_map --------------------------------------------------


class TestChannelMessageMap:
    async def test_map_and_resolve_round_trip(self, store: Store) -> None:
        _, mid = await _msg(store)
        await store.map_channel_messages("discord", ["1001"], mid)
        assert await store.message_id_for_channel_message("discord", "1001") == mid

    async def test_unmapped_resolves_to_none(self, store: Store) -> None:
        # A reaction on a message we never authored (user msg, pre-bot
        # history) has no map row → None → ignored (UoW-20).
        assert await store.message_id_for_channel_message("discord", "999") is None

    async def test_chunked_reply_maps_many_native_ids_to_one_row(self, store: Store) -> None:
        _, mid = await _msg(store)
        await store.map_channel_messages("discord", ["1", "2", "3"], mid)
        for cmid in ("1", "2", "3"):
            assert await store.message_id_for_channel_message("discord", cmid) == mid

    async def test_map_is_idempotent(self, store: Store) -> None:
        _, mid = await _msg(store)
        await store.map_channel_messages("discord", ["1001"], mid)
        # Replayed send: INSERT OR IGNORE must not raise on the PK collision.
        await store.map_channel_messages("discord", ["1001"], mid)
        assert await store.message_id_for_channel_message("discord", "1001") == mid

    async def test_map_empty_list_is_noop(self, store: Store) -> None:
        _, mid = await _msg(store)
        await store.map_channel_messages("discord", [], mid)  # must not raise

    async def test_channel_scoped(self, store: Store) -> None:
        _, mid = await _msg(store)
        await store.map_channel_messages("discord", ["1001"], mid)
        assert await store.message_id_for_channel_message("telegram", "1001") is None


# --- record / remove feedback --------------------------------------------


async def _live_rows(store: Store, message_id: int) -> list[aiosqlite.Row]:
    cur = await store._c.execute(
        "SELECT emoji, sentiment, removed_at FROM feedback "
        "WHERE message_id = ? AND removed_at IS NULL",
        (message_id,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return rows


class TestRecordFeedback:
    async def test_thumbs_up_maps_to_plus_one(self, store: Store) -> None:
        _, mid = await _msg(store)
        await store.record_feedback(mid, THUMBS_UP)
        rows = await _live_rows(store, mid)
        assert len(rows) == 1
        assert rows[0]["emoji"] == THUMBS_UP
        assert rows[0]["sentiment"] == 1

    async def test_thumbs_down_maps_to_minus_one(self, store: Store) -> None:
        _, mid = await _msg(store)
        await store.record_feedback(mid, THUMBS_DOWN)
        rows = await _live_rows(store, mid)
        assert rows[0]["sentiment"] == -1

    async def test_other_emoji_recorded_verbatim_with_null_sentiment(self, store: Store) -> None:
        _, mid = await _msg(store)
        await store.record_feedback(mid, THINKING)
        rows = await _live_rows(store, mid)
        assert rows[0]["emoji"] == THINKING
        assert rows[0]["sentiment"] is None

    async def test_variation_selector_thumbs_still_maps(self, store: Store) -> None:
        # Discord may append U+FE0F (emoji presentation selector).
        _, mid = await _msg(store)
        await store.record_feedback(mid, THUMBS_UP + "️")
        rows = await _live_rows(store, mid)
        assert rows[0]["sentiment"] == 1

    async def test_conversation_id_derived_from_message(self, store: Store) -> None:
        cid, mid = await _msg(store)
        await store.record_feedback(mid, THUMBS_UP)
        cur = await store._c.execute(
            "SELECT conversation_id FROM feedback WHERE message_id = ?", (mid,)
        )
        row = await cur.fetchone()
        await cur.close()
        assert row[0] == cid

    async def test_duplicate_add_is_noop(self, store: Store) -> None:
        _, mid = await _msg(store)
        await store.record_feedback(mid, THUMBS_UP)
        await store.record_feedback(mid, THUMBS_UP)
        rows = await _live_rows(store, mid)
        assert len(rows) == 1  # exactly one live row per (message, emoji)

    async def test_distinct_emoji_coexist(self, store: Store) -> None:
        _, mid = await _msg(store)
        await store.record_feedback(mid, THUMBS_UP)
        await store.record_feedback(mid, THINKING)
        assert len(await _live_rows(store, mid)) == 2

    async def test_record_on_missing_message_raises(self, store: Store) -> None:
        with pytest.raises(StoreError):
            await store.record_feedback(424242, THUMBS_UP)


class TestRemoveFeedback:
    async def test_remove_soft_deletes(self, store: Store) -> None:
        _, mid = await _msg(store)
        await store.record_feedback(mid, THUMBS_DOWN)
        await store.remove_feedback(mid, THUMBS_DOWN)
        assert len(await _live_rows(store, mid)) == 0
        # Audit row survives with removed_at set (TLC-8).
        cur = await store._c.execute("SELECT removed_at FROM feedback WHERE message_id = ?", (mid,))
        row = await cur.fetchone()
        await cur.close()
        assert row[0] is not None

    async def test_remove_nonexistent_is_noop(self, store: Store) -> None:
        _, mid = await _msg(store)
        await store.remove_feedback(mid, THUMBS_UP)  # nothing live; must not raise
        assert len(await _live_rows(store, mid)) == 0

    async def test_readd_after_remove_creates_new_live_row(self, store: Store) -> None:
        # remove then re-add must produce a fresh live row (the partial
        # unique index exempts removed rows, so this doesn't collide).
        _, mid = await _msg(store)
        await store.record_feedback(mid, THUMBS_UP)
        await store.remove_feedback(mid, THUMBS_UP)
        await store.record_feedback(mid, THUMBS_UP)
        assert len(await _live_rows(store, mid)) == 1
        cur = await store._c.execute("SELECT COUNT(*) FROM feedback WHERE message_id = ?", (mid,))
        total = (await cur.fetchone())[0]
        await cur.close()
        assert total == 2  # one removed (audit) + one live


class TestFeedbackCounts:
    async def test_counts_split_by_sentiment(self, store: Store) -> None:
        cid, m1 = await _msg(store, "a")
        m2 = await store.append_message(cid, "assistant", "two")
        m3 = await store.append_message(cid, "assistant", "three")
        await store.record_feedback(m1, THUMBS_UP)
        await store.record_feedback(m2, THUMBS_DOWN)
        await store.record_feedback(m3, THINKING)
        assert await store.feedback_counts(cid) == (1, 1, 1)

    async def test_removed_excluded_from_counts(self, store: Store) -> None:
        cid, mid = await _msg(store)
        await store.record_feedback(mid, THUMBS_UP)
        await store.remove_feedback(mid, THUMBS_UP)
        assert await store.feedback_counts(cid) == (0, 0, 0)

    async def test_counts_scoped_per_conversation(self, store: Store) -> None:
        c1, m1 = await _msg(store, "a")
        _, m2 = await _msg(store, "b")
        await store.record_feedback(m1, THUMBS_UP)
        await store.record_feedback(m2, THUMBS_UP)
        assert await store.feedback_counts(c1) == (1, 0, 0)


class TestFeedbackInWindow:
    async def test_window_filters_by_created_at(self, store: Store) -> None:
        _, mid = await _msg(store)
        await store.record_feedback(mid, THUMBS_UP)
        now = datetime.now(UTC)
        rows = await store.feedback_in_window(now - timedelta(hours=1), now + timedelta(hours=1))
        assert len(rows) == 1
        assert rows[0].message_id == mid
        assert rows[0].sentiment == 1

    async def test_window_excludes_outside_range(self, store: Store) -> None:
        _, mid = await _msg(store)
        await store.record_feedback(mid, THUMBS_UP)
        # A window entirely in the past excludes the just-created row.
        past = datetime.now(UTC) - timedelta(days=2)
        rows = await store.feedback_in_window(past - timedelta(hours=1), past)
        assert rows == []

    async def test_window_excludes_removed(self, store: Store) -> None:
        _, mid = await _msg(store)
        await store.record_feedback(mid, THUMBS_UP)
        await store.remove_feedback(mid, THUMBS_UP)
        now = datetime.now(UTC)
        rows = await store.feedback_in_window(now - timedelta(hours=1), now + timedelta(hours=1))
        assert rows == []


# --- Migration v=9 → v=10 -------------------------------------------------


class TestMigrationV9ToV10:
    async def test_open_v9_db_runs_migration(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v9_db(db_path)
        store = Store(db_path)
        await store.open()  # must not raise
        await store.close()

    async def test_post_migration_records_v10(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v9_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("SELECT version FROM schema_version")
            versions = {r[0] for r in await cur.fetchall()}
            await cur.close()
            assert 9 in versions
            assert 10 in versions
        finally:
            await store.close()

    async def test_migration_preserves_existing_data_and_feedback_works(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v9_db(db_path)
        # Seed a conversation + message directly into the v=9 DB.
        conn = await aiosqlite.connect(db_path)
        try:
            await conn.execute(
                "INSERT INTO conversations (id, channel, channel_conversation_key) "
                "VALUES (1, 'discord', 'preexisting')"
            )
            await conn.execute(
                "INSERT INTO messages (id, conversation_id, role, content) "
                "VALUES (5, 1, 'assistant', 'old reply')"
            )
            await conn.commit()
        finally:
            await conn.close()

        store = Store(db_path)
        await store.open()
        try:
            # Pre-existing row survived.
            cur = await store._c.execute("SELECT content FROM messages WHERE id = 5")
            assert (await cur.fetchone())[0] == "old reply"
            await cur.close()
            # New feedback path works against the migrated DB.
            await store.map_channel_messages("discord", ["7"], 5)
            assert await store.message_id_for_channel_message("discord", "7") == 5
            await store.record_feedback(5, THUMBS_UP)
            assert await store.feedback_counts(1) == (1, 0, 0)
        finally:
            await store.close()
