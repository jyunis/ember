"""Scheduler interfaces (FC-6, UoW-5, TLC-13).

The scheduler service drives APScheduler from a `SkillSource`, wraps
each fire through the run-recorder and run-notifier, and exposes
`run_now` for `/run`. This file defines the surface only — concrete
implementations live in sibling modules.

Splitting the surface from the implementation lets tester agents read
this file (and the contracts doc) without seeing the
APScheduler/SQLAlchemy plumbing, per the implementer/tester
separation protocol.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ember.providers.routing import ProviderRouter
    from ember.store.repo import Conversation, StoredMessage
    from ember.store.types import EvalRun, Feedback, Lesson, Proposal
    from ember.tools.registry import Registry


# Status values stored in `scheduled_runs.status`. The schema enforces
# this set via CHECK constraint; keeping the literal here lets callers
# narrow on it.
RunStatus = Literal["ok", "error", "timeout", "missed"]


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """One scheduled run on an imperative skill (architecture § Scheduler).

    `cron` is a 5-field crontab string, interpreted with *crontab*
    semantics throughout — day-of-week counts Sunday as 0 (7 also works),
    so `1-5` is Mon-Fri. `SchedulerService` translates the field into
    APScheduler's Monday-first numbering; do not pre-compensate for the
    difference in a skill's spec. `function` names a coroutine on the
    owning skill module. `name` distinguishes runs within a skill (a
    skill may schedule the same function under different names).

    Defaults follow the architecture doc § Scheduler.
    """

    cron: str
    function: str
    name: str
    timeout_s: int = 300
    misfire_grace_s: int = 3600
    coalesce: bool = True
    timezone: str = "UTC"


@dataclass(frozen=True, slots=True)
class ScheduledFunction:
    """A schedulable unit produced by `SkillSource.list_schedules()`.

    Pairs the schedule with the bound coroutine and the owning skill's
    name. The scheduler service wraps `fn` in `runner.run_wrapped`; it
    never imports the skill module directly.
    """

    skill: str
    spec: ScheduleSpec
    fn: Callable[["RunContext"], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ToolErrorRow:
    """One tool call that returned `status='error'` inside an agent turn —
    the retrospective's silent-failure evidence source (doc #36 Decisions
    #26). Hydrated from `messages.tool_calls_json` on assistant rows. The
    assistant row itself is never evidence (TLC-20); `user_message_id` is
    the user turn that preceded it in the same conversation and stands in
    as the evidence pointer (None when there is no earlier user turn)."""

    message_id: int
    conversation_id: int
    created_at: datetime
    tool: str
    args: dict[str, Any]
    user_message_id: int | None


@dataclass(frozen=True, slots=True)
class ScheduledRunRow:
    """A row in `scheduled_runs`. Hydrated by the recorder on read."""

    id: int
    skill: str
    function: str
    run_name: str
    scheduled_for: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    status: RunStatus
    result_json: str | None
    error_text: str | None


@runtime_checkable
class LearningContext(Protocol):
    """Narrow, contract-enforcing view of the conversation store for the
    learning loop (v12, UoW-22 / FC-22 / TLC-20).

    The retrospective is the first scheduled skill needing DB access. Rather
    than hand skills the raw `Store` (least-privilege regression — a skill
    could `clear_conversation`), `RunContext.learning` exposes ONLY the
    evidence reads the retrospective needs plus a single gated writer.

    Provenance (TLC-20) is enforced on both sides: the read methods surface
    only evidence-eligible rows (user messages, live feedback, failed runs),
    and `record_proposal` re-validates every evidence id at insert, so a
    buggy caller still cannot persist a proposal citing agent-authored rows.
    LLM output remains data throughout (TLC-13): the skill validates the
    extraction schema, then hands typed rows here — this API never sees free
    text as control flow.
    """

    async def active_conversations(self, since: datetime) -> list["Conversation"]:
        """Conversations with `last_active >= since` (the run window)."""
        ...

    async def user_messages(
        self, conversation_id: int, since: datetime, until: datetime
    ) -> list["StoredMessage"]:
        """User-authored messages for a conversation in `[since, until)` —
        the only transcript content eligible for extraction (TLC-20)."""
        ...

    async def feedback(self, since: datetime, until: datetime) -> list["Feedback"]:
        """Live (non-retracted) feedback rows in `[since, until)`."""
        ...

    async def failed_runs(self, since: datetime, until: datetime) -> list["ScheduledRunRow"]:
        """Non-ok scheduled runs across all skills in `[since, until)`."""
        ...

    async def tool_errors(self, since: datetime, until: datetime) -> list["ToolErrorRow"]:
        """Tool calls that returned `status='error'` in `[since, until)`,
        across all conversations, each paired with the preceding user
        message id (the only evidence-eligible pointer for it, TLC-20)."""
        ...

    async def record_proposal(
        self,
        *,
        kind: str,
        summary: str,
        payload: Any,
        evidence: dict[str, list[int]],
    ) -> tuple[int, bool]:
        """Insert a pending proposal or reinforce an open duplicate.

        Returns `(proposal_id, reinforced)`. Raises if any evidence id is
        not evidence-eligible (TLC-20). Never writes any status but
        `pending` (v12 accumulate-only, TLC-21a)."""
        ...

    async def pending_proposals(self) -> list["Proposal"]:
        """All `status='pending'` proposals, newest-first — the queue the
        weekly digest renders (UoW-23)."""
        ...

    async def expire_stale_proposals(self, *, older_than: datetime, now: datetime) -> list[int]:
        """Bulk-expire pending proposals created before `older_than`
        (`pending → expired`, resolved_at=now), returning the expired ids.
        Silent (TLC-21d). The digest calls this before rendering so the queue
        can't rot; `older_than`/`now` are injected for clock-testability."""
        ...

    # --- v13 closure: applied-change measurement (B) + pruning (C) --------

    async def applied_proposals_since(self, since: datetime) -> list["Proposal"]:
        """Proposals `status='applied'` with `resolved_at >= since` — the
        trailing-window changes whose eval delta the digest reports (UoW-24)."""
        ...

    async def eval_run(self, run_id: int) -> "EvalRun | None":
        """One `eval_runs` row by id (the run an applied proposal is linked to)."""
        ...

    async def previous_eval_run(self, run_id: int, selector: str | None) -> "EvalRun | None":
        """The most recent finished run BEFORE `run_id` with the same selector
        — the before/after baseline for a delta (v11 store method)."""
        ...

    async def reinforce_lesson(self, text: str, *, now: datetime) -> bool:
        """Bump the live lesson matching `text`'s normalized dedup key
        (UoW-25 / seam ①). Returns True if a live lesson was reinforced (so the
        caller skips proposing a duplicate), False if none matched."""
        ...

    async def stale_lessons(self, *, older_than: datetime) -> list["Lesson"]:
        """Live lessons not reinforced since `older_than` — the weekly digest's
        prune candidates (UoW-25 / C)."""
        ...


@dataclass(slots=True)
class RunContext:
    """`ctx` passed to scheduled functions and `pre_turn`/`post_turn`
    hooks (deferred to v7).

    Constructed fresh per fire so cross-run state never leaks. Skills
    use `ctx.tools` for tool registry access, `ctx.router` to resolve
    a provider for bounded LLM calls (TLC-13: structure is fixed,
    LLM output is validated), and `ctx.notify` to post status to the
    scheduler channel.

    `learning` is the v12 addition (LearningContext): the narrow store view
    the retrospective uses for evidence reads + proposal writes. It is
    `None` on the minimal/test wiring path (no store); the standard
    production ctx always carries it, uniform across skills (FC-22).
    """

    skill: str
    run_name: str
    scheduled_for: datetime | None
    profile: str
    soul: str
    tools: "Registry"
    router: "ProviderRouter"
    notify: Callable[[str], Awaitable[None]]
    now: Callable[[], datetime]
    learning: "LearningContext | None" = None


@runtime_checkable
class RunRecorder(Protocol):
    """UoW-5 audit boundary.

    `record_started` returns the new row id; `record_finished` mutates
    that row to its terminal state. `record_missed` writes a row in a
    single shot (no started/finished — APScheduler skipped the fire
    outside the misfire grace window).
    """

    async def record_started(
        self,
        *,
        skill: str,
        function: str,
        run_name: str,
        scheduled_for: datetime | None,
        started_at: datetime,
    ) -> int: ...

    async def record_finished(
        self,
        run_id: int,
        *,
        finished_at: datetime,
        status: RunStatus,
        result_json: str | None = None,
        error_text: str | None = None,
    ) -> None: ...

    async def record_missed(
        self,
        *,
        skill: str,
        function: str,
        run_name: str,
        scheduled_for: datetime,
    ) -> None: ...

    async def list_runs(
        self,
        skill: str,
        *,
        limit: int = 20,
    ) -> list[ScheduledRunRow]: ...


@runtime_checkable
class RunNotifier(Protocol):
    """Posts non-ok terminal states (TLC-7) and other operational
    notifications to the configured scheduler channel."""

    async def notify(self, message: str) -> None: ...


@runtime_checkable
class SkillSource(Protocol):
    """The scheduler's view of loaded skills.

    Implemented by `skills.registry.SkillRegistry`. Decoupling lets the
    scheduler service stay agnostic to how skills are loaded (file
    watcher, in-memory registration in tests, future declarative
    sources).
    """

    def list_schedules(self) -> list[ScheduledFunction]:
        """Snapshot of all currently-scheduled functions across all
        loaded imperative skills. Order-stable (TLC-12 spirit) so the
        service's diffing on refresh is deterministic."""
        ...

    def on_change(self, callback: Callable[[], None]) -> None:
        """Register a callback fired when the underlying skill set
        changes (skill added/removed/replaced). The scheduler service
        uses this to trigger `refresh()`."""
        ...


@runtime_checkable
class SchedulerService(Protocol):
    """Scheduler service surface (FC-6).

    `start` boots APScheduler and registers all current schedules.
    `refresh` reconciles current state against `SkillSource` (used
    on hot-reload). `run_now` invokes the same wrapped path used by
    cron fires (FC-6: there is no "manual-only" path).
    """

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def refresh(self) -> None: ...

    async def run_now(self, skill: str, function: str, run_name: str) -> None: ...

    async def set_enabled(self, skill: str, run_name: str, enabled: bool) -> None: ...

    def list_jobs(self) -> list["JobSnapshot"]: ...


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    """User-visible row for `/schedule list`."""

    skill: str
    run_name: str
    function: str
    cron: str
    next_fire_time: datetime | None
    enabled: bool
