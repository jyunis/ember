-- Ember schema.
--
-- Single source of truth for conversation state (TLC-11). Versioned
-- per FC-7; the store refuses to start against an unrecognized version.
--
-- v=2: messages.failover (rate-limit failover marker).
-- v=3: scheduled_runs (UoW-5 audit table) + scheduled_jobs_state.
-- v=4: messages.compacted, conversations.pending_profile_proposal,
--      summaries (UoW-12), pinned, runtime_config.
-- v=5: conversations.pending_tool_confirmation (TLC-3 enforcement).
-- v=6: messages.subagent_skill, messages.parent_message_id (FC-17 cost
--      attribution: TLC-16 §6).
-- v=7: connected_mcps (mcp_connector tools — runtime-mounted outbound MCPs
--      survive container restarts).
-- v=8: connected_mcps.transport widened to accept 'sse' (legacy
--      two-endpoint flow) in addition to 'streamableHttp'.
-- v=9: conversations.pending_schwab_reauth (BR-FC-11 — two-message
--      Schwab OAuth re-auth handshake driven by /schwab-reauth).
-- v=10: feedback (learning loop Layer 1 — explicit reaction signal,
--      UoW-20 / FC-20) + channel_message_map (native channel message id
--      → messages.id, so a reaction on an agent reply resolves to its
--      row; agent-authored final replies only).
-- v=11: eval_runs + eval_case_results (learning loop Layer 2 — eval
--      harness, UoW-21 / FC-21). NOTE: doc #36 originally labelled
--      migration v11 as `proposals`; migrations are sequential and the
--      eval harness (milestone v11) ships before the retrospective
--      (milestone v12), so eval tables take v11 and `proposals` moves to
--      v12 (doc #36 Decisions log). Results are keyed to a config
--      fingerprint so a change to identity/skills/route can be compared
--      before/after.
-- v=12: proposals (learning loop Layer 3 — the nightly retrospective,
--      UoW-22 / FC-22 / FC-23). v12 is accumulate-only: proposals are
--      inserted `pending` and never applied (application lands in v12.5).
--      `payload_hash` + `reinforced_count` extend doc #36's schema with the
--      operational columns its own UoW-22 dedup clause requires (Decisions
--      log). Evidence ids are validated against TLC-20 at insert.
-- v=14: conversations.archived_at (UoW-26 — channel-side thread deletion
--      archives, never deletes: TLC-23) + messages_fts external-content
--      FTS5 index over user/assistant message content with sync triggers
--      (UoW-27 / FC-27 history search).
-- v=15: messages.cache_creation_tokens + conversations.last_context_tokens
--      (doc #56, FC-28 / TLC-24 honest context accounting — the session-
--      rotation trigger reads the measured context, never a token SUM).

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS conversations (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel                         TEXT NOT NULL,                   -- "discord"
    channel_conversation_key        TEXT NOT NULL,                   -- e.g. forum-thread ID
    name                            TEXT,
    created_at                      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_active                     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    provider_route                  TEXT NOT NULL DEFAULT 'claude_cli',
    token_budget                    INTEGER NOT NULL DEFAULT 150000,
    claude_cli_session_id           TEXT,
    auto_failover_on_rate_limit     INTEGER NOT NULL DEFAULT 0,      -- bool
    active_skill                    TEXT,
    pending_profile_proposal        TEXT,                            -- v=4: nullable JSON (UoW-8)
    pending_tool_confirmation       TEXT,                            -- v=5: nullable JSON (TLC-3)
    pending_schwab_reauth           TEXT,                            -- v=9: nullable JSON (BR-FC-11)
    archived_at                     TEXT,                            -- v=14: NULL = live; stamped on channel-side thread delete (UoW-26)
    last_context_tokens             INTEGER,                         -- v=15: measured context size of the latest claude_cli turn (TLC-24); NULL until measured / after rotation
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
    cost_usd            REAL,                                         -- NULL when subscription
    subscription_turn   INTEGER NOT NULL DEFAULT 0,                   -- bool: 1 if billed via subscription
    failover            INTEGER NOT NULL DEFAULT 0,                   -- bool: 1 if rate-limit failover
    cache_creation_tokens INTEGER,                                    -- v=15: cache-write input tokens (FC-28 / TLC-24)
    tool_calls_json     TEXT,                                         -- JSON-encoded list of ToolCalls; nullable
    compacted           INTEGER NOT NULL DEFAULT 0,                   -- v=4: bool, set when collapsed into a summary (UoW-12)
    subagent_skill      TEXT,                                         -- v=6: skill name when this row was produced inside a sub-agent (FC-17)
    parent_message_id   INTEGER,                                      -- v=6: parent's message id when this row was produced inside a sub-agent (FC-17)
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

-- v=4: per-conversation compaction summaries (UoW-12). One row per
-- compaction event. `start_message_id`..`end_message_id` is the
-- inclusive range of messages.id collapsed into `summary_text`.
-- Listed start_message_id ASC for byte-stable prompt assembly (TLC-12).
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

-- v=4: pinned context entries. Survive trim/compact/clear.
CREATE TABLE IF NOT EXISTS pinned (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id     INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    content             TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_pinned_conversation
    ON pinned (conversation_id, id);

-- v=4: process-wide runtime config (e.g. cheap-tier model override).
CREATE TABLE IF NOT EXISTS runtime_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- v=7: outbound MCPs mounted at runtime via the mcp_connector tools.
-- Boot replays this table to reattach each client.
CREATE TABLE IF NOT EXISTS connected_mcps (
    name         TEXT PRIMARY KEY,
    transport    TEXT NOT NULL CHECK (transport IN ('streamableHttp', 'sse')),
    url          TEXT NOT NULL,
    headers_json TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- v=10: explicit feedback signal (Discord reactions on agent messages).
-- Soft delete via `removed_at` (TLC-8): retracted reactions stay for
-- audit but the retrospective filters them out. `sentiment` is +1/-1/NULL.
CREATE TABLE IF NOT EXISTS feedback (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id     INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id          INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    emoji               TEXT NOT NULL,
    sentiment           INTEGER,                                      -- +1 / -1 / NULL
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    removed_at          TEXT                                          -- NULL while live
);

CREATE INDEX IF NOT EXISTS idx_feedback_message
    ON feedback (message_id);

-- One live (removed_at IS NULL) row per (message, emoji). Removed rows
-- are exempt so soft-deleted history can coexist with a later re-add.
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_live_unique
    ON feedback (message_id, emoji) WHERE removed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_feedback_created
    ON feedback (created_at) WHERE removed_at IS NULL;

-- v=10: native channel message id → messages.id. Written for
-- agent-authored final replies only (one messages row can fan out to
-- several native ids when Discord chunks a long reply). A reaction whose
-- native id is absent here resolves to nothing → ignored (UoW-20).
CREATE TABLE IF NOT EXISTS channel_message_map (
    channel             TEXT NOT NULL,
    channel_message_id  TEXT NOT NULL,
    message_id          INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (channel, channel_message_id)
);

CREATE INDEX IF NOT EXISTS idx_channel_message_map_message
    ON channel_message_map (message_id);

-- v=11: eval-harness result tables (learning loop Layer 2, UoW-21 / FC-21).
-- `eval_runs` = one row per `/eval run` invocation. `finished_at IS NULL`
-- while running / after a crash (partial per-case rows stay queryable). The
-- fingerprint component columns are stored alongside the composite hash so a
-- report can name exactly what differed between two runs.
CREATE TABLE IF NOT EXISTS eval_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at     TEXT,                                         -- NULL while running / crashed
    selector        TEXT,                                         -- [case-id|kind] filter, NULL = all
    fingerprint     TEXT NOT NULL,                                -- composite config fingerprint
    identity_sha    TEXT,                                         -- component: identity repo HEAD sha
    skills_hash     TEXT,                                         -- component: sorted skill-file-set hash
    provider_route  TEXT,                                         -- component: route the run executed on
    image_version   TEXT,                                         -- component: EMBER_IMAGE_VERSION
    total_cases     INTEGER NOT NULL DEFAULT 0,
    passed          INTEGER NOT NULL DEFAULT 0,
    failed          INTEGER NOT NULL DEFAULT 0,
    errored         INTEGER NOT NULL DEFAULT 0,
    skipped         INTEGER NOT NULL DEFAULT 0                     -- parse-skipped cases
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_started
    ON eval_runs (started_at DESC);

-- One row per case×trial (UoW-21). `status` ∈ {pass, fail, error}; a judge
-- schema violation is `error`, never coerced (FC-21). `score`/`judge_reason`
-- are tone-only. Usage columns record per-trial provider cost (TLC-5).
CREATE TABLE IF NOT EXISTS eval_case_results (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    case_id             TEXT NOT NULL,
    kind                TEXT NOT NULL,                            -- routing | tone | tool_flow
    trial               INTEGER NOT NULL,                         -- 1-based trial index
    status              TEXT NOT NULL CHECK (status IN ('pass', 'fail', 'error')),
    score               INTEGER,                                  -- tone judge score 1-5, else NULL
    judge_reason        TEXT,                                     -- tone judge reason, else NULL
    detail              TEXT,                                     -- expected-vs-got / error text
    tokens_in           INTEGER,
    tokens_out          INTEGER,
    cached_tokens       INTEGER,
    cost_usd            REAL,
    subscription_turn   INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_eval_case_results_run
    ON eval_case_results (run_id, case_id, trial);

-- v=12: retrospective proposals (learning loop Layer 3, UoW-22 / FC-23).
-- `kind` routes the payload to a writer path on approval (v12.5). `evidence_json`
-- is the typed provenance bundle {messages,feedback,runs}, validated against
-- TLC-20 at insert. `payload_hash` is the dedup key; a re-derived finding bumps
-- `reinforced_count` on the open row instead of inserting a duplicate. v12 only
-- ever writes status='pending' (accumulate-only, TLC-21a).
CREATE TABLE IF NOT EXISTS proposals (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    kind                 TEXT NOT NULL CHECK (kind IN ('profile', 'skill', 'trigger', 'lesson', 'finding')),
    summary              TEXT NOT NULL,
    payload_json         TEXT NOT NULL,                            -- concrete diff / manifest / lesson text
    evidence_json        TEXT NOT NULL,                            -- {messages:[],feedback:[],runs:[]} (TLC-20)
    payload_hash         TEXT NOT NULL,                            -- (kind, hash) dedup key
    reinforced_count     INTEGER NOT NULL DEFAULT 0,               -- bumped on re-derivation (UoW-22)
    status               TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'approved', 'rejected', 'applied', 'expired')),
    resolved_at          TEXT,                                     -- set on terminal transition (v12.5)
    applied_eval_run_id  INTEGER REFERENCES eval_runs(id)          -- v13 closure link
);

-- Dedup only among open proposals (UoW-22): a rejected/expired one must not
-- block a fresh re-proposal, so uniqueness is partial on status='pending'.
CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_pending_dedup
    ON proposals (kind, payload_hash) WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_proposals_status_created
    ON proposals (status, created_at);

-- v=13: lessons (learning loop Layer 4 — Closure, FC-26 / Decision #23).
-- Lessons stop being flat text in lessons.md and become durable rows so their
-- reinforcement/age can be tracked; lessons.md becomes a deterministic render
-- of the live rows (removed_at IS NULL), ordered by id ASC (TLC-12). `dedup_key`
-- is the normalized-text hash (Decision #24): the nightly reinforces a live row
-- whose key matches a re-derived lesson instead of proposing a duplicate.
-- `last_reinforced_at` = `created_at` at insert; a match bumps it. `removed_at`
-- soft-deletes on prune (audit survives, TLC-8/9).
CREATE TABLE IF NOT EXISTS lessons (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    text                TEXT NOT NULL,
    evidence_json       TEXT NOT NULL,                            -- {messages,feedback,runs} (TLC-20)
    dedup_key           TEXT NOT NULL,                            -- normalized-text hash (seam ①)
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_reinforced_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    reinforced_count    INTEGER NOT NULL DEFAULT 0,
    source_proposal_id  INTEGER REFERENCES proposals(id),
    removed_at          TEXT                                      -- soft-delete on prune (NULL = live)
);

-- At most one LIVE lesson per normalized-text key; a pruned row may share a key
-- with a later re-added one, so uniqueness is partial on removed_at IS NULL.
CREATE UNIQUE INDEX IF NOT EXISTS idx_lessons_live_dedup
    ON lessons (dedup_key) WHERE removed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_lessons_live_reinforced
    ON lessons (last_reinforced_at) WHERE removed_at IS NULL;

-- v=14: history keyword search (UoW-27 / FC-27). External-content FTS5
-- index over `messages.content`, restricted to user/assistant rows —
-- tool/system rows are noise and bloat (doc #54 Decision #5). Kept in
-- sync by the triggers below; no repository function writes
-- `messages_fts` directly (FC-27). The insert/delete triggers carry the
-- SAME role filter (an asymmetric pair corrupts the index).
-- `messages.content` is never UPDATEd in the current codebase
-- (compaction flips `compacted`, not content); the update trigger is
-- defined anyway for safety, in the delete+insert form external-content
-- tables require, each half guarded on its own row's role.
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
INSERT OR IGNORE INTO schema_version (version) VALUES (15);
