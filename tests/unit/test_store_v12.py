"""Store v=12 — learning loop Layer 3 (proposal accumulation, UoW-22 / TLC-20 / TLC-21a).

Tester-authored from the CONTRACT + INTERFACE only (implementer/tester
barrier): these tests exercise the new `proposals` table and the
`Store.record_proposal` / `list_proposals` writer plus the retrospective
evidence-read methods (`conversations_active_since`,
`user_messages_in_window`, `failed_runs_in_window`) purely through the
public API. The two behavior-sensitive invariants pinned here:

- **TLC-20 (Learning provenance):** every proposal carries machine-readable
  evidence pointers, and ONLY user-authored messages, live (non-removed)
  feedback rows, and recorded failures are legal evidence. Agent output
  (assistant/tool/system messages), retracted feedback, ok runs, and
  dangling ids are rejected BEFORE any write — a synthetic day where all
  signal is agent-generated yields zero proposals.
- **TLC-21a (Bounded, human-gated learning):** v12 is accumulate-only —
  `record_proposal` only ever yields `status='pending'` rows; nothing
  auto-applies.

Plus UoW-22 dedup/idempotency: `(kind, payload_hash)` over open proposals
reinforces + unions evidence rather than duplicating.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from ember.errors import StoreError
from ember.store.repo import SCHEMA_VERSION, Store

THUMBS_UP = "\U0001f44d"  # 👍
THUMBS_DOWN = "\U0001f44e"  # 👎


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path):
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


async def _conv(store: Store, key: str = "t") -> int:
    conv = await store.get_or_create_conversation("discord", key)
    return conv.id


async def _feedback_id(store: Store, message_id: int) -> int:
    """Newest feedback row id for a message (live or not)."""
    cur = await store._c.execute(
        "SELECT id FROM feedback WHERE message_id = ? ORDER BY id DESC LIMIT 1",
        (message_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    assert row is not None, "expected a feedback row"
    return row[0]


async def _failed_run(store: Store, *, when: datetime | None = None, skill: str = "retro") -> int:
    """Record a non-ok scheduled run; return its id."""
    started = when or datetime.now(UTC)
    rid = await store.record_run_started(
        skill=skill,
        function="fn",
        run_name="nightly",
        scheduled_for=started,
        started_at=started,
    )
    await store.record_run_finished(rid, finished_at=started, status="error")
    return rid


async def _ok_run(store: Store, *, when: datetime | None = None) -> int:
    started = when or datetime.now(UTC)
    rid = await store.record_run_started(
        skill="retro",
        function="fn",
        run_name="nightly",
        scheduled_for=started,
        started_at=started,
    )
    await store.record_run_finished(rid, finished_at=started, status="ok")
    return rid


def _evidence_ids(proposal, key: str) -> set[int]:
    return set(json.loads(proposal.evidence_json).get(key, []))


# --- v11 DB builder (for the v11 -> v12 migration test) -------------------
#
# Trimmed to schema_version + the tables the v11->v12 migration touches or
# references (proposals FKs eval_runs). We never reach into `_MIGRATIONS` —
# only observable behavior (version 12 present, data survives, writer works).
_V11_SCHEMA = """
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

CREATE TABLE feedback (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id     INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id          INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    emoji               TEXT NOT NULL,
    sentiment           INTEGER,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    removed_at          TEXT
);
CREATE UNIQUE INDEX idx_feedback_live_unique
    ON feedback (message_id, emoji) WHERE removed_at IS NULL;

CREATE TABLE eval_runs (
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

INSERT INTO schema_version (version) VALUES (4);
INSERT INTO schema_version (version) VALUES (5);
INSERT INTO schema_version (version) VALUES (6);
INSERT INTO schema_version (version) VALUES (7);
INSERT INTO schema_version (version) VALUES (8);
INSERT INTO schema_version (version) VALUES (9);
INSERT INTO schema_version (version) VALUES (10);
INSERT INTO schema_version (version) VALUES (11);
"""


async def _build_v11_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.executescript(_V11_SCHEMA)
        await conn.commit()
    finally:
        await conn.close()


# --- Fresh schema at v=12 -------------------------------------------------


class TestFreshV12Schema:
    async def test_schema_version_constant_is_at_least_12(self) -> None:
        assert SCHEMA_VERSION >= 12, (
            f"SCHEMA_VERSION must bump to >=12 for v12; got {SCHEMA_VERSION}"
        )

    async def test_proposals_table_exists(self, store: Store) -> None:
        cur = await store._c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='proposals'"
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None


# --- TLC-20 accept path ---------------------------------------------------


class TestRecordProposalAcceptPath:
    async def test_user_message_evidence_inserts_pending(self, store: Store) -> None:
        cid = await _conv(store)
        uid = await store.append_message(cid, "user", "please stop doing X")
        pid, reinforced = await store.record_proposal(
            kind="lesson",
            summary="Avoid X",
            payload={"text": "avoid X"},
            evidence={"messages": [uid]},
        )
        assert reinforced is False
        rows = await store.list_proposals()
        assert len(rows) == 1
        assert rows[0].id == pid
        assert rows[0].status == "pending"
        assert _evidence_ids(rows[0], "messages") == {uid}

    async def test_live_feedback_evidence_inserts(self, store: Store) -> None:
        cid = await _conv(store)
        # Feedback lives on an agent message (a reaction on the bot's reply),
        # but the feedback ROW itself is human-authored signal → valid.
        mid = await store.append_message(cid, "assistant", "reply")
        await store.record_feedback(mid, THUMBS_DOWN)
        fid = await _feedback_id(store, mid)
        pid, reinforced = await store.record_proposal(
            kind="finding",
            summary="Users disliked reply",
            payload={"n": 1},
            evidence={"feedback": [fid]},
        )
        assert reinforced is False
        rows = await store.list_proposals()
        assert len(rows) == 1
        assert _evidence_ids(rows[0], "feedback") == {fid}

    async def test_failed_run_evidence_inserts(self, store: Store) -> None:
        rid = await _failed_run(store)
        pid, reinforced = await store.record_proposal(
            kind="finding",
            summary="Nightly failing",
            payload={"run": "nightly"},
            evidence={"runs": [rid]},
        )
        assert reinforced is False
        rows = await store.list_proposals()
        assert len(rows) == 1
        assert _evidence_ids(rows[0], "runs") == {rid}

    async def test_evidence_json_round_trips_all_kinds(self, store: Store) -> None:
        cid = await _conv(store)
        uid = await store.append_message(cid, "user", "hi")
        mid = await store.append_message(cid, "assistant", "reply")
        await store.record_feedback(mid, THUMBS_UP)
        fid = await _feedback_id(store, mid)
        rid = await _failed_run(store)
        await store.record_proposal(
            kind="profile",
            summary="mixed evidence",
            payload={"k": "v"},
            evidence={"messages": [uid], "feedback": [fid], "runs": [rid]},
        )
        rows = await store.list_proposals()
        assert _evidence_ids(rows[0], "messages") == {uid}
        assert _evidence_ids(rows[0], "feedback") == {fid}
        assert _evidence_ids(rows[0], "runs") == {rid}


# --- TLC-20 reject path ---------------------------------------------------


class TestRecordProposalRejectPath:
    async def _assert_rejected(self, store: Store, evidence: dict) -> None:
        before = len(await store.list_proposals())
        with pytest.raises(StoreError):
            await store.record_proposal(
                kind="finding",
                summary="should never persist",
                payload={"x": 1},
                evidence=evidence,
            )
        after = await store.list_proposals()
        assert len(after) == before, "rejected proposal must write nothing"

    async def test_assistant_message_rejected(self, store: Store) -> None:
        cid = await _conv(store)
        mid = await store.append_message(cid, "assistant", "agent text")
        await self._assert_rejected(store, {"messages": [mid]})

    async def test_tool_message_rejected(self, store: Store) -> None:
        cid = await _conv(store)
        mid = await store.append_message(cid, "tool", "tool output")
        await self._assert_rejected(store, {"messages": [mid]})

    async def test_system_message_rejected(self, store: Store) -> None:
        cid = await _conv(store)
        mid = await store.append_message(cid, "system", "system note")
        await self._assert_rejected(store, {"messages": [mid]})

    async def test_retracted_feedback_rejected(self, store: Store) -> None:
        cid = await _conv(store)
        mid = await store.append_message(cid, "assistant", "reply")
        await store.record_feedback(mid, THUMBS_DOWN)
        fid = await _feedback_id(store, mid)
        await store.remove_feedback(mid, THUMBS_DOWN)  # soft-delete → not live
        await self._assert_rejected(store, {"feedback": [fid]})

    async def test_ok_run_rejected(self, store: Store) -> None:
        rid = await _ok_run(store)
        await self._assert_rejected(store, {"runs": [rid]})

    async def test_nonexistent_message_rejected(self, store: Store) -> None:
        await self._assert_rejected(store, {"messages": [999999]})

    async def test_nonexistent_feedback_rejected(self, store: Store) -> None:
        await self._assert_rejected(store, {"feedback": [999999]})

    async def test_nonexistent_run_rejected(self, store: Store) -> None:
        await self._assert_rejected(store, {"runs": [999999]})

    async def test_empty_evidence_rejected(self, store: Store) -> None:
        # No provenance at all → rejected (TLC-20).
        await self._assert_rejected(store, {})


# --- TLC-20 headline: all signal agent-generated → zero proposals ---------


class TestAllSignalAgentGenerated:
    async def test_agent_only_day_yields_no_proposals(self, store: Store) -> None:
        cid = await _conv(store)
        a_mid = await store.append_message(cid, "assistant", "assistant text")
        t_mid = await store.append_message(cid, "tool", "tool text")
        s_mid = await store.append_message(cid, "system", "system text")
        # A 👎 that was then retracted — dead signal.
        await store.record_feedback(a_mid, THUMBS_DOWN)
        dead_fid = await _feedback_id(store, a_mid)
        await store.remove_feedback(a_mid, THUMBS_DOWN)

        for evidence in (
            {"messages": [a_mid]},
            {"messages": [t_mid]},
            {"messages": [s_mid]},
            {"messages": [a_mid, t_mid, s_mid]},
            {"feedback": [dead_fid]},
        ):
            with pytest.raises(StoreError):
                await store.record_proposal(
                    kind="finding",
                    summary="derived from agent noise",
                    payload={"e": evidence},
                    evidence=evidence,
                )

        assert await store.list_proposals() == []


# --- TLC-21a: accumulate-only, always pending -----------------------------


class TestBoundedLearning:
    async def test_recorded_proposal_is_pending(self, store: Store) -> None:
        cid = await _conv(store)
        uid = await store.append_message(cid, "user", "signal")
        pid, _ = await store.record_proposal(
            kind="trigger",
            summary="add trigger",
            payload={"t": 1},
            evidence={"messages": [uid]},
        )
        rows = await store.list_proposals()
        assert rows[0].status == "pending"
        # No status filter other than pending ever yields rows in v12.
        assert await store.list_proposals(status="approved") == []
        assert await store.list_proposals(status="applied") == []
        assert await store.list_proposals(status="rejected") == []
        assert len(await store.list_proposals(status="pending")) == 1

    async def test_every_write_is_pending(self, store: Store) -> None:
        cid = await _conv(store)
        for i, kind in enumerate(("profile", "skill", "trigger", "lesson", "finding")):
            uid = await store.append_message(cid, "user", f"m{i}")
            await store.record_proposal(
                kind=kind,
                summary=f"s{i}",
                payload={"i": i},
                evidence={"messages": [uid]},
            )
        assert all(p.status == "pending" for p in await store.list_proposals())


# --- UoW-22 dedup / idempotency -------------------------------------------


class TestDedup:
    async def test_same_kind_payload_reinforces_and_unions(self, store: Store) -> None:
        cid = await _conv(store)
        u1 = await store.append_message(cid, "user", "one")
        u2 = await store.append_message(cid, "user", "two")
        payload = {"finding": "same"}

        pid1, r1 = await store.record_proposal(
            kind="finding", summary="F", payload=payload, evidence={"messages": [u1]}
        )
        assert r1 is False

        pid2, r2 = await store.record_proposal(
            kind="finding", summary="F", payload=payload, evidence={"messages": [u2]}
        )
        assert r2 is True
        assert pid2 == pid1

        rows = await store.list_proposals()
        assert len(rows) == 1, "dedup must not insert a duplicate open row"
        assert rows[0].reinforced_count == 1
        assert _evidence_ids(rows[0], "messages") == {u1, u2}

    async def test_different_kind_same_payload_is_distinct(self, store: Store) -> None:
        cid = await _conv(store)
        uid = await store.append_message(cid, "user", "sig")
        payload = {"x": 1}
        pid1, _ = await store.record_proposal(
            kind="finding", summary="a", payload=payload, evidence={"messages": [uid]}
        )
        pid2, reinforced = await store.record_proposal(
            kind="lesson", summary="b", payload=payload, evidence={"messages": [uid]}
        )
        assert reinforced is False
        assert pid2 != pid1
        assert len(await store.list_proposals()) == 2

    async def test_different_payload_same_kind_is_distinct(self, store: Store) -> None:
        cid = await _conv(store)
        uid = await store.append_message(cid, "user", "sig")
        pid1, _ = await store.record_proposal(
            kind="finding", summary="a", payload={"x": 1}, evidence={"messages": [uid]}
        )
        pid2, reinforced = await store.record_proposal(
            kind="finding", summary="b", payload={"x": 2}, evidence={"messages": [uid]}
        )
        assert reinforced is False
        assert pid2 != pid1
        assert len(await store.list_proposals()) == 2

    async def test_reinforce_keeps_status_pending(self, store: Store) -> None:
        cid = await _conv(store)
        u1 = await store.append_message(cid, "user", "one")
        u2 = await store.append_message(cid, "user", "two")
        await store.record_proposal(
            kind="finding", summary="F", payload={"p": 1}, evidence={"messages": [u1]}
        )
        await store.record_proposal(
            kind="finding", summary="F", payload={"p": 1}, evidence={"messages": [u2]}
        )
        rows = await store.list_proposals()
        assert rows[0].status == "pending"


# --- Read methods (retrospective evidence surfaces) -----------------------


class TestConversationsActiveSince:
    async def test_filters_by_last_active_and_orders_id_asc(self, store: Store) -> None:
        c1 = await store.get_or_create_conversation("discord", "a")
        c2 = await store.get_or_create_conversation("discord", "b")
        # Force one conversation's last_active into the deep past.
        await store._c.execute(
            "UPDATE conversations SET last_active = ? WHERE id = ?",
            ("2000-01-01T00:00:00.000Z", c1.id),
        )
        await store._c.commit()

        since = datetime.now(UTC) - timedelta(hours=1)
        active = await store.conversations_active_since(since)
        ids = [c.id for c in active]
        assert c1.id not in ids  # stale one excluded
        assert c2.id in ids

    async def test_id_ascending(self, store: Store) -> None:
        ids = [(await store.get_or_create_conversation("discord", k)).id for k in ("a", "b", "c")]
        since = datetime.now(UTC) - timedelta(hours=1)
        got = [c.id for c in await store.conversations_active_since(since)]
        assert got == sorted(got)
        assert set(ids) <= set(got)


class TestUserMessagesInWindow:
    async def test_only_user_rows_in_window(self, store: Store) -> None:
        cid = await _conv(store)
        u = await store.append_message(cid, "user", "user says")
        await store.append_message(cid, "assistant", "assistant says")
        await store.append_message(cid, "tool", "tool says")
        now = datetime.now(UTC)
        rows = await store.user_messages_in_window(
            cid, now - timedelta(hours=1), now + timedelta(hours=1)
        )
        ids = [m.id for m in rows]
        assert ids == [u]
        assert all(m.role == "user" for m in rows)

    async def test_excludes_outside_window(self, store: Store) -> None:
        cid = await _conv(store)
        await store.append_message(cid, "user", "recent")
        past = datetime.now(UTC) - timedelta(days=2)
        rows = await store.user_messages_in_window(cid, past - timedelta(hours=1), past)
        assert rows == []

    async def test_oldest_first(self, store: Store) -> None:
        cid = await _conv(store)
        a = await store.append_message(cid, "user", "first")
        b = await store.append_message(cid, "user", "second")
        now = datetime.now(UTC)
        rows = await store.user_messages_in_window(
            cid, now - timedelta(hours=1), now + timedelta(hours=1)
        )
        assert [m.id for m in rows] == [a, b]


class TestFailedRunsInWindow:
    async def test_includes_failed_excludes_ok(self, store: Store) -> None:
        failed = await _failed_run(store)
        await _ok_run(store)
        now = datetime.now(UTC)
        rows = await store.failed_runs_in_window(now - timedelta(hours=1), now + timedelta(hours=1))
        ids = {r.id for r in rows}
        assert failed in ids
        assert all(r.status != "ok" for r in rows)

    async def test_respects_window(self, store: Store) -> None:
        await _failed_run(store)
        past = datetime.now(UTC) - timedelta(days=2)
        rows = await store.failed_runs_in_window(past - timedelta(hours=1), past)
        assert rows == []

    async def test_timeout_and_missed_included(self, store: Store) -> None:
        now = datetime.now(UTC)
        rid = await store.record_run_started(
            skill="retro",
            function="fn",
            run_name="nightly",
            scheduled_for=now,
            started_at=now,
        )
        await store.record_run_finished(rid, finished_at=now, status="timeout")
        rows = await store.failed_runs_in_window(now - timedelta(hours=1), now + timedelta(hours=1))
        assert rid in {r.id for r in rows}


# --- Migration v11 -> v12 -------------------------------------------------


class TestMigrationV11ToV12:
    async def test_open_v11_db_runs_migration(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v11_db(db_path)
        store = Store(db_path)
        await store.open()  # must not raise
        await store.close()

    async def test_post_migration_records_v12(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v11_db(db_path)
        store = Store(db_path)
        await store.open()
        try:
            cur = await store._c.execute("SELECT version FROM schema_version")
            versions = {r[0] for r in await cur.fetchall()}
            await cur.close()
            assert 11 in versions
            assert 12 in versions
        finally:
            await store.close()

    async def test_migration_preserves_data_and_writer_works(self, tmp_path: Path) -> None:
        db_path = tmp_path / "conversations.db"
        await _build_v11_db(db_path)
        # Seed a conversation + user message into the v11 DB.
        conn = await aiosqlite.connect(db_path)
        try:
            await conn.execute(
                "INSERT INTO conversations (id, channel, channel_conversation_key) "
                "VALUES (1, 'discord', 'preexisting')"
            )
            await conn.execute(
                "INSERT INTO messages (id, conversation_id, role, content) "
                "VALUES (5, 1, 'user', 'old user message')"
            )
            await conn.commit()
        finally:
            await conn.close()

        store = Store(db_path)
        await store.open()
        try:
            # Pre-existing row survived.
            cur = await store._c.execute("SELECT content FROM messages WHERE id = 5")
            assert (await cur.fetchone())[0] == "old user message"
            await cur.close()
            # Proposals writer works against the migrated DB, citing the
            # surviving user message as evidence.
            pid, reinforced = await store.record_proposal(
                kind="lesson",
                summary="from migrated db",
                payload={"k": "v"},
                evidence={"messages": [5]},
            )
            assert reinforced is False
            rows = await store.list_proposals()
            assert len(rows) == 1
            assert rows[0].id == pid
            assert rows[0].status == "pending"
        finally:
            await store.close()
