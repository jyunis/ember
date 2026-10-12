"""Conversation store.

Single Store instance per process (TLC-11). All mutations go through
repository methods on Store; no raw SQL elsewhere (FC-7).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import aiosqlite

from ember.errors import ProposalStateError, SchemaVersionError, StoreError
from ember.providers.base import Message, Role, ToolCall, Usage
from ember.scheduler.base import RunStatus, ScheduledRunRow, ToolErrorRow
from ember.store.types import (
    ClearResult,
    EvalCaseResult,
    EvalRun,
    Feedback,
    HistoryHit,
    HistoryWindow,
    HistoryWindowMessage,
    Lesson,
    Pin,
    Proposal,
    Summary,
)

# Re-export the v=4 row dataclasses so callers that import from
# ember.store.repo continue to work — `types.py` is the canonical
# definition site.
__all__ = [
    "SCHEMA_VERSION",
    "Store",
    "Conversation",
    "StoredMessage",
    "Summary",
    "Pin",
    "ClearResult",
    "Feedback",
    "EvalRun",
    "EvalCaseResult",
    "Proposal",
    "HistoryHit",
    "HistoryWindow",
    "HistoryWindowMessage",
]

SCHEMA_VERSION = 15
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# v=14 (UoW-27): per-message content cap in `history_window` results, and
# the marker appended when a row is cut at it.
_HISTORY_CONTENT_MAX_CHARS = 4000
_HISTORY_TRUNCATION_MARKER = "… [truncated]"

# Forward migrations from each version to the next. Keyed by `from_version`.
_MIGRATIONS: dict[int, list[str]] = {
    1: [
        "ALTER TABLE messages ADD COLUMN failover INTEGER NOT NULL DEFAULT 0",
        "INSERT INTO schema_version (version) VALUES (2)",
    ],
    2: [
        """
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
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_scheduled_runs_skill_started "
        "ON scheduled_runs (skill, started_at DESC)",
        """
        CREATE TABLE IF NOT EXISTS scheduled_jobs_state (
            skill       TEXT NOT NULL,
            run_name    TEXT NOT NULL,
            enabled     INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (skill, run_name)
        )
        """,
        "INSERT INTO schema_version (version) VALUES (3)",
    ],
    3: [
        # v=4: compaction, pins, profile proposal, clear, runtime config.
        "ALTER TABLE messages ADD COLUMN compacted INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE conversations ADD COLUMN pending_profile_proposal TEXT",
        """
        CREATE TABLE IF NOT EXISTS summaries (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id     INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            start_message_id    INTEGER NOT NULL,
            end_message_id      INTEGER NOT NULL,
            summary_text        TEXT NOT NULL,
            created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_summaries_conversation_start "
        "ON summaries (conversation_id, start_message_id)",
        """
        CREATE TABLE IF NOT EXISTS pinned (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id     INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            content             TEXT NOT NULL,
            created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_pinned_conversation ON pinned (conversation_id, id)",
        """
        CREATE TABLE IF NOT EXISTS runtime_config (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        "INSERT INTO schema_version (version) VALUES (4)",
    ],
    4: [
        # v=5: pending tool confirmation (TLC-3 enforcement).
        "ALTER TABLE conversations ADD COLUMN pending_tool_confirmation TEXT",
        "INSERT INTO schema_version (version) VALUES (5)",
    ],
    5: [
        # v=6: FC-17 cost attribution. Sub-agent message rows carry the
        # skill name + parent's message id so the parent conversation
        # owns the usage rows produced inside the sub-agent (TLC-16 §6).
        "ALTER TABLE messages ADD COLUMN subagent_skill TEXT",
        "ALTER TABLE messages ADD COLUMN parent_message_id INTEGER",
        "INSERT INTO schema_version (version) VALUES (6)",
    ],
    6: [
        # v=7: mcp_connector — outbound MCPs mounted at runtime. Boot
        # replays this table so connections survive restarts.
        """
        CREATE TABLE IF NOT EXISTS connected_mcps (
            name         TEXT PRIMARY KEY,
            transport    TEXT NOT NULL CHECK (transport IN ('streamableHttp')),
            url          TEXT NOT NULL,
            headers_json TEXT,
            created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        "INSERT INTO schema_version (version) VALUES (7)",
    ],
    7: [
        # v=8: widen connected_mcps.transport to also accept 'sse'
        # (legacy two-endpoint flow). SQLite can't ALTER a CHECK
        # constraint, so rebuild the table.
        "ALTER TABLE connected_mcps RENAME TO connected_mcps_v7",
        """
        CREATE TABLE connected_mcps (
            name         TEXT PRIMARY KEY,
            transport    TEXT NOT NULL CHECK (transport IN ('streamableHttp', 'sse')),
            url          TEXT NOT NULL,
            headers_json TEXT,
            created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        """
        INSERT INTO connected_mcps (name, transport, url, headers_json, created_at)
            SELECT name, transport, url, headers_json, created_at FROM connected_mcps_v7
        """,
        "DROP TABLE connected_mcps_v7",
        "INSERT INTO schema_version (version) VALUES (8)",
    ],
    8: [
        # v=9: pending_schwab_reauth (BR-FC-11). Two-message handshake for
        # Schwab OAuth refresh — first /schwab-reauth stashes
        # {state, callback_url, expires_at}; the redirect-URL pre-handler
        # in the agent loop reads it back and clears on completion.
        "ALTER TABLE conversations ADD COLUMN pending_schwab_reauth TEXT",
        "INSERT INTO schema_version (version) VALUES (9)",
    ],
    9: [
        # v=10: learning loop Layer 1 (UoW-20 / FC-20). `feedback` records
        # explicit reaction signal on agent messages; `channel_message_map`
        # resolves a native channel message id back to its `messages` row
        # so a reaction can find what it's reacting to.
        """
        CREATE TABLE IF NOT EXISTS feedback (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id     INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            message_id          INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            emoji               TEXT NOT NULL,
            sentiment           INTEGER,
            created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            removed_at          TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_feedback_message ON feedback (message_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_live_unique "
        "ON feedback (message_id, emoji) WHERE removed_at IS NULL",
        "CREATE INDEX IF NOT EXISTS idx_feedback_created "
        "ON feedback (created_at) WHERE removed_at IS NULL",
        """
        CREATE TABLE IF NOT EXISTS channel_message_map (
            channel             TEXT NOT NULL,
            channel_message_id  TEXT NOT NULL,
            message_id          INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            PRIMARY KEY (channel, channel_message_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_channel_message_map_message "
        "ON channel_message_map (message_id)",
        "INSERT INTO schema_version (version) VALUES (10)",
    ],
    10: [
        # v=11: learning loop Layer 2 (UoW-21 / FC-21). Eval result tables.
        # doc #36 labelled migration v11 as `proposals`, but migrations are
        # sequential and the eval harness (milestone v11) ships before the
        # retrospective (milestone v12); eval tables therefore take v11 and
        # `proposals` moves to v12 (doc #36 Decisions log).
        """
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
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_eval_runs_started ON eval_runs (started_at DESC)",
        """
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
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_eval_case_results_run "
        "ON eval_case_results (run_id, case_id, trial)",
        "INSERT INTO schema_version (version) VALUES (11)",
    ],
    11: [
        # v=12: learning loop Layer 3 (UoW-22 / FC-22 / FC-23). The nightly
        # retrospective accumulates `proposals` — durable improvements it
        # proposes but never applies (v12 is accumulate-only). Doc #36's
        # schema is extended by two operational columns its own UoW-22 dedup
        # clause requires: `payload_hash` (the (kind, hash) dedup key) and
        # `reinforced_count` (bumped when a re-run re-derives a finding
        # instead of duplicating it). See doc #36 Decisions log.
        """
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
        )
        """,
        # Dedup scope is open proposals only (UoW-22: "an open proposal with
        # matching kind+payload-hash is reinforced, not duplicated"). A
        # rejected/expired proposal must not block a fresh re-proposal, so the
        # uniqueness is partial on status='pending'.
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_pending_dedup "
        "ON proposals (kind, payload_hash) WHERE status = 'pending'",
        "CREATE INDEX IF NOT EXISTS idx_proposals_status_created ON proposals (status, created_at)",
        "INSERT INTO schema_version (version) VALUES (12)",
    ],
    12: [
        # v=13: learning loop Layer 4 (Closure, FC-26 / Decision #23). Lessons
        # become durable rows so their reinforcement/age is trackable;
        # lessons.md becomes a deterministic render of the live rows (removed_at
        # IS NULL, ordered by id ASC — TLC-12). `dedup_key` is the
        # normalized-text hash (Decision #24 / seam ①): the nightly reinforces a
        # live row whose key matches a re-derived lesson instead of proposing a
        # duplicate. Both schema.sql (fresh DBs) and this migration (live
        # upgrade) must define the table — schema.sql is NOT derived from
        # migrations.
        """
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
        )
        """,
        # At most one LIVE lesson per normalized-text key; a pruned row may share
        # a key with a later re-added one, so uniqueness is partial.
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_lessons_live_dedup "
        "ON lessons (dedup_key) WHERE removed_at IS NULL",
        "CREATE INDEX IF NOT EXISTS idx_lessons_live_reinforced "
        "ON lessons (last_reinforced_at) WHERE removed_at IS NULL",
        "INSERT INTO schema_version (version) VALUES (13)",
    ],
    13: [
        # v=14: conversation archival + history FTS (doc #54, UoW-26 /
        # UoW-27 / TLC-23). Channel-side thread deletion stamps
        # `archived_at` — rows are never deleted (TLC-23). `messages_fts`
        # is an external-content FTS5 index over user/assistant message
        # content (Decision #5: tool/system rows are noise and bloat),
        # kept in sync by triggers; insert/delete triggers carry the SAME
        # role filter (an asymmetric pair corrupts the index). content is
        # never UPDATEd today (compaction flips `compacted`); the update
        # trigger is defined anyway for safety, in the delete+insert form
        # external-content tables require, each half guarded on its own
        # row's role. Existing user/assistant rows are backfilled.
        "ALTER TABLE conversations ADD COLUMN archived_at TEXT",
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content,
            content='messages',
            content_rowid='id'
        )
        """,
        """
        CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages
        WHEN new.role IN ('user', 'assistant')
        BEGIN
            INSERT INTO messages_fts (rowid, content) VALUES (new.id, new.content);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages
        WHEN old.role IN ('user', 'assistant')
        BEGIN
            INSERT INTO messages_fts (messages_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages
        WHEN old.role IN ('user', 'assistant') OR new.role IN ('user', 'assistant')
        BEGIN
            INSERT INTO messages_fts (messages_fts, rowid, content)
                SELECT 'delete', old.id, old.content WHERE old.role IN ('user', 'assistant');
            INSERT INTO messages_fts (rowid, content)
                SELECT new.id, new.content WHERE new.role IN ('user', 'assistant');
        END
        """,
        "INSERT INTO messages_fts (rowid, content) "
        "SELECT id, content FROM messages WHERE role IN ('user', 'assistant')",
        "INSERT INTO schema_version (version) VALUES (14)",
    ],
    14: [
        # v=15: honest context accounting (doc #56, FC-28 / TLC-24).
        # `messages.cache_creation_tokens` completes the per-message usage
        # record (cache-write input was previously dropped on the floor);
        # `conversations.last_context_tokens` is the model's TRUE context
        # size from the latest completed claude_cli turn — the final
        # assistant event's input + cache_read + cache_creation sum. The
        # session-rotation trigger (UoW-28) reads this; no Ember mechanism
        # may use a cross-turn SUM of message tokens as a context-size
        # proxy (TLC-24).
        "ALTER TABLE messages ADD COLUMN cache_creation_tokens INTEGER",
        "ALTER TABLE conversations ADD COLUMN last_context_tokens INTEGER",
        "INSERT INTO schema_version (version) VALUES (15)",
    ],
}


# --- Row dataclasses ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Conversation:
    id: int
    channel: str
    channel_conversation_key: str
    name: str | None
    created_at: datetime
    last_active: datetime
    provider_route: str
    token_budget: int
    claude_cli_session_id: str | None
    auto_failover_on_rate_limit: bool
    active_skill: str | None
    pending_tool_confirmation: str | None = None
    pending_schwab_reauth: str | None = None
    archived_at: datetime | None = None
    # v=15 (FC-28 / TLC-24): true context size from the latest completed
    # claude_cli turn; None before the first measured turn and after a
    # session rotation clears it (UoW-28 step 6).
    last_context_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ChannelSession:
    """Session-resolution view of a channel's newest live conversation
    (SIRI-UoW-1). `key` is the `channel_conversation_key`; `has_pending`
    is True when any pending-state column is set — a pending
    confirmation/proposal must keep the session redeemable."""

    key: str
    last_active: datetime
    has_pending: bool


@dataclass(frozen=True, slots=True)
class StoredMessage:
    """Hydrated `messages` row. Use `to_message()` to drop store-only
    columns and get a provider-shaped Message."""

    id: int
    conversation_id: int
    role: Role
    content: str
    provider_route: str | None
    usage: Usage | None
    tool_calls: list[ToolCall] | None
    created_at: datetime
    failover: bool = False

    def to_message(self) -> Message:
        return Message(role=self.role, content=self.content, tool_calls=self.tool_calls)


# --- Store -----------------------------------------------------------------


class Store:
    """SQLite-backed conversation store.

    Holds the single open connection for the process. Methods are
    coroutine functions so the agent loop never blocks the event loop
    on disk I/O.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    # --- Lifecycle --------------------------------------------------------

    async def open(self) -> None:
        if self._conn is not None:
            raise StoreError("Store is already open")

        if str(self._db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = await aiosqlite.connect(self._db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")

        try:
            await self._verify_fts5_available(conn)
            await self._verify_or_apply_schema(conn)
        except BaseException:
            await conn.close()
            raise

        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @staticmethod
    async def _verify_fts5_available(conn: aiosqlite.Connection) -> None:
        """v=14 startup gate: raise `StoreError` if the linked SQLite has
        no FTS5 (TLC-7: no silent degradation — the schema and the
        history-search surface both depend on it). Cheap probe: create and
        drop a temp virtual table."""
        try:
            await conn.execute("CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)")
            await conn.execute("DROP TABLE temp._fts5_probe")
        except aiosqlite.OperationalError as e:
            raise StoreError(f"SQLite FTS5 is unavailable ({e}); refusing to start (TLC-7)") from e

    async def _verify_or_apply_schema(self, conn: aiosqlite.Connection) -> None:
        """If the DB is fresh, apply schema.sql. Otherwise verify the
        schema_version row matches SCHEMA_VERSION (FC-7)."""
        # Is this a fresh DB? (no tables at all)
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1")
        any_table = await cur.fetchone()
        await cur.close()

        if any_table is None:
            schema_sql = _SCHEMA_PATH.read_text()
            await conn.executescript(schema_sql)
            await conn.commit()
            return

        # Existing DB — schema_version must exist and match.
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        sv_table = await cur.fetchone()
        await cur.close()
        if sv_table is None:
            raise SchemaVersionError("DB exists but has no schema_version table; refusing to start")

        cur = await conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise SchemaVersionError("schema_version table is empty")
        version = row[0]
        if version == SCHEMA_VERSION:
            return
        # Forward-migrate if a path exists.
        while version < SCHEMA_VERSION and version in _MIGRATIONS:
            for stmt in _MIGRATIONS[version]:
                await conn.execute(stmt)
            await conn.commit()
            version += 1
        if version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"schema_version={version}, expected {SCHEMA_VERSION}; no migration path"
            )

    # --- Internal helpers -------------------------------------------------

    @property
    def _c(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise StoreError("Store is not open")
        return self._conn

    @staticmethod
    def _parse_dt(s: str) -> datetime:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    @staticmethod
    def _tool_calls_to_json(tcs: list[ToolCall] | None) -> str | None:
        if not tcs:
            return None
        return json.dumps([asdict(tc) for tc in tcs])

    @staticmethod
    def _tool_calls_from_json(s: str | None) -> list[ToolCall] | None:
        if not s:
            return None
        raw = json.loads(s)
        return [
            ToolCall(id=d["id"], name=d["name"], args=d["args"], status=d.get("status"))
            for d in raw
        ]

    @classmethod
    def _hydrate_conversation(cls, row: aiosqlite.Row) -> Conversation:
        keys = row.keys()
        ptc = row["pending_tool_confirmation"] if "pending_tool_confirmation" in keys else None
        psr = row["pending_schwab_reauth"] if "pending_schwab_reauth" in keys else None
        arch = row["archived_at"] if "archived_at" in keys else None
        lct = row["last_context_tokens"] if "last_context_tokens" in keys else None
        return Conversation(
            id=row["id"],
            channel=row["channel"],
            channel_conversation_key=row["channel_conversation_key"],
            name=row["name"],
            created_at=cls._parse_dt(row["created_at"]),
            last_active=cls._parse_dt(row["last_active"]),
            provider_route=row["provider_route"],
            token_budget=row["token_budget"],
            claude_cli_session_id=row["claude_cli_session_id"],
            auto_failover_on_rate_limit=bool(row["auto_failover_on_rate_limit"]),
            active_skill=row["active_skill"],
            pending_tool_confirmation=ptc,
            pending_schwab_reauth=psr,
            archived_at=cls._parse_dt(arch) if arch else None,
            last_context_tokens=int(lct) if lct is not None else None,
        )

    @classmethod
    def _hydrate_message(cls, row: aiosqlite.Row) -> StoredMessage:
        usage: Usage | None
        if row["tokens_in"] is None and row["tokens_out"] is None:
            usage = None
        else:
            cct = row["cache_creation_tokens"] if "cache_creation_tokens" in row.keys() else None
            usage = Usage(
                input_tokens=row["tokens_in"] or 0,
                output_tokens=row["tokens_out"] or 0,
                cached_tokens=row["cached_tokens"] or 0,
                cache_creation_tokens=cct or 0,
                cost_usd=row["cost_usd"],
                subscription=bool(row["subscription_turn"]),
            )
        return StoredMessage(
            id=row["id"],
            conversation_id=row["conversation_id"],
            role=cast(Role, row["role"]),
            content=row["content"],
            provider_route=row["provider_route"],
            usage=usage,
            tool_calls=cls._tool_calls_from_json(row["tool_calls_json"]),
            created_at=cls._parse_dt(row["created_at"]),
            failover=bool(row["failover"]) if "failover" in row.keys() else False,
        )

    # --- Conversations ----------------------------------------------------

    async def get_or_create_conversation(
        self,
        channel: str,
        channel_conversation_key: str,
        *,
        name: str | None = None,
    ) -> Conversation:
        """UoW-14: idempotent create-or-fetch by `(channel, key)`.

        v=14 (UoW-26 self-heal): resolving a stamped row clears
        `archived_at` — a message arriving proves the thread exists, so a
        false-positive archive stamp heals itself here.
        """
        existing = await self._fetch_conversation_by_key(channel, channel_conversation_key)
        if existing is not None:
            return await self._heal_archived_stamp(existing)

        try:
            # Explicitly pass provider_route rather than relying on the
            # schema DEFAULT — SQLite only applies a column DEFAULT on
            # initial CREATE TABLE, so schema.sql changes don't propagate
            # to existing DBs. (The DB created before the v9 schema-default
            # change still has DEFAULT 'claude_cli:sonnet-4-6', which is
            # an invalid claude CLI alias.) Hard-coding 'claude_cli' here
            # makes the default robust to schema drift.
            await self._c.execute(
                """
                INSERT INTO conversations (channel, channel_conversation_key, name, provider_route)
                VALUES (?, ?, ?, ?)
                """,
                (channel, channel_conversation_key, name, "claude_cli"),
            )
            await self._c.commit()
        except aiosqlite.IntegrityError:
            # Race: someone else inserted between our read and write.
            await self._c.rollback()

        again = await self._fetch_conversation_by_key(channel, channel_conversation_key)
        if again is None:
            raise StoreError("conversation insert succeeded but row not found")
        return await self._heal_archived_stamp(again)

    async def _heal_archived_stamp(self, conv: Conversation) -> Conversation:
        """UoW-26 self-heal: clear `archived_at` on a resolved stamped row.
        No-op for live rows."""
        if conv.archived_at is None:
            return conv
        await self.unarchive_conversation(conv.channel, conv.channel_conversation_key)
        return replace(conv, archived_at=None)

    async def _fetch_conversation_by_key(self, channel: str, key: str) -> Conversation | None:
        cur = await self._c.execute(
            """
            SELECT * FROM conversations
            WHERE channel = ? AND channel_conversation_key = ?
            """,
            (channel, key),
        )
        row = await cur.fetchone()
        await cur.close()
        return self._hydrate_conversation(row) if row else None

    async def get_conversation(self, conversation_id: int) -> Conversation | None:
        cur = await self._c.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        row = await cur.fetchone()
        await cur.close()
        return self._hydrate_conversation(row) if row else None

    async def update_claude_cli_session(
        self,
        conversation_id: int,
        session_id: str | None,
    ) -> None:
        await self._c.execute(
            "UPDATE conversations SET claude_cli_session_id = ? WHERE id = ?",
            (session_id, conversation_id),
        )
        await self._c.commit()

    async def update_last_context_tokens(
        self,
        conversation_id: int,
        tokens: int | None,
    ) -> None:
        """v=15 (FC-28 / TLC-24): persist (or clear, with None) the
        conversation's measured context size. Written by the agent after
        every claude_cli turn that carried `CompletionResult.context_tokens`;
        cleared by session rotation (UoW-28 step 6). Single-column update,
        `update_claude_cli_session` pattern."""
        await self._c.execute(
            "UPDATE conversations SET last_context_tokens = ? WHERE id = ?",
            (tokens, conversation_id),
        )
        await self._c.commit()

    async def touch_conversation(self, conversation_id: int) -> None:
        await self._c.execute(
            """
            UPDATE conversations
            SET last_active = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (conversation_id,),
        )
        await self._c.commit()

    async def newest_channel_session(self, channel: str) -> ChannelSession | None:
        """SIRI-UoW-1: the unarchived conversation on `channel` with the
        greatest `last_active` (ties broken by greatest `id`), or None.
        `has_pending` is True iff any pending-state column is non-NULL."""
        cur = await self._c.execute(
            """
            SELECT channel_conversation_key,
                   last_active,
                   (pending_profile_proposal IS NOT NULL
                    OR pending_tool_confirmation IS NOT NULL
                    OR pending_schwab_reauth IS NOT NULL) AS has_pending
            FROM conversations
            WHERE channel = ? AND archived_at IS NULL
            ORDER BY last_active DESC, id DESC
            LIMIT 1
            """,
            (channel,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return ChannelSession(
            key=row["channel_conversation_key"],
            last_active=self._parse_dt(row["last_active"]),
            has_pending=bool(row["has_pending"]),
        )

    # --- Messages ---------------------------------------------------------

    async def append_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        *,
        provider_route: str | None = None,
        usage: Usage | None = None,
        tool_calls: list[ToolCall] | None = None,
        failover: bool = False,
        subagent_skill: str | None = None,
        parent_message_id: int | None = None,
    ) -> int:
        tokens_in: int | None = usage.input_tokens if usage else None
        tokens_out: int | None = usage.output_tokens if usage else None
        cached_tokens: int | None = usage.cached_tokens if usage else None
        # v=15 (FC-28): persist the cache-write volume alongside reads.
        cache_creation_tokens: int | None = usage.cache_creation_tokens if usage else None
        cost_usd: float | None = usage.cost_usd if usage else None
        subscription_turn: int = 1 if (usage and usage.subscription) else 0
        tool_calls_json = self._tool_calls_to_json(tool_calls)
        failover_int = 1 if failover else 0

        params: tuple[Any, ...] = (
            conversation_id,
            role,
            content,
            provider_route,
            tokens_in,
            tokens_out,
            cached_tokens,
            cache_creation_tokens,
            cost_usd,
            subscription_turn,
            failover_int,
            tool_calls_json,
            subagent_skill,
            parent_message_id,
        )

        try:
            cur = await self._c.execute(
                """
                INSERT INTO messages (
                    conversation_id, role, content, provider_route,
                    tokens_in, tokens_out, cached_tokens, cache_creation_tokens,
                    cost_usd, subscription_turn, failover, tool_calls_json,
                    subagent_skill, parent_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params,
            )
            await self._c.commit()
        except aiosqlite.IntegrityError as e:
            await self._c.rollback()
            raise StoreError(f"append_message integrity: {e}") from e

        new_id = cur.lastrowid
        await cur.close()
        if new_id is None:
            raise StoreError("INSERT did not return lastrowid")
        return new_id

    async def update_provider_route(
        self,
        conversation_id: int,
        route: str,
    ) -> None:
        """Persist a new `<provider>:<model>` route on the conversation."""
        await self._c.execute(
            "UPDATE conversations SET provider_route = ? WHERE id = ?",
            (route, conversation_id),
        )
        await self._c.commit()

    async def update_failover_flag(
        self,
        conversation_id: int,
        on: bool,
    ) -> None:
        """Toggle UoW-15 auto-failover on this conversation."""
        await self._c.execute(
            "UPDATE conversations SET auto_failover_on_rate_limit = ? WHERE id = ?",
            (1 if on else 0, conversation_id),
        )
        await self._c.commit()

    async def set_active_skill(
        self,
        conversation_id: int,
        skill_name: str | None,
    ) -> None:
        """Set or clear the active skill on a conversation (FC-5, v7)."""
        await self._c.execute(
            "UPDATE conversations SET active_skill = ? WHERE id = ?",
            (skill_name, conversation_id),
        )
        await self._c.commit()

    async def recent_messages(
        self,
        conversation_id: int,
        *,
        limit: int = 50,
        include_compacted: bool = False,
    ) -> list[StoredMessage]:
        """Most recent `limit` messages for the conversation, returned
        oldest-first for prompt assembly.

        v=4: by default filters out messages with `compacted=1` (UoW-12);
        callers needing the full record (e.g. `/context`, debug paths)
        opt back in via `include_compacted=True`.
        """
        if include_compacted:
            sql = """
                SELECT * FROM (
                    SELECT * FROM messages
                    WHERE conversation_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                )
                ORDER BY created_at ASC, id ASC
            """
        else:
            sql = """
                SELECT * FROM (
                    SELECT * FROM messages
                    WHERE conversation_id = ? AND compacted = 0
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                )
                ORDER BY created_at ASC, id ASC
            """
        cur = await self._c.execute(sql, (conversation_id, limit))
        rows = await cur.fetchall()
        await cur.close()
        return [self._hydrate_message(r) for r in rows]

    async def message_count(self, conversation_id: int) -> int:
        cur = await self._c.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return int(row[0]) if row else 0

    # --- Conversation archival + history search (v=14, UoW-26/27, FC-27) --
    #
    # Channel-side thread deletion means ARCHIVAL, never row deletion
    # (TLC-23): nothing here — or anywhere in store/ — issues a DELETE
    # against `conversations` or `messages`. Search/fetch are SELECT-only
    # (read-only in the TLC-19 spirit); the FTS index is maintained by the
    # v=14 schema triggers, never written directly (FC-27).

    async def archive_conversation(self, channel: str, key: str, *, now: datetime) -> bool:
        """UoW-26: stamp `archived_at = now` on the `(channel, key)` row.
        Returns True only when newly stamped; False if no row exists or it
        is already stamped (idempotent — a duplicate delete event no-ops).
        `now` is injected by the caller for clock-testability."""
        cur = await self._c.execute(
            "UPDATE conversations SET archived_at = ? "
            "WHERE channel = ? AND channel_conversation_key = ? AND archived_at IS NULL",
            (self._iso(now), channel, key),
        )
        await self._c.commit()
        return (cur.rowcount or 0) == 1

    async def unarchive_conversation(self, channel: str, key: str) -> bool:
        """Clear the `archived_at` stamp on the `(channel, key)` row.
        Returns True if a stamp was cleared; False if no row exists or it
        was already live. (`get_or_create_conversation` performs the
        equivalent clear inline on resolution — UoW-26 self-heal.)"""
        cur = await self._c.execute(
            "UPDATE conversations SET archived_at = NULL "
            "WHERE channel = ? AND channel_conversation_key = ? AND archived_at IS NOT NULL",
            (channel, key),
        )
        await self._c.commit()
        return (cur.rowcount or 0) == 1

    async def unarchived_conversation_keys(self, channel: str) -> list[str]:
        """Keys of every live (unstamped) conversation on `channel`, id-ASC
        — the UoW-26 reconciliation sweep's input."""
        cur = await self._c.execute(
            "SELECT channel_conversation_key FROM conversations "
            "WHERE channel = ? AND archived_at IS NULL ORDER BY id ASC",
            (channel,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [str(r[0]) for r in rows]

    async def search_history(
        self,
        query: str,
        *,
        limit: int = 8,
        conversation_id: int | None = None,
    ) -> list[HistoryHit]:
        """UoW-27: FTS5 MATCH over indexed messages, bm25-ranked, joined to
        `conversations`. Archived conversations are always included — hits
        are labeled, never filtered (Decision #7). Malformed FTS5 syntax
        must not surface as an error: on a syntax error the query is
        retried once with every whitespace-separated term double-quoted
        (literal AND search); `StoreError` is raised only if the quoted
        fallback also fails. Read-only."""
        try:
            return await self._search_history_once(
                query, limit=limit, conversation_id=conversation_id
            )
        except aiosqlite.OperationalError:
            quoted = self._quote_fts_terms(query)
            try:
                return await self._search_history_once(
                    quoted, limit=limit, conversation_id=conversation_id
                )
            except aiosqlite.OperationalError as e:
                raise StoreError(
                    f"search_history: query {query!r} failed even after quoting: {e}"
                ) from e

    @staticmethod
    def _quote_fts_terms(query: str) -> str:
        """Fallback form of a query FTS5 rejected: every whitespace-separated
        term double-quoted (embedded double quotes escaped by doubling), so
        operator characters are matched literally."""
        return " ".join('"' + term.replace('"', '""') + '"' for term in query.split())

    async def _search_history_once(
        self,
        match_expr: str,
        *,
        limit: int,
        conversation_id: int | None,
    ) -> list[HistoryHit]:
        conv_clause = "AND m.conversation_id = ? " if conversation_id is not None else ""
        params: tuple[Any, ...] = (
            (match_expr, conversation_id, limit)
            if conversation_id is not None
            else (match_expr, limit)
        )
        # snippet(): column 0 (`content`), no highlight markers, `…` for
        # elided context, ~64 tokens (UoW-27 snippet shape).
        cur = await self._c.execute(
            f"""
            SELECT m.id AS message_id,
                   m.conversation_id AS conversation_id,
                   c.name AS conversation_name,
                   c.archived_at AS archived_at,
                   m.role AS role,
                   m.created_at AS created_at,
                   snippet(messages_fts, 0, '', '', '…', 64) AS snippet
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN conversations c ON c.id = m.conversation_id
            WHERE messages_fts MATCH ?
            {conv_clause}ORDER BY bm25(messages_fts), m.id
            LIMIT ?
            """,
            params,
        )
        rows = await cur.fetchall()
        await cur.close()
        return [
            HistoryHit(
                message_id=r["message_id"],
                conversation_id=r["conversation_id"],
                conversation_name=r["conversation_name"],
                archived=r["archived_at"] is not None,
                role=r["role"],
                created_at=self._parse_dt(r["created_at"]),
                snippet=r["snippet"],
            )
            for r in rows
        ]

    async def history_window(
        self,
        message_id: int,
        *,
        before: int = 5,
        after: int = 5,
    ) -> HistoryWindow | None:
        """UoW-27: conversation meta plus an ordered (by message id) window
        of `before` + anchor + `after` messages around `message_id`. ALL
        roles appear — tool/system rows are part of the record even though
        they aren't indexed. Per-message content is truncated at
        `_HISTORY_CONTENT_MAX_CHARS` with a marker appended. Returns None
        for an unknown id. Read-only."""
        cur = await self._c.execute(
            """
            SELECT m.conversation_id AS conversation_id,
                   c.name AS conversation_name,
                   c.archived_at AS archived_at,
                   c.created_at AS created_at,
                   c.last_active AS last_active
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.id = ?
            """,
            (message_id,),
        )
        meta = await cur.fetchone()
        await cur.close()
        if meta is None:
            return None
        conversation_id = int(meta["conversation_id"])

        cur = await self._c.execute(
            """
            SELECT id, role, created_at, content FROM messages
            WHERE conversation_id = ? AND id < ?
            ORDER BY id DESC LIMIT ?
            """,
            (conversation_id, message_id, before),
        )
        before_rows = list(await cur.fetchall())
        await cur.close()
        cur = await self._c.execute(
            "SELECT id, role, created_at, content FROM messages WHERE id = ?",
            (message_id,),
        )
        anchor_row = await cur.fetchone()
        await cur.close()
        cur = await self._c.execute(
            """
            SELECT id, role, created_at, content FROM messages
            WHERE conversation_id = ? AND id > ?
            ORDER BY id ASC LIMIT ?
            """,
            (conversation_id, message_id, after),
        )
        after_rows = list(await cur.fetchall())
        await cur.close()

        ordered = list(reversed(before_rows)) + ([anchor_row] if anchor_row else []) + after_rows
        return HistoryWindow(
            conversation_id=conversation_id,
            conversation_name=meta["conversation_name"],
            archived=meta["archived_at"] is not None,
            created_at=self._parse_dt(meta["created_at"]),
            last_active=self._parse_dt(meta["last_active"]),
            messages=tuple(
                HistoryWindowMessage(
                    id=r["id"],
                    role=r["role"],
                    created_at=self._parse_dt(r["created_at"]),
                    content=self._truncate_history_content(r["content"]),
                )
                for r in ordered
            ),
        )

    @staticmethod
    def _truncate_history_content(content: str) -> str:
        """Cap window content at `_HISTORY_CONTENT_MAX_CHARS`, appending a
        marker so the model knows the row continues (UoW-27)."""
        if len(content) <= _HISTORY_CONTENT_MAX_CHARS:
            return content
        return content[:_HISTORY_CONTENT_MAX_CHARS] + _HISTORY_TRUNCATION_MARKER

    # --- Feedback + channel message map (v=10, UoW-20 / FC-20) -----------

    @staticmethod
    def _sentiment_for_emoji(emoji: str) -> int | None:
        """Deterministic emoji → sentiment mapping (channel-agnostic).

        👍 → +1, 👎 → -1, everything else → NULL (the raw emoji is kept
        verbatim; nothing downstream may assume a meaning for it). Bare
        code points only — Discord may append a variation selector, so we
        strip U+FE0E/U+FE0F before comparing.
        """
        core = emoji.replace("️", "").replace("︎", "")
        if core == "\U0001f44d":  # 👍
            return 1
        if core == "\U0001f44e":  # 👎
            return -1
        return None

    async def map_channel_messages(
        self,
        channel: str,
        channel_message_ids: list[str],
        message_id: int,
    ) -> None:
        """Record native-channel-message-id → `messages.id` links for an
        agent-authored reply (UoW-20 resolution path). A long reply Discord
        splits into several native messages maps every chunk to the one
        `messages` row. Idempotent: replayed sends `INSERT OR IGNORE` on the
        `(channel, channel_message_id)` primary key. Empty list is a no-op."""
        if not channel_message_ids:
            return
        await self._c.executemany(
            "INSERT OR IGNORE INTO channel_message_map "
            "(channel, channel_message_id, message_id) VALUES (?, ?, ?)",
            [(channel, cmid, message_id) for cmid in channel_message_ids],
        )
        await self._c.commit()

    async def message_id_for_channel_message(
        self, channel: str, channel_message_id: str
    ) -> int | None:
        """Resolve a native channel message id back to its `messages.id`,
        or None if unmapped (pre-bot history, a user message, or anything
        we didn't author) — the UoW-20 "unresolvable → ignored" path."""
        cur = await self._c.execute(
            "SELECT message_id FROM channel_message_map "
            "WHERE channel = ? AND channel_message_id = ?",
            (channel, channel_message_id),
        )
        row = await cur.fetchone()
        await cur.close()
        return int(row[0]) if row else None

    async def record_feedback(self, message_id: int, emoji: str) -> None:
        """UoW-20 add path. Upserts a live `feedback` row for
        `(message_id, emoji)`: if a live row already exists it's a no-op
        (exactly one live row per pair). Sentiment is derived from the
        emoji. Raises `StoreError` if `message_id` is not a real message."""
        cur = await self._c.execute(
            "SELECT conversation_id FROM messages WHERE id = ?",
            (message_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise StoreError(f"record_feedback: message {message_id} does not exist")
        conversation_id = int(row[0])
        sentiment = self._sentiment_for_emoji(emoji)
        # INSERT OR IGNORE against the partial unique index → duplicate live
        # (message, emoji) adds collapse to a no-op (idempotent, UoW-20).
        await self._c.execute(
            "INSERT OR IGNORE INTO feedback "
            "(conversation_id, message_id, emoji, sentiment) VALUES (?, ?, ?, ?)",
            (conversation_id, message_id, emoji, sentiment),
        )
        await self._c.commit()

    async def remove_feedback(self, message_id: int, emoji: str) -> None:
        """UoW-20 remove path. Soft-deletes the live `feedback` row for
        `(message_id, emoji)` by stamping `removed_at` (TLC-8: audit history
        survives). No live row → no-op."""
        await self._c.execute(
            "UPDATE feedback SET removed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE message_id = ? AND emoji = ? AND removed_at IS NULL",
            (message_id, emoji),
        )
        await self._c.commit()

    async def feedback_in_window(self, since: datetime, until: datetime) -> list[Feedback]:
        """Live (non-removed) feedback rows created in `[since, until)`,
        across all conversations — the retrospective's evidence source
        (Layer 3). Retracted signal is excluded (`removed_at IS NULL`)."""
        cur = await self._c.execute(
            "SELECT id, conversation_id, message_id, emoji, sentiment, "
            "created_at, removed_at FROM feedback "
            "WHERE removed_at IS NULL AND created_at >= ? AND created_at < ? "
            "ORDER BY created_at",
            (self._iso(since), self._iso(until)),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [self._hydrate_feedback(r) for r in rows]

    async def feedback_counts(self, conversation_id: int) -> tuple[int, int, int]:
        """Live feedback counts for a conversation as `(pos, neg, other)`
        — surfaced in `/context` (UoW-20 observability)."""
        cur = await self._c.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN sentiment = 1 THEN 1 ELSE 0 END), 0),
              COALESCE(SUM(CASE WHEN sentiment = -1 THEN 1 ELSE 0 END), 0),
              COALESCE(SUM(CASE WHEN sentiment IS NULL THEN 1 ELSE 0 END), 0)
            FROM feedback
            WHERE conversation_id = ? AND removed_at IS NULL
            """,
            (conversation_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return (0, 0, 0)
        return (int(row[0]), int(row[1]), int(row[2]))

    @classmethod
    def _hydrate_feedback(cls, row: aiosqlite.Row) -> Feedback:
        removed = row["removed_at"]
        return Feedback(
            id=row["id"],
            conversation_id=row["conversation_id"],
            message_id=row["message_id"],
            emoji=row["emoji"],
            sentiment=row["sentiment"],
            created_at=cls._parse_dt(row["created_at"]),
            removed_at=cls._parse_dt(removed) if removed else None,
        )

    @staticmethod
    def _iso(dt: datetime) -> str:
        """UTC ISO-8601 with a trailing `Z`, matching the format the schema
        writes via `strftime(... 'now')` so text-range comparisons line up."""
        return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    # --- Evals (v=11, UoW-21) ---------------------------------------------
    #
    # Append-only result tables for the eval harness (FC-21). Re-running is
    # the normal case; runs are never mutated except to stamp `finished_at`
    # + totals once the run completes. The runner writes ONLY these two
    # tables (FC-21) — usage lands in `eval_case_results`, satisfying TLC-5
    # without touching `messages`.

    async def create_eval_run(
        self,
        *,
        selector: str | None,
        fingerprint: str,
        identity_sha: str | None,
        skills_hash: str | None,
        provider_route: str | None,
        image_version: str | None,
    ) -> int:
        """Open a new `eval_runs` row (`finished_at` NULL) and return its id.
        The per-case rows commit as they run; `finalize_eval_run` stamps
        `finished_at` + totals at the end (UoW-21 atomicity: a crash leaves
        `finished_at IS NULL` with partial rows queryable)."""
        cur = await self._c.execute(
            """
            INSERT INTO eval_runs
              (selector, fingerprint, identity_sha, skills_hash, provider_route, image_version)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (selector, fingerprint, identity_sha, skills_hash, provider_route, image_version),
        )
        await self._c.commit()
        run_id = cur.lastrowid
        await cur.close()
        assert run_id is not None
        return int(run_id)

    async def record_case_result(
        self,
        run_id: int,
        *,
        case_id: str,
        kind: str,
        trial: int,
        status: str,
        score: int | None = None,
        judge_reason: str | None = None,
        detail: str | None = None,
        usage: Usage | None = None,
    ) -> int:
        """Append one case×trial result (UoW-21 commit point). `status` must
        be `pass`|`fail`|`error` (schema CHECK enforces). Usage is recorded
        per-trial for cost transparency (TLC-5)."""
        cur = await self._c.execute(
            """
            INSERT INTO eval_case_results
              (run_id, case_id, kind, trial, status, score, judge_reason, detail,
               tokens_in, tokens_out, cached_tokens, cost_usd, subscription_turn)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                case_id,
                kind,
                trial,
                status,
                score,
                judge_reason,
                detail,
                usage.input_tokens if usage else None,
                usage.output_tokens if usage else None,
                usage.cached_tokens if usage else None,
                usage.cost_usd if usage else None,
                1 if (usage and usage.subscription) else 0,
            ),
        )
        await self._c.commit()
        rid = cur.lastrowid
        await cur.close()
        assert rid is not None
        return int(rid)

    async def finalize_eval_run(
        self,
        run_id: int,
        *,
        total_cases: int,
        passed: int,
        failed: int,
        errored: int,
        skipped: int,
    ) -> None:
        """Stamp `finished_at` + totals on a run (UoW-21 finalize)."""
        await self._c.execute(
            """
            UPDATE eval_runs
            SET finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                total_cases = ?, passed = ?, failed = ?, errored = ?, skipped = ?
            WHERE id = ?
            """,
            (total_cases, passed, failed, errored, skipped, run_id),
        )
        await self._c.commit()

    async def get_eval_run(self, run_id: int) -> EvalRun | None:
        cur = await self._c.execute("SELECT * FROM eval_runs WHERE id = ?", (run_id,))
        row = await cur.fetchone()
        await cur.close()
        return self._hydrate_eval_run(row) if row else None

    async def latest_eval_run(self, *, finished_only: bool = True) -> EvalRun | None:
        """Most recent run by `started_at`. `finished_only` (default) skips
        runs still in flight / crashed, which is what `/eval report` wants."""
        clause = "WHERE finished_at IS NOT NULL " if finished_only else ""
        cur = await self._c.execute(
            f"SELECT * FROM eval_runs {clause}ORDER BY started_at DESC, id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        await cur.close()
        return self._hydrate_eval_run(row) if row else None

    async def previous_eval_run(
        self, before_run_id: int, *, selector: str | None
    ) -> EvalRun | None:
        """The most recent finished run BEFORE `before_run_id` with the same
        `selector` (same case set) — the delta baseline for `/eval report`.
        `selector IS NULL` matches only other whole-suite runs."""
        base = await self.get_eval_run(before_run_id)
        if base is None:
            return None
        if selector is None:
            sel_clause = "selector IS NULL"
            params: tuple[Any, ...] = (base.id,)
        else:
            sel_clause = "selector = ?"
            params = (selector, base.id)
        cur = await self._c.execute(
            f"""
            SELECT * FROM eval_runs
            WHERE finished_at IS NOT NULL AND {sel_clause} AND id < ?
            ORDER BY id DESC LIMIT 1
            """,
            params,
        )
        row = await cur.fetchone()
        await cur.close()
        return self._hydrate_eval_run(row) if row else None

    async def case_results_for_run(self, run_id: int) -> list[EvalCaseResult]:
        """All case×trial rows for a run, ordered by (case_id, trial)."""
        cur = await self._c.execute(
            "SELECT * FROM eval_case_results WHERE run_id = ? ORDER BY case_id, trial",
            (run_id,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [self._hydrate_eval_case_result(r) for r in rows]

    @classmethod
    def _hydrate_eval_run(cls, row: aiosqlite.Row) -> EvalRun:
        fin = row["finished_at"]
        return EvalRun(
            id=row["id"],
            started_at=cls._parse_dt(row["started_at"]),
            finished_at=cls._parse_dt(fin) if fin else None,
            selector=row["selector"],
            fingerprint=row["fingerprint"],
            identity_sha=row["identity_sha"],
            skills_hash=row["skills_hash"],
            provider_route=row["provider_route"],
            image_version=row["image_version"],
            total_cases=row["total_cases"],
            passed=row["passed"],
            failed=row["failed"],
            errored=row["errored"],
            skipped=row["skipped"],
        )

    @classmethod
    def _hydrate_eval_case_result(cls, row: aiosqlite.Row) -> EvalCaseResult:
        if row["tokens_in"] is None and row["tokens_out"] is None:
            usage: Usage | None = None
        else:
            usage = Usage(
                input_tokens=row["tokens_in"] or 0,
                output_tokens=row["tokens_out"] or 0,
                cached_tokens=row["cached_tokens"] or 0,
                cost_usd=row["cost_usd"],
                subscription=bool(row["subscription_turn"]),
            )
        return EvalCaseResult(
            id=row["id"],
            run_id=row["run_id"],
            case_id=row["case_id"],
            kind=row["kind"],
            trial=row["trial"],
            status=row["status"],
            score=row["score"],
            judge_reason=row["judge_reason"],
            detail=row["detail"],
            usage=usage,
            created_at=cls._parse_dt(row["created_at"]),
        )

    # --- Learning loop Layer 3: evidence reads + proposals (v=12) ----------
    #
    # The nightly retrospective (UoW-22 / FC-22) reads evidence through the
    # narrow methods below and writes only `proposals`. TLC-20 (provenance)
    # is enforced at TWO layers: the read side surfaces only evidence-eligible
    # rows (user messages, live feedback, failed runs), and `record_proposal`
    # re-validates every evidence id at insert so a buggy caller still can't
    # persist a proposal citing agent-authored rows.

    async def conversations_active_since(self, since: datetime) -> list[Conversation]:
        """Conversations with `last_active >= since`, id-ASC (UoW-22 input
        selection). The retrospective runs one extraction per row."""
        cur = await self._c.execute(
            "SELECT * FROM conversations WHERE last_active >= ? ORDER BY id",
            (self._iso(since),),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [self._hydrate_conversation(r) for r in rows]

    async def user_messages_in_window(
        self, conversation_id: int, since: datetime, until: datetime
    ) -> list[StoredMessage]:
        """User-authored messages for a conversation in `[since, until)`,
        oldest-first — the ONLY transcript content the retrospective may feed
        into extraction (TLC-20: agent output is never evidence). Compacted
        rows are included: the summary that replaced them is agent-authored
        and thus ineligible, but the original user turns remain valid signal.
        """
        cur = await self._c.execute(
            """
            SELECT * FROM messages
            WHERE conversation_id = ? AND role = 'user'
              AND created_at >= ? AND created_at < ?
            ORDER BY created_at ASC, id ASC
            """,
            (conversation_id, self._iso(since), self._iso(until)),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [self._hydrate_message(r) for r in rows]

    async def failed_runs_in_window(
        self, since: datetime, until: datetime
    ) -> list[ScheduledRunRow]:
        """Non-ok scheduled runs across all skills whose `started_at` falls in
        `[since, until)` (UoW-22 evidence: recorded failures). `missed` rows
        have a NULL `started_at`, so they are matched on `scheduled_for`."""
        cur = await self._c.execute(
            """
            SELECT * FROM scheduled_runs
            WHERE status != 'ok'
              AND COALESCE(started_at, scheduled_for) >= ?
              AND COALESCE(started_at, scheduled_for) < ?
            ORDER BY id
            """,
            (self._iso(since), self._iso(until)),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [self._hydrate_scheduled_run(r) for r in rows]

    async def tool_errors_in_window(self, since: datetime, until: datetime) -> list[ToolErrorRow]:
        """Tool calls recorded with `status='error'` on assistant rows whose
        `created_at` falls in `[since, until)`, oldest-first (doc #36
        Decisions #26: silent tool failures are a deterministic finding
        source). Each is paired with the id of the nearest earlier
        `role='user'` message in the same conversation — the assistant row
        carrying the call is agent output and never evidence (TLC-20)."""
        cur = await self._c.execute(
            """
            SELECT * FROM messages
            WHERE role = 'assistant' AND tool_calls_json IS NOT NULL
              AND created_at >= ? AND created_at < ?
            ORDER BY created_at ASC, id ASC
            """,
            (self._iso(since), self._iso(until)),
        )
        rows = await cur.fetchall()
        await cur.close()

        out: list[ToolErrorRow] = []
        user_before: dict[int, int | None] = {}
        for r in rows:
            msg = self._hydrate_message(r)
            errored = [c for c in (msg.tool_calls or []) if c.status == "error"]
            if not errored:
                continue
            if msg.id not in user_before:
                cur = await self._c.execute(
                    """
                    SELECT id FROM messages
                    WHERE conversation_id = ? AND role = 'user' AND id < ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (msg.conversation_id, msg.id),
                )
                urow = await cur.fetchone()
                await cur.close()
                user_before[msg.id] = int(urow["id"]) if urow is not None else None
            for call in errored:
                out.append(
                    ToolErrorRow(
                        message_id=msg.id,
                        conversation_id=msg.conversation_id,
                        created_at=msg.created_at,
                        tool=call.name,
                        args=dict(call.args or {}),
                        user_message_id=user_before[msg.id],
                    )
                )
        return out

    @staticmethod
    def payload_hash(kind: str, payload: Any) -> str:
        """Stable dedup key for a proposal (UoW-22). Canonical JSON of the
        payload keyed by kind — the retrospective must build the same payload
        for the same finding across runs so a re-run reinforces rather than
        duplicates."""
        import hashlib

        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(f"{kind}\x00{canonical}".encode()).hexdigest()

    async def _validate_evidence(self, evidence: dict[str, list[int]]) -> None:
        """TLC-20 gate. Raise `StoreError` unless every evidence id resolves
        to an evidence-eligible row: messages must be `role='user'`, feedback
        must be live (`removed_at IS NULL`), runs must be non-ok failures. A
        proposal with no evidence at all is rejected (no provenance)."""
        msgs = evidence.get("messages", [])
        fbs = evidence.get("feedback", [])
        runs = evidence.get("runs", [])
        if not (msgs or fbs or runs):
            raise StoreError("record_proposal: proposal has no evidence (TLC-20)")

        for mid in msgs:
            cur = await self._c.execute("SELECT role FROM messages WHERE id = ?", (mid,))
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                raise StoreError(f"record_proposal: message {mid} does not exist")
            if row["role"] != "user":
                raise StoreError(
                    f"record_proposal: message {mid} is role={row['role']!r}, "
                    "not user — agent output is never evidence (TLC-20)"
                )

        for fid in fbs:
            cur = await self._c.execute("SELECT removed_at FROM feedback WHERE id = ?", (fid,))
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                raise StoreError(f"record_proposal: feedback {fid} does not exist")
            if row["removed_at"] is not None:
                raise StoreError(
                    f"record_proposal: feedback {fid} is retracted — "
                    "removed signal is never evidence (TLC-20)"
                )

        for rid in runs:
            cur = await self._c.execute("SELECT status FROM scheduled_runs WHERE id = ?", (rid,))
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                raise StoreError(f"record_proposal: run {rid} does not exist")
            if row["status"] == "ok":
                raise StoreError(
                    f"record_proposal: run {rid} succeeded — only recorded "
                    "failures are evidence (TLC-20)"
                )

    async def record_proposal(
        self,
        *,
        kind: str,
        summary: str,
        payload: Any,
        evidence: dict[str, list[int]],
    ) -> tuple[int, bool]:
        """Insert a pending proposal, or reinforce an existing open one
        (UoW-22 / FC-23). Returns `(proposal_id, reinforced)`.

        Enforces TLC-20 first: every evidence id is validated before any
        write, so a rejected proposal touches nothing. Dedup is by
        `(kind, payload_hash)` over `status='pending'` rows: a match bumps
        `reinforced_count`, unions the new evidence into the stored bundle,
        and returns `reinforced=True`; otherwise a fresh `pending` row is
        inserted. v12 never writes any status but `pending` (accumulate-only,
        TLC-21a).
        """
        # Normalize + dedup evidence id lists up front.
        norm = {
            "messages": sorted({int(x) for x in evidence.get("messages", [])}),
            "feedback": sorted({int(x) for x in evidence.get("feedback", [])}),
            "runs": sorted({int(x) for x in evidence.get("runs", [])}),
        }
        await self._validate_evidence(norm)

        phash = self.payload_hash(kind, payload)
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))

        cur = await self._c.execute(
            "SELECT id, evidence_json FROM proposals "
            "WHERE kind = ? AND payload_hash = ? AND status = 'pending'",
            (kind, phash),
        )
        existing = await cur.fetchone()
        await cur.close()

        if existing is not None:
            merged = self._merge_evidence(existing["evidence_json"], norm)
            await self._c.execute(
                "UPDATE proposals SET reinforced_count = reinforced_count + 1, "
                "evidence_json = ? WHERE id = ?",
                (json.dumps(merged, sort_keys=True, separators=(",", ":")), existing["id"]),
            )
            await self._c.commit()
            return int(existing["id"]), True

        cur = await self._c.execute(
            """
            INSERT INTO proposals (kind, summary, payload_json, evidence_json, payload_hash)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                kind,
                summary,
                payload_json,
                json.dumps(norm, sort_keys=True, separators=(",", ":")),
                phash,
            ),
        )
        await self._c.commit()
        return int(cur.lastrowid), False

    @staticmethod
    def _merge_evidence(existing_json: str, incoming: dict[str, list[int]]) -> dict[str, list[int]]:
        """Union the two typed evidence bundles, id lists sorted + deduped."""
        try:
            existing = json.loads(existing_json)
        except (TypeError, ValueError):
            existing = {}
        out: dict[str, list[int]] = {}
        for key in ("messages", "feedback", "runs"):
            ids = set(int(x) for x in existing.get(key, []))
            ids |= set(int(x) for x in incoming.get(key, []))
            out[key] = sorted(ids)
        return out

    async def list_proposals(
        self, *, status: str | None = None, limit: int = 200
    ) -> list[Proposal]:
        """Proposals, newest-first, optionally filtered by `status`. Used by
        the acceptance smoke-test and (v12.5) the digest."""
        if status is None:
            cur = await self._c.execute(
                "SELECT * FROM proposals ORDER BY id DESC LIMIT ?", (limit,)
            )
        else:
            cur = await self._c.execute(
                "SELECT * FROM proposals WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            )
        rows = await cur.fetchall()
        await cur.close()
        return [self._hydrate_proposal(r) for r in rows]

    async def applied_proposals_since(self, since: datetime) -> list[Proposal]:
        """Proposals `status='applied'` with `resolved_at >= since`, newest
        resolved first — the trailing-window changes the v13 digest reports an
        eval delta for (UoW-24 / B)."""
        cur = await self._c.execute(
            "SELECT * FROM proposals WHERE status = 'applied' AND resolved_at IS NOT NULL "
            "AND resolved_at >= ? ORDER BY resolved_at DESC, id DESC",
            (self._iso(since),),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [self._hydrate_proposal(r) for r in rows]

    @classmethod
    def _hydrate_proposal(cls, row: aiosqlite.Row) -> Proposal:
        resolved = row["resolved_at"]
        return Proposal(
            id=row["id"],
            created_at=cls._parse_dt(row["created_at"]),
            kind=row["kind"],
            summary=row["summary"],
            payload_json=row["payload_json"],
            evidence_json=row["evidence_json"],
            payload_hash=row["payload_hash"],
            reinforced_count=row["reinforced_count"],
            status=row["status"],
            resolved_at=cls._parse_dt(resolved) if resolved else None,
            applied_eval_run_id=row["applied_eval_run_id"],
        )

    # --- Proposal state machine (v=12.5, FC-23) ---------------------------
    #
    # The applier half of the learning loop. These are the ONLY methods that
    # write a proposal status other than `pending`. The state machine is
    # `pending → approved → applied`, `pending → rejected`, `pending →
    # expired`; there is no other edge and terminal states are immutable. Each
    # transition is a single guarded UPDATE (`WHERE id=? AND status=<expected>`)
    # whose rowcount is the enforcement: 0 rows changed → the id is unknown or
    # in the wrong state → `ProposalStateError` and nothing is mutated. `now`
    # is always injected by the caller (never `datetime.now()` here) so expiry
    # and apply-stamping are clock-testable (TLC-21d).

    async def get_proposal(self, proposal_id: int) -> Proposal | None:
        """The proposal row, or None if the id is unknown."""
        cur = await self._c.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        row = await cur.fetchone()
        await cur.close()
        return self._hydrate_proposal(row) if row is not None else None

    async def _transition(
        self,
        proposal_id: int,
        *,
        from_status: str,
        to_status: str,
        resolved_at: datetime | None = None,
        applied_eval_run_id: int | None = None,
    ) -> Proposal:
        """Guarded status edge. Updates `proposals` iff the row exists AND is
        currently `from_status`; otherwise raises `ProposalStateError` (unknown
        id, or an illegal edge from a wrong/terminal state). Returns the updated
        row. `resolved_at` is stamped only on terminal transitions."""
        sets = ["status = ?"]
        params: list[Any] = [to_status]
        if resolved_at is not None:
            sets.append("resolved_at = ?")
            params.append(self._iso(resolved_at))
        if applied_eval_run_id is not None:
            sets.append("applied_eval_run_id = ?")
            params.append(applied_eval_run_id)
        params += [proposal_id, from_status]
        cur = await self._c.execute(
            f"UPDATE proposals SET {', '.join(sets)} WHERE id = ? AND status = ?",
            tuple(params),
        )
        await self._c.commit()
        if cur.rowcount != 1:
            current = await self.get_proposal(proposal_id)
            if current is None:
                raise ProposalStateError(f"proposal {proposal_id} does not exist")
            raise ProposalStateError(
                f"proposal {proposal_id} is {current.status!r}, not {from_status!r} — "
                f"cannot transition to {to_status!r} (FC-23)"
            )
        updated = await self.get_proposal(proposal_id)
        assert updated is not None  # just updated it
        return updated

    async def approve_proposal(self, proposal_id: int) -> Proposal:
        """`pending → approved`. `approved` is not terminal (application still
        has to run), so `resolved_at` stays NULL. Raises `ProposalStateError`
        if the id is unknown or its status is not `pending`."""
        return await self._transition(proposal_id, from_status="pending", to_status="approved")

    async def mark_applied(
        self,
        proposal_id: int,
        *,
        now: datetime,
        applied_eval_run_id: int | None = None,
    ) -> Proposal:
        """`approved → applied` (terminal). Stamps `resolved_at = now`. The
        writer path must have committed already — this only records that it
        did. Raises `ProposalStateError` unless the row is currently
        `approved`. (`applied_eval_run_id` is the v13 closure link.)"""
        return await self._transition(
            proposal_id,
            from_status="approved",
            to_status="applied",
            resolved_at=now,
            applied_eval_run_id=applied_eval_run_id,
        )

    async def link_applied_eval_run(self, proposal_id: int, eval_run_id: int) -> None:
        """v13 closure link (UoW-24): stamp `applied_eval_run_id` on an APPLIED
        proposal at auto-eval run creation, so the link survives even if the run
        crashes mid-flight. Guarded on `status='applied'` (the applier marks the
        proposal applied before enqueuing the eval); a no-match is a silent
        no-op (the eval still runs; only the back-link is skipped)."""
        await self._c.execute(
            "UPDATE proposals SET applied_eval_run_id = ? WHERE id = ? AND status = 'applied'",
            (eval_run_id, proposal_id),
        )
        await self._c.commit()

    async def reject_proposal(self, proposal_id: int, *, now: datetime) -> Proposal:
        """`pending → rejected` (terminal). Stamps `resolved_at = now`. Raises
        `ProposalStateError` unless the row is currently `pending`."""
        return await self._transition(
            proposal_id, from_status="pending", to_status="rejected", resolved_at=now
        )

    async def reject_proposals(self, proposal_ids: list[int], *, now: datetime) -> list[int]:
        """Bulk `pending → rejected` for the given ids. Rejects every id that is
        currently `pending` and silently skips the rest (unknown / already
        resolved) — bulk reject is a queue-clearing convenience, so a stale id in
        the batch is a no-op, not an error. Returns the ids actually rejected.
        Stamps `resolved_at = now` (injected for testability)."""
        ids = sorted({int(x) for x in proposal_ids})
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        cur = await self._c.execute(
            f"SELECT id FROM proposals WHERE id IN ({placeholders}) AND status = 'pending'",
            tuple(ids),
        )
        pending = [r["id"] for r in await cur.fetchall()]
        await cur.close()
        if not pending:
            return []
        ph2 = ",".join("?" for _ in pending)
        await self._c.execute(
            f"UPDATE proposals SET status = 'rejected', resolved_at = ? "
            f"WHERE id IN ({ph2}) AND status = 'pending'",
            (self._iso(now), *pending),
        )
        await self._c.commit()
        return pending

    async def edit_proposal(
        self, proposal_id: int, *, payload: Any, summary: str | None = None
    ) -> Proposal:
        """Replace a PENDING proposal's payload (recomputing `payload_hash`)
        and, optionally, its summary. The row stays `pending` — the caller
        approves + applies afterwards. Raises `ProposalStateError` if the row
        is not currently `pending`, or if the new `(kind, payload_hash)`
        collides with a DIFFERENT pending row (the pending-dedup uniqueness is
        preserved)."""
        current = await self.get_proposal(proposal_id)
        if current is None:
            raise ProposalStateError(f"proposal {proposal_id} does not exist")
        if current.status != "pending":
            raise ProposalStateError(
                f"proposal {proposal_id} is {current.status!r}, not 'pending' — "
                "only pending proposals can be edited (FC-23)"
            )
        phash = self.payload_hash(current.kind, payload)
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        sets = ["payload_json = ?", "payload_hash = ?"]
        params: list[Any] = [payload_json, phash]
        if summary is not None:
            sets.append("summary = ?")
            params.append(summary)
        params.append(proposal_id)
        try:
            await self._c.execute(
                f"UPDATE proposals SET {', '.join(sets)} WHERE id = ?",
                tuple(params),
            )
            await self._c.commit()
        except aiosqlite.IntegrityError as e:
            await self._c.rollback()
            raise ProposalStateError(
                f"edit of proposal {proposal_id} would collide with an existing "
                f"pending proposal of the same kind+payload: {e}"
            ) from e
        updated = await self.get_proposal(proposal_id)
        assert updated is not None
        return updated

    async def expire_proposals(self, *, older_than: datetime, now: datetime) -> list[int]:
        """Bulk `pending → expired` (terminal) for every pending proposal
        created before `older_than`, stamping `resolved_at = now`. Returns the
        expired ids (empty if none). Only `pending` rows are touched; expiry is
        silent (TLC-21d) — this method has no side effect beyond the status
        write, and callers do not announce it."""
        cur = await self._c.execute(
            "SELECT id FROM proposals WHERE status = 'pending' AND created_at < ? ORDER BY id",
            (self._iso(older_than),),
        )
        ids = [r["id"] for r in await cur.fetchall()]
        await cur.close()
        if not ids:
            return []
        await self._c.execute(
            "UPDATE proposals SET status = 'expired', resolved_at = ? "
            "WHERE status = 'pending' AND created_at < ?",
            (self._iso(now), self._iso(older_than)),
        )
        await self._c.commit()
        return ids

    # --- Lessons (v=13, learning loop Layer 4 / FC-26) --------------------
    #
    # The `lessons` table is the source of truth; `lessons.md` is a
    # deterministic render of the live rows (see `identity/lessons.py`). These
    # are the ONLY methods that write it. `now`/`created_at` are injected by
    # the caller (never `datetime.now()` here) so reinforcement/staleness are
    # clock-testable. All matching is by the normalized-text `dedup_key`
    # (seam ①) among LIVE rows (`removed_at IS NULL`).

    async def insert_lesson(
        self,
        *,
        text: str,
        evidence: dict[str, list[int]],
        dedup_key: str,
        created_at: datetime,
        reinforced_count: int = 0,
        source_proposal_id: int | None = None,
    ) -> int:
        """Insert a live lesson row and return its id. `last_reinforced_at` is
        seeded to `created_at`. Raises `StoreError` if a LIVE row already shares
        `dedup_key` (the partial-unique index) — the caller should reinforce
        instead (see `reinforce_lesson`)."""
        norm = {
            "messages": sorted({int(x) for x in evidence.get("messages", [])}),
            "feedback": sorted({int(x) for x in evidence.get("feedback", [])}),
            "runs": sorted({int(x) for x in evidence.get("runs", [])}),
        }
        iso = self._iso(created_at)
        try:
            cur = await self._c.execute(
                """
                INSERT INTO lessons
                  (text, evidence_json, dedup_key, created_at, last_reinforced_at,
                   reinforced_count, source_proposal_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    text,
                    json.dumps(norm, sort_keys=True, separators=(",", ":")),
                    dedup_key,
                    iso,
                    iso,
                    reinforced_count,
                    source_proposal_id,
                ),
            )
        except aiosqlite.IntegrityError as e:
            await self._c.rollback()
            raise StoreError(
                f"insert_lesson: a live lesson already exists for dedup_key "
                f"{dedup_key!r} — reinforce instead ({e})"
            ) from e
        await self._c.commit()
        lid = cur.lastrowid
        await cur.close()
        assert lid is not None
        return int(lid)

    async def reinforce_lesson(self, dedup_key: str, *, now: datetime) -> Lesson | None:
        """Bump `last_reinforced_at = now` + `reinforced_count += 1` on the LIVE
        lesson matching `dedup_key`. Returns the updated row, or None if no live
        row matches (the caller then proposes a fresh lesson). This is how an
        applied lesson stays 'fresh' (UoW-25 / seam ①)."""
        cur = await self._c.execute(
            "SELECT id FROM lessons WHERE dedup_key = ? AND removed_at IS NULL",
            (dedup_key,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        lesson_id = row["id"]
        await self._c.execute(
            "UPDATE lessons SET last_reinforced_at = ?, reinforced_count = reinforced_count + 1 "
            "WHERE id = ?",
            (self._iso(now), lesson_id),
        )
        await self._c.commit()
        return await self.get_lesson(lesson_id)

    async def get_lesson(self, lesson_id: int) -> Lesson | None:
        cur = await self._c.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,))
        row = await cur.fetchone()
        await cur.close()
        return self._hydrate_lesson(row) if row is not None else None

    async def live_lessons(self) -> list[Lesson]:
        """All live lessons (`removed_at IS NULL`), ordered by `id` ASC — the
        stable order the lessons.md render depends on for TLC-12 byte-stability."""
        cur = await self._c.execute(
            "SELECT * FROM lessons WHERE removed_at IS NULL ORDER BY id ASC"
        )
        rows = await cur.fetchall()
        await cur.close()
        return [self._hydrate_lesson(r) for r in rows]

    async def stale_lessons(self, *, older_than: datetime) -> list[Lesson]:
        """Live lessons whose `last_reinforced_at` is strictly before
        `older_than` — the weekly digest's prune candidates (UoW-25). Ordered by
        `id` ASC."""
        cur = await self._c.execute(
            "SELECT * FROM lessons WHERE removed_at IS NULL AND last_reinforced_at < ? "
            "ORDER BY id ASC",
            (self._iso(older_than),),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [self._hydrate_lesson(r) for r in rows]

    async def soft_delete_lesson(self, lesson_id: int, *, now: datetime) -> Lesson | None:
        """Soft-delete a live lesson (`removed_at = now`), dropping it from the
        render (UoW-25 prune). Idempotent-safe: returns the row on success, or
        None if the id is unknown or already removed (guarded on
        `removed_at IS NULL`)."""
        cur = await self._c.execute(
            "UPDATE lessons SET removed_at = ? WHERE id = ? AND removed_at IS NULL",
            (self._iso(now), lesson_id),
        )
        await self._c.commit()
        if cur.rowcount != 1:
            return None
        return await self.get_lesson(lesson_id)

    @classmethod
    def _hydrate_lesson(cls, row: aiosqlite.Row) -> Lesson:
        removed = row["removed_at"]
        return Lesson(
            id=row["id"],
            text=row["text"],
            evidence_json=row["evidence_json"],
            dedup_key=row["dedup_key"],
            created_at=cls._parse_dt(row["created_at"]),
            last_reinforced_at=cls._parse_dt(row["last_reinforced_at"]),
            reinforced_count=row["reinforced_count"],
            source_proposal_id=row["source_proposal_id"],
            removed_at=cls._parse_dt(removed) if removed else None,
        )

    # --- Compaction / summaries / pins (v=4, UoW-12) ----------------------

    async def mark_messages_compacted(self, message_ids: list[int]) -> None:
        """Flip `messages.compacted` to 1 for the given ids. Idempotent —
        re-marking already-compacted rows is a no-op (SET to 1 again).
        Empty list is a no-op (no SQL emitted)."""
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        await self._c.execute(
            f"UPDATE messages SET compacted = 1 WHERE id IN ({placeholders})",
            tuple(message_ids),
        )
        await self._c.commit()

    async def trim_oldest_active(self, conversation_id: int, n: int) -> int:
        """Mark the oldest N non-compacted messages as compacted=1.

        Returns how many were actually marked (clamped by available count).
        N <= 0 is a no-op returning 0.
        """
        if n <= 0:
            return 0
        cur = await self._c.execute(
            """
            SELECT id FROM messages
            WHERE conversation_id = ? AND compacted = 0
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (conversation_id, n),
        )
        rows = await cur.fetchall()
        await cur.close()
        ids = [int(r[0]) for r in rows]
        if not ids:
            return 0
        await self.mark_messages_compacted(ids)
        return len(ids)

    async def insert_summary(
        self,
        conversation_id: int,
        *,
        start_message_id: int,
        end_message_id: int,
        summary_text: str,
    ) -> int:
        """UoW-12: record one compaction event. Returns inserted row id."""
        try:
            cur = await self._c.execute(
                """
                INSERT INTO summaries (
                    conversation_id, start_message_id, end_message_id, summary_text
                ) VALUES (?, ?, ?, ?)
                """,
                (conversation_id, start_message_id, end_message_id, summary_text),
            )
            await self._c.commit()
        except aiosqlite.IntegrityError as e:
            await self._c.rollback()
            raise StoreError(f"insert_summary integrity: {e}") from e
        new_id = cur.lastrowid
        await cur.close()
        if new_id is None:
            raise StoreError("INSERT did not return lastrowid")
        return new_id

    async def delete_summary(self, summary_id: int) -> None:
        """Delete a single summary row by id.

        Used by the compactor to roll back a `insert_summary` when the
        follow-up `mark_messages_compacted` fails — keeps the
        all-or-nothing observable transition (UoW-12) without needing a
        SQL-level transaction wrapping both calls.
        """
        await self._c.execute(
            "DELETE FROM summaries WHERE id = ?",
            (summary_id,),
        )
        await self._c.commit()

    async def delete_summaries_for_conversation(self, conversation_id: int) -> int:
        """v=15 (UoW-28 step 3): delete ALL summaries rows for a
        conversation, returning how many were deleted. Session rotation
        supersedes prior handoffs/compaction summaries with one fresh
        handoff row. Summaries are derivative rows — the only deletable
        table under TLC-23 (`/clear` already relies on the same carve-out).
        """
        cur = await self._c.execute(
            "DELETE FROM summaries WHERE conversation_id = ?",
            (conversation_id,),
        )
        deleted = cur.rowcount or 0
        await cur.close()
        await self._c.commit()
        return int(deleted)

    async def list_summaries(self, conversation_id: int) -> list[Summary]:
        """All summaries for a conversation, ordered by start_message_id
        ASC (TLC-12: byte-stable prompt assembly)."""
        cur = await self._c.execute(
            """
            SELECT id, conversation_id, start_message_id, end_message_id,
                   summary_text, created_at
            FROM summaries
            WHERE conversation_id = ?
            ORDER BY start_message_id ASC, id ASC
            """,
            (conversation_id,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [
            Summary(
                id=r["id"],
                conversation_id=r["conversation_id"],
                start_message_id=r["start_message_id"],
                end_message_id=r["end_message_id"],
                summary_text=r["summary_text"],
                created_at=self._parse_dt(r["created_at"]),
            )
            for r in rows
        ]

    async def add_pin(self, conversation_id: int, content: str) -> int:
        """Insert one pinned-context row. Returns inserted row id."""
        try:
            cur = await self._c.execute(
                "INSERT INTO pinned (conversation_id, content) VALUES (?, ?)",
                (conversation_id, content),
            )
            await self._c.commit()
        except aiosqlite.IntegrityError as e:
            await self._c.rollback()
            raise StoreError(f"add_pin integrity: {e}") from e
        new_id = cur.lastrowid
        await cur.close()
        if new_id is None:
            raise StoreError("INSERT did not return lastrowid")
        return new_id

    async def list_pins(self, conversation_id: int) -> list[Pin]:
        """All pins for a conversation, ordered by id ASC (insertion
        order)."""
        cur = await self._c.execute(
            """
            SELECT id, conversation_id, content, created_at
            FROM pinned
            WHERE conversation_id = ?
            ORDER BY id ASC
            """,
            (conversation_id,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [
            Pin(
                id=r["id"],
                conversation_id=r["conversation_id"],
                content=r["content"],
                created_at=self._parse_dt(r["created_at"]),
            )
            for r in rows
        ]

    # --- Pending profile proposal (UoW-8) --------------------------------

    async def set_pending_proposal(
        self,
        conversation_id: int,
        proposal_json: str | None,
    ) -> None:
        """Stash (or clear) a profile-update proposal awaiting user
        confirmation (UoW-8). Pass None to clear."""
        await self._c.execute(
            "UPDATE conversations SET pending_profile_proposal = ? WHERE id = ?",
            (proposal_json, conversation_id),
        )
        await self._c.commit()

    async def consume_pending_proposal(self, conversation_id: int) -> str | None:
        """Atomic read+clear of `pending_profile_proposal`. Returns the
        prior value (None if unset). After this call the column is
        always NULL."""
        cur = await self._c.execute(
            "SELECT pending_profile_proposal FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        prior: str | None = row[0]
        if prior is not None:
            await self._c.execute(
                "UPDATE conversations SET pending_profile_proposal = NULL WHERE id = ?",
                (conversation_id,),
            )
            await self._c.commit()
        return prior

    # --- Pending tool confirmation (TLC-3) -------------------------------

    async def set_pending_tool_confirmation(
        self,
        conversation_id: int,
        payload_json: str | None,
    ) -> None:
        """Stash (or clear) a tool-confirmation payload awaiting user
        confirmation (TLC-3). Pass None to clear."""
        await self._c.execute(
            "UPDATE conversations SET pending_tool_confirmation = ? WHERE id = ?",
            (payload_json, conversation_id),
        )
        await self._c.commit()

    async def consume_pending_tool_confirmation(self, conversation_id: int) -> str | None:
        """Atomic read+clear of `pending_tool_confirmation`. Returns the
        prior value (None if unset). After this call the column is
        always NULL."""
        cur = await self._c.execute(
            "SELECT pending_tool_confirmation FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        prior: str | None = row[0]
        if prior is not None:
            await self._c.execute(
                "UPDATE conversations SET pending_tool_confirmation = NULL WHERE id = ?",
                (conversation_id,),
            )
            await self._c.commit()
        return prior

    # --- Pending Schwab re-auth (BR-FC-11) -------------------------------

    async def set_pending_schwab_reauth(
        self,
        conversation_id: int,
        json_blob: str | None,
    ) -> None:
        """Stash (or clear) a Schwab re-auth handshake payload (BR-FC-11).
        Pass None to clear. Payload shape:
        ``{state, callback_url, expires_at}``."""
        await self._c.execute(
            "UPDATE conversations SET pending_schwab_reauth = ? WHERE id = ?",
            (json_blob, conversation_id),
        )
        await self._c.commit()

    async def get_pending_schwab_reauth(self, conversation_id: int) -> str | None:
        """Read `pending_schwab_reauth` without clearing it. The redirect-URL
        pre-handler peeks via this method to decide whether the message
        should be intercepted; only on intercept (or expiry) does it
        ``consume_pending_schwab_reauth`` to clear the row."""
        cur = await self._c.execute(
            "SELECT pending_schwab_reauth FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return row[0]

    async def consume_pending_schwab_reauth(self, conversation_id: int) -> str | None:
        """Atomic read+clear of `pending_schwab_reauth`. Returns the prior
        value (None if unset). After this call the column is always NULL."""
        cur = await self._c.execute(
            "SELECT pending_schwab_reauth FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        prior: str | None = row[0]
        if prior is not None:
            await self._c.execute(
                "UPDATE conversations SET pending_schwab_reauth = NULL WHERE id = ?",
                (conversation_id,),
            )
            await self._c.commit()
        return prior

    # --- Clear conversation ----------------------------------------------

    async def clear_conversation(self, conversation_id: int) -> ClearResult:
        """Reset working state for a conversation:
        - mark every message compacted=1 (so prompt assembly skips them);
        - delete summaries rows (a fresh slate has no compaction history);
        - null `claude_cli_session_id` (sever the resumable CLI session);
        - clear `pending_profile_proposal`.

        Pins survive (architecture doc § Conversation store: "pinned
        survives trim/compact"). Returns counts so callers can describe
        what changed.

        `session_reset` is True only when there was a non-NULL session id
        before the call (i.e., we actually severed something). Empty
        conversations report False.
        """
        # Snapshot the old session id so `session_reset` is honest.
        cur = await self._c.execute(
            "SELECT claude_cli_session_id FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        prior_session: str | None = row[0] if row is not None else None

        # Mark all currently-active (compacted=0) messages.
        cur = await self._c.execute(
            "UPDATE messages SET compacted = 1 WHERE conversation_id = ? AND compacted = 0",
            (conversation_id,),
        )
        messages_marked = cur.rowcount or 0
        await cur.close()

        cur = await self._c.execute(
            "DELETE FROM summaries WHERE conversation_id = ?",
            (conversation_id,),
        )
        summaries_deleted = cur.rowcount or 0
        await cur.close()

        await self._c.execute(
            "UPDATE conversations "
            "SET claude_cli_session_id = NULL, "
            "    pending_profile_proposal = NULL, "
            "    pending_tool_confirmation = NULL, "
            "    pending_schwab_reauth = NULL "
            "WHERE id = ?",
            (conversation_id,),
        )
        await self._c.commit()

        return ClearResult(
            messages_marked=int(messages_marked),
            summaries_deleted=int(summaries_deleted),
            session_reset=prior_session is not None,
        )

    # --- Runtime config --------------------------------------------------

    async def get_runtime_config(self, key: str) -> str | None:
        """Return the value for `key`, or None if unset."""
        cur = await self._c.execute(
            "SELECT value FROM runtime_config WHERE key = ?",
            (key,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return row[0]

    async def set_runtime_config(self, key: str, value: str) -> None:
        """Upsert `(key, value)`. `updated_at` advances on overwrite."""
        await self._c.execute(
            """
            INSERT INTO runtime_config (key, value, updated_at)
            VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            ON CONFLICT (key) DO UPDATE
              SET value = excluded.value,
                  updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (key, value),
        )
        await self._c.commit()

    # --- Connected MCPs (v=7, mcp_connector) -----------------------------

    async def list_connected_mcps(self) -> list[dict[str, Any]]:
        """Return every persisted outbound MCP, ordered by name. Each
        row: `{name, transport, url, headers}` (headers is a parsed dict
        or None)."""
        cur = await self._c.execute(
            "SELECT name, transport, url, headers_json FROM connected_mcps ORDER BY name"
        )
        rows = await cur.fetchall()
        await cur.close()
        out: list[dict[str, Any]] = []
        for row in rows:
            headers = None
            if row["headers_json"]:
                try:
                    headers = json.loads(row["headers_json"])
                except (TypeError, ValueError):
                    headers = None
            out.append(
                {
                    "name": row["name"],
                    "transport": row["transport"],
                    "url": row["url"],
                    "headers": headers,
                }
            )
        return out

    async def save_connected_mcp(
        self,
        name: str,
        transport: str,
        url: str,
        headers: dict[str, str] | None,
    ) -> None:
        """Upsert a connected MCP row. `headers` is JSON-encoded if non-empty."""
        headers_json = json.dumps(headers) if headers else None
        await self._c.execute(
            """
            INSERT INTO connected_mcps (name, transport, url, headers_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (name) DO UPDATE
              SET transport = excluded.transport,
                  url = excluded.url,
                  headers_json = excluded.headers_json
            """,
            (name, transport, url, headers_json),
        )
        await self._c.commit()

    async def delete_connected_mcp(self, name: str) -> bool:
        """Delete the row by name. Returns True if a row was deleted."""
        cur = await self._c.execute(
            "DELETE FROM connected_mcps WHERE name = ?",
            (name,),
        )
        deleted = cur.rowcount or 0
        await cur.close()
        await self._c.commit()
        return deleted > 0

    # --- Scheduled runs (FC-7, UoW-5) ------------------------------------

    @staticmethod
    def _dt_to_iso(dt: datetime | None) -> str | None:
        if dt is None:
            return None
        return dt.isoformat()

    @classmethod
    def _hydrate_scheduled_run(cls, row: aiosqlite.Row) -> ScheduledRunRow:
        def parse(s: str | None) -> datetime | None:
            if s is None:
                return None
            return cls._parse_dt(s)

        return ScheduledRunRow(
            id=row["id"],
            skill=row["skill"],
            function=row["function"],
            run_name=row["run_name"],
            scheduled_for=parse(row["scheduled_for"]),
            started_at=parse(row["started_at"]),
            finished_at=parse(row["finished_at"]),
            status=cast(RunStatus, row["status"]),
            result_json=row["result_json"],
            error_text=row["error_text"],
        )

    async def record_run_started(
        self,
        *,
        skill: str,
        function: str,
        run_name: str,
        scheduled_for: datetime | None,
        started_at: datetime,
    ) -> int:
        """UoW-5: insert a row in `running` (started, no finished_at yet).

        Status is provisionally `ok` so the CHECK constraint is satisfied;
        `record_run_finished` mutates it to the terminal status. We rely
        on `started_at IS NOT NULL AND finished_at IS NULL` as the
        in-progress predicate (test_max_instances_one).
        """
        try:
            cur = await self._c.execute(
                """
                INSERT INTO scheduled_runs (
                    skill, function, run_name, scheduled_for,
                    started_at, finished_at, status, result_json, error_text
                ) VALUES (?, ?, ?, ?, ?, NULL, 'ok', NULL, NULL)
                """,
                (
                    skill,
                    function,
                    run_name,
                    self._dt_to_iso(scheduled_for),
                    self._dt_to_iso(started_at),
                ),
            )
            await self._c.commit()
        except aiosqlite.IntegrityError as e:
            await self._c.rollback()
            raise StoreError(f"record_run_started integrity: {e}") from e

        new_id = cur.lastrowid
        await cur.close()
        if new_id is None:
            raise StoreError("INSERT did not return lastrowid")
        return new_id

    async def record_run_finished(
        self,
        run_id: int,
        *,
        finished_at: datetime,
        status: RunStatus,
        result_json: str | None = None,
        error_text: str | None = None,
    ) -> None:
        """UoW-5: mutate the running row to its terminal state."""
        try:
            await self._c.execute(
                """
                UPDATE scheduled_runs
                SET finished_at = ?, status = ?, result_json = ?, error_text = ?
                WHERE id = ?
                """,
                (
                    self._dt_to_iso(finished_at),
                    status,
                    result_json,
                    error_text,
                    run_id,
                ),
            )
            await self._c.commit()
        except aiosqlite.IntegrityError as e:
            await self._c.rollback()
            raise StoreError(f"record_run_finished integrity: {e}") from e

    async def record_run_missed(
        self,
        *,
        skill: str,
        function: str,
        run_name: str,
        scheduled_for: datetime,
    ) -> None:
        """UoW-5: write a standalone `missed` row (no started/finished)."""
        try:
            await self._c.execute(
                """
                INSERT INTO scheduled_runs (
                    skill, function, run_name, scheduled_for,
                    started_at, finished_at, status, result_json, error_text
                ) VALUES (?, ?, ?, ?, NULL, NULL, 'missed', NULL, NULL)
                """,
                (
                    skill,
                    function,
                    run_name,
                    self._dt_to_iso(scheduled_for),
                ),
            )
            await self._c.commit()
        except aiosqlite.IntegrityError as e:
            await self._c.rollback()
            raise StoreError(f"record_run_missed integrity: {e}") from e

    async def list_runs(
        self,
        skill: str,
        *,
        limit: int = 20,
    ) -> list[ScheduledRunRow]:
        """Most-recent-first window of scheduled runs for `skill`.

        We order by `id DESC` because (a) ids are monotonic, and (b)
        `started_at` is NULL for missed rows — so a started_at-only sort
        would push missed rows in unpredictable ways. Tests rely on the
        insertion order matching the most-recent-first guarantee.
        """
        cur = await self._c.execute(
            """
            SELECT * FROM scheduled_runs
            WHERE skill = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (skill, limit),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [self._hydrate_scheduled_run(r) for r in rows]

    async def set_job_enabled(
        self,
        skill: str,
        run_name: str,
        enabled: bool,
    ) -> None:
        """Persist enable/disable state in `scheduled_jobs_state` (FC-6)."""
        try:
            await self._c.execute(
                """
                INSERT INTO scheduled_jobs_state (skill, run_name, enabled)
                VALUES (?, ?, ?)
                ON CONFLICT (skill, run_name) DO UPDATE
                  SET enabled = excluded.enabled
                """,
                (skill, run_name, 1 if enabled else 0),
            )
            await self._c.commit()
        except aiosqlite.IntegrityError as e:
            await self._c.rollback()
            raise StoreError(f"set_job_enabled integrity: {e}") from e

    async def is_job_enabled(self, skill: str, run_name: str) -> bool:
        """Return persisted enabled state. Defaults to True if no row."""
        cur = await self._c.execute(
            "SELECT enabled FROM scheduled_jobs_state WHERE skill = ? AND run_name = ?",
            (skill, run_name),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return True
        return bool(row[0])
