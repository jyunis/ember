"""Concrete `RunRecorder` (UoW-5).

The implementer ships a `StoreRunRecorder` (under `ember.scheduler.recorder`)
that persists runs to the `scheduled_runs` table introduced in schema v3.

These tests exercise the recorder's four entry points end-to-end against
a freshly-opened Store:
- `record_started` returns a non-zero id.
- `record_finished` mutates the row to its terminal state.
- `record_missed` writes a standalone row with `status=missed`,
  `started_at` NULL.
- `list_runs(skill, limit)` returns most-recent-first up to `limit`.

The tests intentionally cross the recorder ↔ Store boundary; they do
not poke at the Store directly except where the spec doesn't expose a
read API (e.g. raw column inspection for `started_at IS NULL`).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ember.store.repo import Store


def _import_recorder():
    from ember.scheduler.recorder import StoreRunRecorder

    return StoreRunRecorder


@pytest.fixture
async def store(tmp_path: Path):
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def recorder(store: Store):
    StoreRunRecorder = _import_recorder()
    return StoreRunRecorder(store)


# --- record_started -------------------------------------------------------


class TestRecordStarted:
    async def test_returns_int_id(self, recorder, store: Store) -> None:
        rid = await recorder.record_started(
            skill="demo",
            function="tick",
            run_name="tick",
            scheduled_for=datetime(2026, 4, 27, 5, 0, tzinfo=timezone.utc),
            started_at=datetime(2026, 4, 27, 5, 0, 1, tzinfo=timezone.utc),
        )
        assert isinstance(rid, int) and rid > 0

    async def test_two_starts_return_distinct_ids(self, recorder, store: Store) -> None:
        a = await recorder.record_started(
            skill="demo",
            function="tick",
            run_name="tick",
            scheduled_for=None,
            started_at=datetime(2026, 4, 27, 5, 0, 1, tzinfo=timezone.utc),
        )
        b = await recorder.record_started(
            skill="demo",
            function="tick",
            run_name="tick",
            scheduled_for=None,
            started_at=datetime(2026, 4, 27, 6, 0, 1, tzinfo=timezone.utc),
        )
        assert a != b


# --- record_finished mutates the started row ------------------------------


class TestRecordFinished:
    async def test_finishing_sets_status_and_finished_at(self, recorder, store: Store) -> None:
        started_at = datetime(2026, 4, 27, 5, 0, 1, tzinfo=timezone.utc)
        rid = await recorder.record_started(
            skill="demo",
            function="tick",
            run_name="tick",
            scheduled_for=None,
            started_at=started_at,
        )
        finished_at = started_at + timedelta(seconds=2)
        await recorder.record_finished(
            rid,
            finished_at=finished_at,
            status="ok",
            result_json='{"count": 3}',
            error_text=None,
        )

        # Inspect via Store's raw connection (the spec doesn't expose a
        # read-by-id API, but the schema is the contract here).
        cur = await store._c.execute("SELECT * FROM scheduled_runs WHERE id = ?", (rid,))
        row = await cur.fetchone()
        await cur.close()
        assert row is not None
        assert row["status"] == "ok"
        assert row["result_json"] == '{"count": 3}'
        assert row["error_text"] is None
        assert row["finished_at"] is not None

    async def test_finishing_with_error_text(self, recorder, store: Store) -> None:
        started_at = datetime(2026, 4, 27, 5, 0, 1, tzinfo=timezone.utc)
        rid = await recorder.record_started(
            skill="demo",
            function="tick",
            run_name="tick",
            scheduled_for=None,
            started_at=started_at,
        )
        await recorder.record_finished(
            rid,
            finished_at=started_at + timedelta(seconds=1),
            status="error",
            result_json=None,
            error_text="Traceback (most recent call last):\nValueError: nope",
        )

        cur = await store._c.execute(
            "SELECT status, result_json, error_text FROM scheduled_runs WHERE id = ?", (rid,)
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None
        assert row["status"] == "error"
        assert row["result_json"] is None
        assert "ValueError" in row["error_text"]


# --- record_missed --------------------------------------------------------


class TestRecordMissed:
    async def test_missed_writes_row_with_started_at_null(self, recorder, store: Store) -> None:
        scheduled_for = datetime(2026, 4, 27, 5, 0, tzinfo=timezone.utc)
        await recorder.record_missed(
            skill="demo",
            function="tick",
            run_name="tick",
            scheduled_for=scheduled_for,
        )

        cur = await store._c.execute(
            "SELECT skill, run_name, status, started_at, scheduled_for "
            "FROM scheduled_runs WHERE skill = ? ORDER BY id DESC LIMIT 1",
            ("demo",),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None
        assert row["skill"] == "demo"
        assert row["run_name"] == "tick"
        assert row["status"] == "missed"
        assert row["started_at"] is None
        # `scheduled_for` is persisted (non-null).
        assert row["scheduled_for"] is not None


# --- list_runs ordering & limit ------------------------------------------


class TestListRuns:
    async def test_orders_most_recent_first(self, recorder, store: Store) -> None:
        # Insert three runs with increasing started_at.
        base = datetime(2026, 4, 27, 5, 0, tzinfo=timezone.utc)
        for i, delta in enumerate([0, 60, 120]):
            rid = await recorder.record_started(
                skill="demo",
                function=f"f{i}",
                run_name=f"run{i}",
                scheduled_for=None,
                started_at=base + timedelta(seconds=delta),
            )
            await recorder.record_finished(
                rid,
                finished_at=base + timedelta(seconds=delta + 1),
                status="ok",
                result_json=f'"{i}"',
                error_text=None,
            )

        runs = await recorder.list_runs("demo", limit=10)
        # Most-recent-first → run2, run1, run0.
        run_names = [r.run_name for r in runs]
        assert run_names == ["run2", "run1", "run0"]

    async def test_respects_limit(self, recorder, store: Store) -> None:
        base = datetime(2026, 4, 27, 5, 0, tzinfo=timezone.utc)
        for i in range(5):
            rid = await recorder.record_started(
                skill="demo",
                function="f",
                run_name=f"run{i}",
                scheduled_for=None,
                started_at=base + timedelta(seconds=i),
            )
            await recorder.record_finished(
                rid,
                finished_at=base + timedelta(seconds=i + 1),
                status="ok",
                result_json=None,
                error_text=None,
            )
        runs = await recorder.list_runs("demo", limit=2)
        assert len(runs) == 2
        # Most recent two: run4, run3.
        assert [r.run_name for r in runs] == ["run4", "run3"]

    async def test_filters_by_skill(self, recorder, store: Store) -> None:
        base = datetime(2026, 4, 27, 5, 0, tzinfo=timezone.utc)
        rid_a = await recorder.record_started(
            skill="alpha",
            function="f",
            run_name="x",
            scheduled_for=None,
            started_at=base,
        )
        await recorder.record_finished(
            rid_a,
            finished_at=base + timedelta(seconds=1),
            status="ok",
            result_json=None,
            error_text=None,
        )
        rid_b = await recorder.record_started(
            skill="beta",
            function="f",
            run_name="y",
            scheduled_for=None,
            started_at=base + timedelta(seconds=10),
        )
        await recorder.record_finished(
            rid_b,
            finished_at=base + timedelta(seconds=11),
            status="ok",
            result_json=None,
            error_text=None,
        )

        only_alpha = await recorder.list_runs("alpha", limit=10)
        assert len(only_alpha) == 1
        assert only_alpha[0].skill == "alpha"

    async def test_includes_missed_rows(self, recorder, store: Store) -> None:
        await recorder.record_missed(
            skill="demo",
            function="tick",
            run_name="tick",
            scheduled_for=datetime(2026, 4, 27, 5, 0, tzinfo=timezone.utc),
        )
        runs = await recorder.list_runs("demo", limit=10)
        assert len(runs) == 1
        assert runs[0].status == "missed"
        assert runs[0].started_at is None
