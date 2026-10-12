"""Store v=12.5 — proposal state machine (FC-23) + expiry (TLC-21d).

Tester-authored from the CONTRACT + SIGNATURES only (implementer/tester
barrier): these tests exercise the new transition methods on
`Store` — `get_proposal`, `approve_proposal`, `mark_applied`,
`reject_proposal`, `edit_proposal`, `expire_proposals` — purely
through the public API, never reading their bodies.

Invariants pinned here:

- **FC-23 state machine**: legal edges are `pending → approved → applied`,
  `pending → rejected`, `pending → expired`; **no other edge exists** and
  terminal states (applied/rejected/expired) are immutable. Every illegal
  transition raises `ProposalStateError` and mutates NOTHING.
- **TLC-21d expiry**: unresolved (pending) proposals older than the cutoff
  expire silently; the clock is injected (`now=`), so no hidden
  `datetime.now()` may leak into the stamped `resolved_at`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ember.errors import ProposalStateError, StoreError
from ember.store.repo import Store


# --- Fixtures (copied verbatim from test_store_v12.py) --------------------


@pytest.fixture
async def store(tmp_path: Path):
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


async def _conv(store: Store, key: str = "t") -> int:
    conv = await store.get_or_create_conversation("discord", key)
    return conv.id


async def _seed_pending(
    store: Store,
    *,
    kind: str = "lesson",
    summary: str = "s",
    payload=None,
    key: str = "t",
    text: str = "x",
) -> int:
    """Seed one pending proposal citing a real user message as evidence."""
    cid = await _conv(store, key)
    uid = await store.append_message(cid, "user", text)
    pid, _reinforced = await store.record_proposal(
        kind=kind,
        summary=summary,
        payload=payload if payload is not None else {"text": "avoid X"},
        evidence={"messages": [uid]},
    )
    return pid


def _dt(y: int, mo: int = 1, d: int = 1, h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


# --- ProposalStateError is a StoreError -----------------------------------


class TestProposalStateErrorType:
    def test_is_store_error_subclass(self) -> None:
        assert issubclass(ProposalStateError, StoreError)


# --- get_proposal ---------------------------------------------------------


class TestGetProposal:
    async def test_unknown_id_returns_none(self, store: Store) -> None:
        assert await store.get_proposal(999999) is None

    async def test_known_id_returns_row(self, store: Store) -> None:
        pid = await _seed_pending(store)
        p = await store.get_proposal(pid)
        assert p is not None
        assert p.id == pid
        assert p.status == "pending"


# --- Legal edges ----------------------------------------------------------


class TestLegalEdges:
    async def test_approve_pending_to_approved_not_terminal(self, store: Store) -> None:
        pid = await _seed_pending(store)
        p = await store.approve_proposal(pid)
        assert p.status == "approved"
        assert p.resolved_at is None  # approved is NOT terminal

        # Re-read confirms persistence.
        again = await store.get_proposal(pid)
        assert again is not None
        assert again.status == "approved"
        assert again.resolved_at is None

    async def test_mark_applied_stamps_resolved_at_and_run_id(self, store: Store) -> None:
        pid = await _seed_pending(store)
        await store.approve_proposal(pid)
        now = _dt(2026, 7, 6, 12, 0, 0)
        p = await store.mark_applied(pid, now=now, applied_eval_run_id=None)
        assert p.status == "applied"
        assert p.resolved_at == now
        assert p.applied_eval_run_id is None

    async def test_mark_applied_echoes_eval_run_id(self, store: Store) -> None:
        # Need a real eval run id to satisfy any FK.
        rid = await store.create_eval_run(
            selector=None,
            fingerprint="fp",
            identity_sha=None,
            skills_hash=None,
            provider_route=None,
            image_version=None,
        )
        pid = await _seed_pending(store)
        await store.approve_proposal(pid)
        now = _dt(2026, 7, 6, 13, 0, 0)
        p = await store.mark_applied(pid, now=now, applied_eval_run_id=rid)
        assert p.status == "applied"
        assert p.applied_eval_run_id == rid
        assert p.resolved_at == now

    async def test_reject_pending_to_rejected_stamps_resolved_at(self, store: Store) -> None:
        pid = await _seed_pending(store)
        now = _dt(2026, 7, 6, 9, 30, 0)
        p = await store.reject_proposal(pid, now=now)
        assert p.status == "rejected"
        assert p.resolved_at == now


# --- Illegal edges raise + mutate nothing ---------------------------------


class TestIllegalEdges:
    async def _status(self, store: Store, pid: int) -> str:
        p = await store.get_proposal(pid)
        assert p is not None
        return p.status

    async def test_approve_twice_raises_and_no_mutation(self, store: Store) -> None:
        pid = await _seed_pending(store)
        await store.approve_proposal(pid)
        with pytest.raises(ProposalStateError):
            await store.approve_proposal(pid)
        assert await self._status(store, pid) == "approved"

    async def test_approve_applied_raises(self, store: Store) -> None:
        pid = await _seed_pending(store)
        await store.approve_proposal(pid)
        await store.mark_applied(pid, now=_dt(2026, 7, 6))
        with pytest.raises(ProposalStateError):
            await store.approve_proposal(pid)
        assert await self._status(store, pid) == "applied"

    async def test_approve_rejected_raises(self, store: Store) -> None:
        pid = await _seed_pending(store)
        await store.reject_proposal(pid, now=_dt(2026, 7, 6))
        with pytest.raises(ProposalStateError):
            await store.approve_proposal(pid)
        assert await self._status(store, pid) == "rejected"

    async def test_mark_applied_on_pending_raises(self, store: Store) -> None:
        pid = await _seed_pending(store)
        with pytest.raises(ProposalStateError):
            await store.mark_applied(pid, now=_dt(2026, 7, 6))
        assert await self._status(store, pid) == "pending"

    async def test_mark_applied_twice_raises(self, store: Store) -> None:
        pid = await _seed_pending(store)
        await store.approve_proposal(pid)
        await store.mark_applied(pid, now=_dt(2026, 7, 6))
        with pytest.raises(ProposalStateError):
            await store.mark_applied(pid, now=_dt(2026, 7, 7))
        assert await self._status(store, pid) == "applied"

    async def test_reject_approved_raises(self, store: Store) -> None:
        pid = await _seed_pending(store)
        await store.approve_proposal(pid)
        with pytest.raises(ProposalStateError):
            await store.reject_proposal(pid, now=_dt(2026, 7, 6))
        assert await self._status(store, pid) == "approved"

    async def test_reject_applied_raises(self, store: Store) -> None:
        pid = await _seed_pending(store)
        await store.approve_proposal(pid)
        await store.mark_applied(pid, now=_dt(2026, 7, 6))
        with pytest.raises(ProposalStateError):
            await store.reject_proposal(pid, now=_dt(2026, 7, 7))
        assert await self._status(store, pid) == "applied"

    async def test_reject_rejected_raises(self, store: Store) -> None:
        pid = await _seed_pending(store)
        await store.reject_proposal(pid, now=_dt(2026, 7, 6))
        with pytest.raises(ProposalStateError):
            await store.reject_proposal(pid, now=_dt(2026, 7, 7))
        assert await self._status(store, pid) == "rejected"

    async def test_unknown_id_every_transition_raises(self, store: Store) -> None:
        with pytest.raises(ProposalStateError):
            await store.approve_proposal(999999)
        with pytest.raises(ProposalStateError):
            await store.mark_applied(999999, now=_dt(2026, 7, 6))
        with pytest.raises(ProposalStateError):
            await store.reject_proposal(999999, now=_dt(2026, 7, 6))
        with pytest.raises(ProposalStateError):
            await store.edit_proposal(999999, payload={"x": 1})


# --- Terminal immutability ------------------------------------------------


class TestTerminalImmutability:
    async def _assert_all_transitions_raise(self, store: Store, pid: int, expected: str) -> None:
        with pytest.raises(ProposalStateError):
            await store.approve_proposal(pid)
        with pytest.raises(ProposalStateError):
            await store.mark_applied(pid, now=_dt(2026, 7, 8))
        with pytest.raises(ProposalStateError):
            await store.reject_proposal(pid, now=_dt(2026, 7, 8))
        with pytest.raises(ProposalStateError):
            await store.edit_proposal(pid, payload={"x": 999})
        p = await store.get_proposal(pid)
        assert p is not None
        assert p.status == expected

    async def test_applied_is_immutable(self, store: Store) -> None:
        pid = await _seed_pending(store)
        await store.approve_proposal(pid)
        await store.mark_applied(pid, now=_dt(2026, 7, 6))
        await self._assert_all_transitions_raise(store, pid, "applied")

    async def test_rejected_is_immutable(self, store: Store) -> None:
        pid = await _seed_pending(store)
        await store.reject_proposal(pid, now=_dt(2026, 7, 6))
        await self._assert_all_transitions_raise(store, pid, "rejected")

    async def test_expired_is_immutable(self, store: Store) -> None:
        pid = await _seed_pending(store)
        # Force it old, then expire it.
        await store._c.execute(
            "UPDATE proposals SET created_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00.000Z", pid),
        )
        await store._c.commit()
        expired = await store.expire_proposals(older_than=_dt(2026, 1, 1), now=_dt(2026, 7, 6))
        assert pid in expired
        await self._assert_all_transitions_raise(store, pid, "expired")


# --- edit_proposal --------------------------------------------------------


class TestEditProposal:
    async def test_edit_pending_replaces_payload_and_rehashes(self, store: Store) -> None:
        pid = await _seed_pending(store, payload={"text": "old"})
        before = await store.get_proposal(pid)
        assert before is not None
        p = await store.edit_proposal(pid, payload={"text": "new"}, summary="new summary")
        assert p.status == "pending"
        assert p.summary == "new summary"
        assert p.payload_hash != before.payload_hash

    async def test_edit_non_pending_raises(self, store: Store) -> None:
        pid = await _seed_pending(store)
        await store.approve_proposal(pid)
        with pytest.raises(ProposalStateError):
            await store.edit_proposal(pid, payload={"text": "new"})
        p = await store.get_proposal(pid)
        assert p is not None
        assert p.status == "approved"

    async def test_edit_into_existing_pending_collision_raises(self, store: Store) -> None:
        cid = await _conv(store)
        u1 = await store.append_message(cid, "user", "a")
        u2 = await store.append_message(cid, "user", "b")
        pid1, _ = await store.record_proposal(
            kind="finding", summary="A", payload={"p": 1}, evidence={"messages": [u1]}
        )
        pid2, _ = await store.record_proposal(
            kind="finding", summary="B", payload={"p": 2}, evidence={"messages": [u2]}
        )
        # Editing pid2 to collide with pid1's (kind, payload_hash) must raise.
        with pytest.raises(ProposalStateError):
            await store.edit_proposal(pid2, payload={"p": 1})
        # Both rows untouched.
        assert len(await store.list_proposals()) == 2


# --- Expiry (TLC-21d) -----------------------------------------------------


class TestExpiry:
    async def _force_old(self, store: Store, pid: int) -> None:
        await store._c.execute(
            "UPDATE proposals SET created_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00.000Z", pid),
        )
        await store._c.commit()

    async def test_old_pending_expires_and_stamps_now(self, store: Store) -> None:
        pid = await _seed_pending(store)
        await self._force_old(store, pid)
        now = _dt(2026, 7, 6, 3, 0, 0)
        ids = await store.expire_proposals(older_than=_dt(2026, 1, 1), now=now)
        assert pid in ids
        p = await store.get_proposal(pid)
        assert p is not None
        assert p.status == "expired"
        assert p.resolved_at == now  # injected clock, no hidden now()

    async def test_fresh_pending_not_expired(self, store: Store) -> None:
        pid = await _seed_pending(store)  # created_at = now (recent)
        ids = await store.expire_proposals(older_than=_dt(2000, 1, 1), now=_dt(2026, 7, 6))
        assert pid not in ids
        p = await store.get_proposal(pid)
        assert p is not None
        assert p.status == "pending"

    async def test_non_pending_old_row_not_expired(self, store: Store) -> None:
        pid = await _seed_pending(store)
        await store.approve_proposal(pid)
        await self._force_old(store, pid)
        ids = await store.expire_proposals(older_than=_dt(2026, 1, 1), now=_dt(2026, 7, 6))
        assert pid not in ids
        p = await store.get_proposal(pid)
        assert p is not None
        assert p.status == "approved"  # unchanged

    async def test_empty_case_returns_empty_and_changes_nothing(self, store: Store) -> None:
        pid = await _seed_pending(store)  # fresh, not old
        ids = await store.expire_proposals(older_than=_dt(2000, 1, 1), now=_dt(2026, 7, 6))
        assert ids == []
        p = await store.get_proposal(pid)
        assert p is not None
        assert p.status == "pending"
        assert p.resolved_at is None

    async def test_only_pending_rows_touched_mixed(self, store: Store) -> None:
        # One old pending, one old approved, one old rejected → only pending expires.
        # Distinct payloads so the (kind, payload_hash) dedup keeps them as
        # three separate pending rows (identical payloads would reinforce into
        # one row — correct v12 behavior, but not what this test is exercising).
        p_pending = await _seed_pending(store, key="p1", summary="pend", payload={"text": "p1"})
        p_approved = await _seed_pending(store, key="p2", summary="appr", payload={"text": "p2"})
        p_rejected = await _seed_pending(store, key="p3", summary="rej", payload={"text": "p3"})
        await store.approve_proposal(p_approved)
        await store.reject_proposal(p_rejected, now=_dt(2025, 1, 1))
        for pid in (p_pending, p_approved, p_rejected):
            await self._force_old(store, pid)

        ids = await store.expire_proposals(older_than=_dt(2026, 1, 1), now=_dt(2026, 7, 6))
        assert ids == [p_pending]

        assert (await store.get_proposal(p_pending)).status == "expired"
        assert (await store.get_proposal(p_approved)).status == "approved"
        assert (await store.get_proposal(p_rejected)).status == "rejected"


# --- Bulk reject (v12.5 UX) ------------------------------------------------


class TestBulkReject:
    async def test_rejects_all_pending_in_batch(self, store: Store) -> None:
        a = await _seed_pending(store, key="a", payload={"text": "a"})
        b = await _seed_pending(store, key="b", payload={"text": "b"})
        c = await _seed_pending(store, key="c", payload={"text": "c"})
        rejected = await store.reject_proposals([a, b, c], now=_dt(2026, 7, 6))
        assert set(rejected) == {a, b, c}
        for pid in (a, b, c):
            p = await store.get_proposal(pid)
            assert p.status == "rejected"
            assert p.resolved_at == _dt(2026, 7, 6)

    async def test_skips_non_pending_and_unknown(self, store: Store) -> None:
        a = await _seed_pending(store, key="a", payload={"text": "a"})
        b = await _seed_pending(store, key="b", payload={"text": "b"})
        await store.approve_proposal(b)  # b no longer pending
        rejected = await store.reject_proposals([a, b, 999999], now=_dt(2026, 7, 6))
        assert rejected == [a]  # only the pending one
        assert (await store.get_proposal(b)).status == "approved"

    async def test_empty_and_no_pending_return_empty(self, store: Store) -> None:
        assert await store.reject_proposals([], now=_dt(2026, 7, 6)) == []
        assert await store.reject_proposals([12345], now=_dt(2026, 7, 6)) == []
