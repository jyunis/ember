"""`StoreLearningContext` — concrete `LearningContext` (v12, FC-22).

The narrow store façade handed to scheduled skills as `ctx.learning`. Every
method delegates to a `Store` repository method (FC-7: no raw SQL outside
`store.py`); this class adds no query logic of its own, only the scoping that
keeps `ctx.learning` from being a full `Store` handle.

Split from `skills/context.py` (where it's constructed) so the surface stays
readable in isolation, mirroring `recorder.py` / `notifier.py`.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ember.scheduler.base import ScheduledRunRow, ToolErrorRow
    from ember.store.repo import Conversation, StoredMessage, Store
    from ember.store.types import EvalRun, Feedback, Lesson, Proposal


class StoreLearningContext:
    """`LearningContext` backed by the process `Store`."""

    def __init__(self, store: "Store") -> None:
        self._store = store

    async def active_conversations(self, since: datetime) -> list["Conversation"]:
        return await self._store.conversations_active_since(since)

    async def user_messages(
        self, conversation_id: int, since: datetime, until: datetime
    ) -> list["StoredMessage"]:
        return await self._store.user_messages_in_window(conversation_id, since, until)

    async def feedback(self, since: datetime, until: datetime) -> list["Feedback"]:
        return await self._store.feedback_in_window(since, until)

    async def failed_runs(self, since: datetime, until: datetime) -> list["ScheduledRunRow"]:
        return await self._store.failed_runs_in_window(since, until)

    async def tool_errors(self, since: datetime, until: datetime) -> list["ToolErrorRow"]:
        return await self._store.tool_errors_in_window(since, until)

    async def record_proposal(
        self,
        *,
        kind: str,
        summary: str,
        payload: Any,
        evidence: dict[str, list[int]],
    ) -> tuple[int, bool]:
        return await self._store.record_proposal(
            kind=kind, summary=summary, payload=payload, evidence=evidence
        )

    async def pending_proposals(self) -> list["Proposal"]:
        return await self._store.list_proposals(status="pending")

    async def expire_stale_proposals(self, *, older_than: datetime, now: datetime) -> list[int]:
        return await self._store.expire_proposals(older_than=older_than, now=now)

    # --- v13 closure: applied-change measurement (B) + pruning (C) --------

    async def applied_proposals_since(self, since: datetime) -> list["Proposal"]:
        return await self._store.applied_proposals_since(since)

    async def eval_run(self, run_id: int) -> "EvalRun | None":
        return await self._store.get_eval_run(run_id)

    async def previous_eval_run(self, run_id: int, selector: str | None) -> "EvalRun | None":
        return await self._store.previous_eval_run(run_id, selector=selector)

    async def reinforce_lesson(self, text: str, *, now: datetime) -> bool:
        from ember.identity.lessons import LessonsWriter

        matched = await self._store.reinforce_lesson(LessonsWriter.dedup_key(text), now=now)
        return matched is not None

    async def stale_lessons(self, *, older_than: datetime) -> list["Lesson"]:
        return await self._store.stale_lessons(older_than=older_than)
