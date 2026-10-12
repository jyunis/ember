"""Concrete `RunRecorder` backed by `Store` (UoW-5, FC-7).

Thin adapter — every method delegates to the matching `Store` repo
method. Existing for two reasons:
- Keeps the scheduler service's only knowledge of persistence behind
  the `RunRecorder` Protocol (so tests can substitute a fake).
- Lets the Store stay focused on raw repo functions; status / row
  shaping concerns live in `scheduler.base.ScheduledRunRow`.
"""

from __future__ import annotations

from datetime import datetime

from ember.scheduler.base import RunStatus, ScheduledRunRow
from ember.store.repo import Store


class StoreRunRecorder:
    """`RunRecorder` over `Store`."""

    def __init__(self, store: Store) -> None:
        self._store = store

    async def record_started(
        self,
        *,
        skill: str,
        function: str,
        run_name: str,
        scheduled_for: datetime | None,
        started_at: datetime,
    ) -> int:
        return await self._store.record_run_started(
            skill=skill,
            function=function,
            run_name=run_name,
            scheduled_for=scheduled_for,
            started_at=started_at,
        )

    async def record_finished(
        self,
        run_id: int,
        *,
        finished_at: datetime,
        status: RunStatus,
        result_json: str | None = None,
        error_text: str | None = None,
    ) -> None:
        await self._store.record_run_finished(
            run_id,
            finished_at=finished_at,
            status=status,
            result_json=result_json,
            error_text=error_text,
        )

    async def record_missed(
        self,
        *,
        skill: str,
        function: str,
        run_name: str,
        scheduled_for: datetime,
    ) -> None:
        await self._store.record_run_missed(
            skill=skill,
            function=function,
            run_name=run_name,
            scheduled_for=scheduled_for,
        )

    async def list_runs(
        self,
        skill: str,
        *,
        limit: int = 20,
    ) -> list[ScheduledRunRow]:
        return await self._store.list_runs(skill, limit=limit)
