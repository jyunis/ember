"""Shared store-layer dataclasses (interface-only).

These rows are surfaced by repository methods on `Store` (see `repo.py`)
but are split out here so tests and other consumers can import the
shapes without pulling the full repository implementation. Mirrors the
`scheduler/base.py` split: types separate from plumbing.

Added in v=4 alongside the v9 compaction / pin / clear feature set
(architecture doc § "Token tracker + auto-compactor", contracts doc
§ UoW-12).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ember.providers.base import Usage


@dataclass(frozen=True, slots=True)
class Summary:
    """One compaction event row from the `summaries` table.

    `start_message_id` / `end_message_id` reference the inclusive range
    of `messages.id` collapsed by this summary. `summary_text` is the
    cheap-tier-model-produced paragraph (UoW-12). Order summaries by
    `start_message_id` ASC for prompt-cache stability (TLC-12).
    """

    id: int
    conversation_id: int
    start_message_id: int
    end_message_id: int
    summary_text: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Pin:
    """One pinned-context entry. Pins survive trim/compact/clear
    (architecture doc § Conversation store: "pinned ... survives
    trim/compact"). Listed in id-ASC order.
    """

    id: int
    conversation_id: int
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Feedback:
    """One `feedback` row (v=10, learning loop Layer 1 / UoW-20).

    Explicit quality signal from a Discord reaction on an agent-authored
    message. `emoji` is the raw reaction string; `sentiment` is +1 for 👍,
    -1 for 👎, and NULL for everything else (recorded verbatim — the
    retrospective may later learn what other emoji mean, but nothing
    downstream may assume it). `removed_at` is a soft-delete marker set
    when the reaction is removed (TLC-8: audit history survives; the
    retrospective must not learn from retracted signal — filter on
    `removed_at IS NULL`).
    """

    id: int
    conversation_id: int
    message_id: int
    emoji: str
    sentiment: int | None
    created_at: datetime
    removed_at: datetime | None


@dataclass(frozen=True, slots=True)
class EvalRun:
    """One `eval_runs` row (v=11, learning loop Layer 2 / UoW-21).

    One row per `/eval run` invocation. `finished_at IS NULL` marks a run
    that is still executing or crashed mid-flight (UoW-21 atomicity: the
    per-case rows remain queryable). `fingerprint` is the composite config
    fingerprint; the four component columns (`identity_sha`, `skills_hash`,
    `provider_route`, `image_version`) are stored alongside it so a report
    can show exactly what differed between two runs. `selector` is the
    `[case-id|kind]` filter the run was launched with, or NULL for "all".
    """

    id: int
    started_at: datetime
    finished_at: datetime | None
    selector: str | None
    fingerprint: str
    identity_sha: str | None
    skills_hash: str | None
    provider_route: str | None
    image_version: str | None
    total_cases: int
    passed: int
    failed: int
    errored: int
    skipped: int


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    """One `eval_case_results` row (v=11, UoW-21).

    One row per case×trial. `status` is `pass` | `fail` | `error` — a judge
    schema violation surfaces as `error` and is never coerced to pass/fail
    (FC-21). `score` / `judge_reason` are populated only for `tone` cases.
    `detail` is a freeform diagnostic (expected-vs-got, error text). `usage`
    carries the provider cost of this trial's model call(s) (TLC-5); the
    runner records usage here and writes nowhere else (FC-21).
    """

    id: int
    run_id: int
    case_id: str
    kind: str
    trial: int
    status: str
    score: int | None
    judge_reason: str | None
    detail: str | None
    usage: Usage | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Proposal:
    """One `proposals` row (v=12, learning loop Layer 3 / UoW-22).

    A durable improvement the nightly retrospective proposes but never
    applies (v12 is accumulate-only; application lands in v12.5). `kind`
    routes the payload to a writer path on approval:
    `profile | skill | trigger | lesson | finding`. `payload_json` is the
    concrete diff / manifest / lesson text; `evidence_json` is the typed
    provenance bundle `{"messages": [...], "feedback": [...], "runs": [...]}`
    — validated against TLC-20 at insert (only user-authored messages, live
    feedback, and recorded failures are legal; agent output never is).

    `payload_hash` is the dedup key: a re-run that re-derives the same
    finding bumps `reinforced_count` on the existing open row instead of
    inserting a duplicate (UoW-22 idempotency). `status` walks the FC-23
    state machine (`pending → approved → applied`, `pending → rejected`,
    `pending → expired`); v12 only ever writes `pending`.
    `applied_eval_run_id` is the v13 closure link, NULL until then.
    """

    id: int
    created_at: datetime
    kind: str
    summary: str
    payload_json: str
    evidence_json: str
    payload_hash: str
    reinforced_count: int
    status: str
    resolved_at: datetime | None
    applied_eval_run_id: int | None


@dataclass(frozen=True, slots=True)
class Lesson:
    """One `lessons` row (v=13, learning loop Layer 4 / FC-26).

    v13 promotes lessons from flat text in `lessons.md` to durable rows so
    their reinforcement/age can be tracked (Decision #23). The table is the
    source of truth; `lessons.md` is a deterministic render of the live
    (`removed_at IS NULL`) rows, ordered by `id` ASC, so TLC-12 byte-stability
    holds. `dedup_key` is the normalized-text hash (Decision #24 / seam ①):
    the nightly retrospective reinforces a live row whose key matches a
    re-derived lesson instead of proposing a duplicate. `last_reinforced_at`
    starts equal to `created_at`; a match bumps it (+ `reinforced_count`).
    `removed_at` is the soft-delete marker set on prune (audit survives,
    TLC-8/9). `source_proposal_id` links back to the lesson proposal that
    created the row.
    """

    id: int
    text: str
    evidence_json: str
    dedup_key: str
    created_at: datetime
    last_reinforced_at: datetime
    reinforced_count: int
    source_proposal_id: int | None
    removed_at: datetime | None


@dataclass(frozen=True, slots=True)
class HistoryHit:
    """One `history_search` result row (v=14, UoW-27 / FC-27).

    A bm25-ranked FTS5 match over indexed `messages` rows (`user` /
    `assistant` only — Decision #5), joined to its conversation. `archived`
    reflects `conversations.archived_at IS NOT NULL` at query time — hits
    are labeled, never filtered (Decision #7). `snippet` is the FTS5
    `snippet()` output, ellipsized, ~64 tokens.
    """

    message_id: int
    conversation_id: int
    conversation_name: str | None
    archived: bool
    role: str
    created_at: datetime
    snippet: str


@dataclass(frozen=True, slots=True)
class HistoryWindowMessage:
    """One message inside a `HistoryWindow` (v=14, UoW-27 / FC-27).

    ALL roles appear here — tool/system rows are part of the record even
    though they are not FTS-indexed. `content` is truncated at 4000 chars
    with a marker appended.
    """

    id: int
    role: str
    created_at: datetime
    content: str


@dataclass(frozen=True, slots=True)
class HistoryWindow:
    """`history_fetch` result (v=14, UoW-27 / FC-27): conversation meta
    plus an ordered (by message id) window of messages around an anchor —
    `before`/`after` rows on each side, anchor included."""

    conversation_id: int
    conversation_name: str | None
    archived: bool
    created_at: datetime
    last_active: datetime
    messages: tuple[HistoryWindowMessage, ...]


@dataclass(frozen=True, slots=True)
class ClearResult:
    """Outcome counts from `Store.clear_conversation`. The clear
    operation marks all messages compacted=1, deletes summaries rows,
    nulls `claude_cli_session_id`, and clears `pending_profile_proposal`.
    Pins survive."""

    messages_marked: int
    summaries_deleted: int
    session_reset: bool
