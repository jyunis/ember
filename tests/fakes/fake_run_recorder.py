"""Programmable RunRecorder stub for tests (UoW-5).

Captures every record_* call so unit tests for the scheduler runner
can assert the recorder boundary without standing up a real Store.

Implements the `RunRecorder` Protocol from `ember.scheduler.base`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ember.scheduler.base import RunStatus, ScheduledRunRow


@dataclass
class StartedCall:
    skill: str
    function: str
    run_name: str
    scheduled_for: datetime | None
    started_at: datetime


@dataclass
class FinishedCall:
    run_id: int
    finished_at: datetime
    status: RunStatus
    result_json: str | None
    error_text: str | None


@dataclass
class MissedCall:
    skill: str
    function: str
    run_name: str
    scheduled_for: datetime


class FakeRunRecorder:
    """In-memory `RunRecorder` for tests.

    `record_started` returns sequential ids starting at 1. Use
    `started_calls`, `finished_calls`, `missed_calls` to assert
    behavior. `rows_for(skill, limit)` returns canned `ScheduledRunRow`
    objects when callers exercise the `list_runs` path; tests can
    override by populating `runs`.
    """

    def __init__(self) -> None:
        self.started_calls: list[StartedCall] = []
        self.finished_calls: list[FinishedCall] = []
        self.missed_calls: list[MissedCall] = []
        self.runs: list[ScheduledRunRow] = []
        self._next_id: int = 1

    async def record_started(
        self,
        *,
        skill: str,
        function: str,
        run_name: str,
        scheduled_for: datetime | None,
        started_at: datetime,
    ) -> int:
        rid = self._next_id
        self._next_id += 1
        self.started_calls.append(
            StartedCall(
                skill=skill,
                function=function,
                run_name=run_name,
                scheduled_for=scheduled_for,
                started_at=started_at,
            )
        )
        return rid

    async def record_finished(
        self,
        run_id: int,
        *,
        finished_at: datetime,
        status: RunStatus,
        result_json: str | None = None,
        error_text: str | None = None,
    ) -> None:
        self.finished_calls.append(
            FinishedCall(
                run_id=run_id,
                finished_at=finished_at,
                status=status,
                result_json=result_json,
                error_text=error_text,
            )
        )

    async def record_missed(
        self,
        *,
        skill: str,
        function: str,
        run_name: str,
        scheduled_for: datetime,
    ) -> None:
        self.missed_calls.append(
            MissedCall(
                skill=skill,
                function=function,
                run_name=run_name,
                scheduled_for=scheduled_for,
            )
        )

    async def list_runs(
        self,
        skill: str,
        *,
        limit: int = 20,
    ) -> list[ScheduledRunRow]:
        matching = [r for r in self.runs if r.skill == skill]
        return matching[:limit]
