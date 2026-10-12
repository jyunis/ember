"""Store repository functions for `scheduled_runs` (FC-7, UoW-5).

The implementer extends `Store` with four new repository methods:
- `record_run_started(skill, function, run_name, scheduled_for, started_at) -> int`
- `record_run_finished(run_id, finished_at, status, result_json, error_text) -> None`
- `record_run_missed(skill, function, run_name, scheduled_for) -> None`
- `list_runs(skill, limit) -> list[ScheduledRunRow]`

Per FC-7: "all mutations go through repository functions."

These tests run end-to-end against a freshly-opened `Store` (so the
schema is real). They're "unit" in the sense that they exercise a
single layer's surface, even though they touch SQLite.

Spec ambiguity flagged in report:
- The argument naming for the Store-level methods is the only place
  the implementer might deviate. The Protocol on `RunRecorder` uses
  the same kw-only names; we mirror them on the Store. If the
  implementer chooses different positional/keyword args, this test
  is the place that surfaces the divergence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ember.scheduler.base import ScheduledRunRow
from ember.store.repo import Store


@pytest.fixture
async def store(tmp_path: Path):
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


# --- record_run_started --------------------------------------------------


class TestRecordRunStarted:
    async def test_returns_positive_int_id(self, store: Store) -> None:
        rid = await store.record_run_started(
            skill="demo",
            function="tick",
            run_name="tick",
            scheduled_for=None,
            started_at=datetime(2026, 4, 27, 5, 0, 1, tzinfo=timezone.utc),
        )
        assert isinstance(rid, int) and rid > 0

    async def test_two_starts_distinct_ids(self, store: Store) -> None:
        a = await store.record_run_started(
            skill="demo",
            function="tick",
            run_name="tick",
            scheduled_for=None,
            started_at=datetime(2026, 4, 27, 5, 0, 1, tzinfo=timezone.utc),
        )
        b = await store.record_run_started(
            skill="demo",
            function="tick",
            run_name="tick",
            scheduled_for=None,
            started_at=datetime(2026, 4, 27, 6, 0, 1, tzinfo=timezone.utc),
        )
        assert a != b


# --- record_run_finished -------------------------------------------------


class TestRecordRunFinished:
    async def test_finishing_mutates_existing_row(self, store: Store) -> None:
        started = datetime(2026, 4, 27, 5, 0, 1, tzinfo=timezone.utc)
        rid = await store.record_run_started(
            skill="demo",
            function="tick",
            run_name="tick",
            scheduled_for=None,
            started_at=started,
        )
        await store.record_run_finished(
            rid,
            finished_at=started + timedelta(seconds=2),
            status="ok",
            result_json='{"n": 1}',
            error_text=None,
        )

        runs = await store.list_runs("demo", limit=10)
        assert len(runs) == 1
        row = runs[0]
        assert isinstance(row, ScheduledRunRow)
        assert row.id == rid
        assert row.status == "ok"
        assert row.result_json == '{"n": 1}'
        assert row.error_text is None
        assert row.finished_at is not None

    async def test_finishing_with_error(self, store: Store) -> None:
        started = datetime(2026, 4, 27, 5, 0, 1, tzinfo=timezone.utc)
        rid = await store.record_run_started(
            skill="demo",
            function="tick",
            run_name="tick",
            scheduled_for=None,
            started_at=started,
        )
        await store.record_run_finished(
            rid,
            finished_at=started + timedelta(seconds=2),
            status="error",
            result_json=None,
            error_text="Traceback ... ValueError: nope",
        )
        runs = await store.list_runs("demo", limit=10)
        assert runs[0].status == "error"
        assert runs[0].error_text is not None
        assert "ValueError" in runs[0].error_text


# --- record_run_missed ---------------------------------------------------


class TestRecordRunMissed:
    async def test_missed_writes_standalone_row(self, store: Store) -> None:
        scheduled_for = datetime(2026, 4, 27, 5, 0, tzinfo=timezone.utc)
        await store.record_run_missed(
            skill="demo",
            function="tick",
            run_name="tick",
            scheduled_for=scheduled_for,
        )
        runs = await store.list_runs("demo", limit=10)
        assert len(runs) == 1
        row = runs[0]
        assert row.status == "missed"
        assert row.started_at is None
        # `scheduled_for` is set; the spec is that missed rows carry
        # the schedule timestamp so /runs renders something useful.
        assert row.scheduled_for is not None


# --- list_runs ordering & filtering --------------------------------------


class TestListRuns:
    async def test_orders_most_recent_first(self, store: Store) -> None:
        base = datetime(2026, 4, 27, 5, 0, tzinfo=timezone.utc)
        for i in range(3):
            rid = await store.record_run_started(
                skill="demo",
                function=f"f{i}",
                run_name=f"r{i}",
                scheduled_for=None,
                started_at=base + timedelta(minutes=i),
            )
            await store.record_run_finished(
                rid,
                finished_at=base + timedelta(minutes=i, seconds=1),
                status="ok",
                result_json=None,
                error_text=None,
            )

        runs = await store.list_runs("demo", limit=10)
        assert [r.run_name for r in runs] == ["r2", "r1", "r0"]

    async def test_respects_limit(self, store: Store) -> None:
        base = datetime(2026, 4, 27, 5, 0, tzinfo=timezone.utc)
        for i in range(5):
            rid = await store.record_run_started(
                skill="demo",
                function="f",
                run_name=f"r{i}",
                scheduled_for=None,
                started_at=base + timedelta(seconds=i),
            )
            await store.record_run_finished(
                rid,
                finished_at=base + timedelta(seconds=i + 1),
                status="ok",
                result_json=None,
                error_text=None,
            )
        runs = await store.list_runs("demo", limit=2)
        assert len(runs) == 2

    async def test_filters_by_skill(self, store: Store) -> None:
        base = datetime(2026, 4, 27, 5, 0, tzinfo=timezone.utc)
        # Two skills, one row each.
        rid_a = await store.record_run_started(
            skill="alpha",
            function="f",
            run_name="x",
            scheduled_for=None,
            started_at=base,
        )
        await store.record_run_finished(
            rid_a,
            finished_at=base + timedelta(seconds=1),
            status="ok",
            result_json=None,
            error_text=None,
        )
        rid_b = await store.record_run_started(
            skill="beta",
            function="f",
            run_name="y",
            scheduled_for=None,
            started_at=base + timedelta(seconds=10),
        )
        await store.record_run_finished(
            rid_b,
            finished_at=base + timedelta(seconds=11),
            status="ok",
            result_json=None,
            error_text=None,
        )

        only_alpha = await store.list_runs("alpha", limit=10)
        assert len(only_alpha) == 1
        assert only_alpha[0].skill == "alpha"
