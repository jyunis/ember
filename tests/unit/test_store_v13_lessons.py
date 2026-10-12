"""Store v=13 — learning loop Layer 4 (lessons table, FC-26 / seam ①).

Tester-authored from the CONTRACT + INTERFACE only (implementer/tester
barrier): these exercise the six new `lessons` writer/reader methods on
`Store` purely through the public API, never reading their bodies.

Invariants pinned here:

- **FC-26 lessons store**: the `lessons` table is the source of truth for
  learned lessons; rows carry reinforcement + age so `lessons.md` can be a
  deterministic render of the live (`removed_at IS NULL`) rows.
- **seam ① (normalized-text dedup)**: a partial-unique index on
  `dedup_key WHERE removed_at IS NULL` forbids two LIVE rows sharing a key;
  a soft-deleted row does NOT block a fresh insert of the same key, and
  `reinforce_lesson` only ever touches the live row.

Datetimes are injected so ordering/staleness are deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ember.errors import StoreError
from ember.store.repo import Store

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path):
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


async def _seed_proposal(store: Store, *, summary: str = "s") -> int:
    """A real pending lesson proposal, so a lesson row may FK-reference it."""
    conv = await store.get_or_create_conversation("discord", f"k-{summary}")
    uid = await store.append_message(conv.id, "user", "user signal")
    pid, _ = await store.record_proposal(
        kind="lesson", summary=summary, payload={"text": summary}, evidence={"messages": [uid]}
    )
    return pid


# --- insert_lesson --------------------------------------------------------


class TestInsertLesson:
    async def test_insert_returns_id_and_row_is_live(self, store: Store) -> None:
        lid = await store.insert_lesson(
            text="prefers metric units",
            evidence={"messages": [1]},
            dedup_key="k-metric",
            created_at=_T0,
        )
        assert isinstance(lid, int)
        row = await store.get_lesson(lid)
        assert row is not None
        assert row.id == lid
        assert row.text == "prefers metric units"
        assert row.dedup_key == "k-metric"
        assert row.removed_at is None
        assert row.reinforced_count == 0
        assert row.source_proposal_id is None
        assert [r.id for r in await store.live_lessons()] == [lid]

    async def test_last_reinforced_at_seeded_to_created_at(self, store: Store) -> None:
        lid = await store.insert_lesson(
            text="be terse",
            evidence={"messages": [1]},
            dedup_key="k-terse",
            created_at=_T0,
        )
        row = await store.get_lesson(lid)
        assert row is not None
        assert row.created_at == _T0
        assert row.last_reinforced_at == _T0

    async def test_optional_fields_persist(self, store: Store) -> None:
        pid = await _seed_proposal(store, summary="cite")
        lid = await store.insert_lesson(
            text="cite sources",
            evidence={"messages": [7]},
            dedup_key="k-cite",
            created_at=_T0,
            reinforced_count=3,
            source_proposal_id=pid,
        )
        row = await store.get_lesson(lid)
        assert row is not None
        assert row.reinforced_count == 3
        assert row.source_proposal_id == pid

    async def test_duplicate_live_dedup_key_raises(self, store: Store) -> None:
        await store.insert_lesson(
            text="prefers metric units",
            evidence={"messages": [1]},
            dedup_key="dup",
            created_at=_T0,
        )
        with pytest.raises(StoreError):
            await store.insert_lesson(
                text="prefers metric units (again)",
                evidence={"messages": [2]},
                dedup_key="dup",
                created_at=_T0 + timedelta(days=1),
            )
        # Only the first row exists.
        assert len(await store.live_lessons()) == 1


# --- reinforce_lesson -----------------------------------------------------


class TestReinforceLesson:
    async def test_reinforce_bumps_count_and_timestamp(self, store: Store) -> None:
        lid = await store.insert_lesson(
            text="be terse",
            evidence={"messages": [1]},
            dedup_key="k",
            created_at=_T0,
        )
        later = _T0 + timedelta(days=5)
        updated = await store.reinforce_lesson("k", now=later)
        assert updated is not None
        assert updated.id == lid
        assert updated.reinforced_count == 1
        assert updated.last_reinforced_at == later
        # created_at is untouched.
        assert updated.created_at == _T0

    async def test_reinforce_is_cumulative(self, store: Store) -> None:
        await store.insert_lesson(
            text="be terse", evidence={"messages": [1]}, dedup_key="k", created_at=_T0
        )
        await store.reinforce_lesson("k", now=_T0 + timedelta(days=1))
        second = await store.reinforce_lesson("k", now=_T0 + timedelta(days=2))
        assert second is not None
        assert second.reinforced_count == 2
        assert second.last_reinforced_at == _T0 + timedelta(days=2)

    async def test_reinforce_no_match_returns_none(self, store: Store) -> None:
        assert await store.reinforce_lesson("nope", now=_T0) is None

    async def test_reinforce_ignores_soft_deleted(self, store: Store) -> None:
        # A pruned row with a given key must NOT be reinforced, and a fresh
        # insert of that key is allowed after soft-delete (seam ①).
        lid = await store.insert_lesson(
            text="old lesson", evidence={"messages": [1]}, dedup_key="k", created_at=_T0
        )
        await store.soft_delete_lesson(lid, now=_T0 + timedelta(days=1))

        # No live row → reinforce finds nothing.
        assert await store.reinforce_lesson("k", now=_T0 + timedelta(days=2)) is None

        # Re-insert of the same key is permitted now that the old row is dead.
        new_lid = await store.insert_lesson(
            text="fresh lesson",
            evidence={"messages": [2]},
            dedup_key="k",
            created_at=_T0 + timedelta(days=3),
        )
        assert new_lid != lid
        bumped = await store.reinforce_lesson("k", now=_T0 + timedelta(days=4))
        assert bumped is not None
        assert bumped.id == new_lid
        assert bumped.reinforced_count == 1
        # The dead row stayed dead and untouched.
        dead = await store.get_lesson(lid)
        assert dead is not None
        assert dead.removed_at is not None
        assert dead.reinforced_count == 0


# --- live_lessons ---------------------------------------------------------


class TestLiveLessons:
    async def test_empty_when_none(self, store: Store) -> None:
        assert await store.live_lessons() == []

    async def test_excludes_removed_and_orders_id_asc(self, store: Store) -> None:
        a = await store.insert_lesson(
            text="a", evidence={"messages": [1]}, dedup_key="ka", created_at=_T0
        )
        b = await store.insert_lesson(
            text="b", evidence={"messages": [2]}, dedup_key="kb", created_at=_T0
        )
        c = await store.insert_lesson(
            text="c", evidence={"messages": [3]}, dedup_key="kc", created_at=_T0
        )
        await store.soft_delete_lesson(b, now=_T0 + timedelta(days=1))

        live = await store.live_lessons()
        assert [r.id for r in live] == [a, c]


# --- stale_lessons --------------------------------------------------------


class TestStaleLessons:
    async def test_only_live_older_than_cutoff(self, store: Store) -> None:
        old = await store.insert_lesson(
            text="old", evidence={"messages": [1]}, dedup_key="ko", created_at=_T0
        )
        recent = await store.insert_lesson(
            text="recent",
            evidence={"messages": [2]},
            dedup_key="kr",
            created_at=_T0 + timedelta(days=30),
        )
        cutoff = _T0 + timedelta(days=10)
        stale = await store.stale_lessons(older_than=cutoff)
        ids = [r.id for r in stale]
        assert old in ids
        assert recent not in ids

    async def test_boundary_equal_is_not_stale(self, store: Store) -> None:
        # last_reinforced_at seeds to created_at; exactly-equal is NOT stale
        # since the comparison is strict `<`.
        await store.insert_lesson(
            text="edge", evidence={"messages": [1]}, dedup_key="ke", created_at=_T0
        )
        assert await store.stale_lessons(older_than=_T0) == []
        assert len(await store.stale_lessons(older_than=_T0 + timedelta(seconds=1))) == 1

    async def test_reinforce_refreshes_staleness(self, store: Store) -> None:
        await store.insert_lesson(
            text="edge", evidence={"messages": [1]}, dedup_key="k", created_at=_T0
        )
        cutoff = _T0 + timedelta(days=10)
        assert len(await store.stale_lessons(older_than=cutoff)) == 1
        # Reinforcing past the cutoff clears staleness.
        await store.reinforce_lesson("k", now=_T0 + timedelta(days=20))
        assert await store.stale_lessons(older_than=cutoff) == []

    async def test_excludes_removed(self, store: Store) -> None:
        lid = await store.insert_lesson(
            text="old", evidence={"messages": [1]}, dedup_key="k", created_at=_T0
        )
        await store.soft_delete_lesson(lid, now=_T0 + timedelta(days=1))
        assert await store.stale_lessons(older_than=_T0 + timedelta(days=100)) == []

    async def test_ordered_id_asc(self, store: Store) -> None:
        a = await store.insert_lesson(
            text="a", evidence={"messages": [1]}, dedup_key="ka", created_at=_T0
        )
        b = await store.insert_lesson(
            text="b", evidence={"messages": [2]}, dedup_key="kb", created_at=_T0
        )
        got = [r.id for r in await store.stale_lessons(older_than=_T0 + timedelta(days=1))]
        assert got == [a, b]


# --- soft_delete_lesson ---------------------------------------------------


class TestSoftDeleteLesson:
    async def test_sets_removed_at_and_drops_from_live(self, store: Store) -> None:
        lid = await store.insert_lesson(
            text="doomed", evidence={"messages": [1]}, dedup_key="k", created_at=_T0
        )
        now = _T0 + timedelta(days=1)
        removed = await store.soft_delete_lesson(lid, now=now)
        assert removed is not None
        assert removed.id == lid
        assert removed.removed_at == now
        assert await store.live_lessons() == []
        # The row still exists for audit (TLC-8/9).
        row = await store.get_lesson(lid)
        assert row is not None
        assert row.removed_at == now

    async def test_second_call_returns_none(self, store: Store) -> None:
        lid = await store.insert_lesson(
            text="doomed", evidence={"messages": [1]}, dedup_key="k", created_at=_T0
        )
        assert await store.soft_delete_lesson(lid, now=_T0 + timedelta(days=1)) is not None
        # Guarded on removed_at IS NULL → second delete is a no-op.
        assert await store.soft_delete_lesson(lid, now=_T0 + timedelta(days=2)) is None

    async def test_unknown_id_returns_none(self, store: Store) -> None:
        assert await store.soft_delete_lesson(999999, now=_T0) is None


# --- get_lesson -----------------------------------------------------------


class TestGetLesson:
    async def test_unknown_id_returns_none(self, store: Store) -> None:
        assert await store.get_lesson(999999) is None
