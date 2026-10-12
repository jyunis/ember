"""Ember v12.5 — weekly-digest formatting (`ember.learning.digest.format_digest`).

Tester-authored from the CONTRACT + SIGNATURE only (implementer/tester
barrier): `format_digest(pending: list[Proposal]) -> str` renders a
deterministic digest body for the scheduler channel. Empty input → one
short line; non-empty → a header plus one numbered line per proposal,
each carrying the proposal id and summary. Pure/deterministic (no clock).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from datetime import UTC, datetime

from ember.learning.digest import AppliedEval, format_applied_since, format_digest
from ember.store.types import EvalRun


def _run(
    run_id: int,
    *,
    finished: bool = True,
    passed: int = 0,
    failed: int = 0,
    errored: int = 0,
    total: int = 3,
    identity: str | None = "id-a",
    selector: str | None = "tone",
) -> EvalRun:
    dt = datetime(2026, 7, 5, tzinfo=UTC)
    return EvalRun(
        id=run_id,
        started_at=dt,
        finished_at=dt if finished else None,
        selector=selector,
        fingerprint="fp",
        identity_sha=identity,
        skills_hash="sk",
        provider_route="claude_cli",
        image_version="img",
        total_cases=total,
        passed=passed,
        failed=failed,
        errored=errored,
        skipped=0,
    )


# --- v13 applied-delta section (B) ---------------------------------------


class TestFormatAppliedSince:
    def test_empty_is_blank(self) -> None:
        assert format_applied_since([]) == ""

    def test_missing_run_noted(self) -> None:
        out = format_applied_since([AppliedEval(1, "profile", "fact: x", run=None, prev=None)])
        assert "link missing" in out.lower()

    def test_in_flight_measuring(self) -> None:
        run = _run(9, finished=False)
        out = format_applied_since([AppliedEval(2, "lesson", "be terse", run=run, prev=None)])
        assert "measuring" in out.lower()
        assert "#9" in out

    def test_all_errored_noted(self) -> None:
        run = _run(4, passed=0, failed=0, errored=3, total=3)
        out = format_applied_since([AppliedEval(3, "profile", "f", run=run, prev=None)])
        assert "errored" in out.lower()

    def test_no_prev_is_baseline(self) -> None:
        run = _run(7, passed=3, failed=0, errored=0)
        out = format_applied_since([AppliedEval(5, "profile", "f", run=run, prev=None)])
        assert "baseline" in out.lower()
        assert "#7" in out

    def test_delta_vs_prev(self) -> None:
        prev = _run(6, passed=1, failed=2, errored=0, identity="id-old")
        cur = _run(8, passed=3, failed=0, errored=0, identity="id-new")
        out = format_applied_since([AppliedEval(5, "profile", "f", run=cur, prev=prev)])
        assert "pass +2" in out
        assert "fail -2" in out
        assert "identity" in out  # the axis that moved
        assert "#8 vs #6" in out

    def test_deterministic(self) -> None:
        cur = _run(8, passed=2, failed=1)
        prev = _run(6, passed=1, failed=2)
        entries = [AppliedEval(5, "profile", "f", run=cur, prev=prev)]
        assert format_applied_since(entries) == format_applied_since(entries)


from ember.store.repo import Store  # noqa: E402  (kept after the pure-formatter tests)


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


# --- Empty input ----------------------------------------------------------


class TestEmptyDigest:
    def test_empty_is_single_nonempty_line(self) -> None:
        result = format_digest([])
        assert result != ""
        assert result.strip() != ""
        assert "\n" not in result.strip()


# --- Non-empty input ------------------------------------------------------


class TestNonEmptyDigest:
    async def _seed_two(self, store: Store):
        cid = await _conv(store)
        u1 = await store.append_message(cid, "user", "one")
        u2 = await store.append_message(cid, "user", "two")
        await store.record_proposal(
            kind="lesson",
            summary="Avoid the flaky retry path",
            # The full lesson text lives in the payload; the digest renders THIS
            # (not the 80-char `summary` label) so the reviewer sees the real
            # suggestion untruncated (v12.5 refinement).
            payload={"text": "always retry the flaky path at most twice, then give up"},
            evidence={"messages": [u1]},
        )
        await store.record_proposal(
            kind="finding",
            summary="Nightly retro keeps failing",
            payload={"run": "nightly"},  # no prose field → digest falls back to summary
            evidence={"messages": [u2]},
        )
        return await store.list_proposals(status="pending")

    async def test_contains_each_id_and_full_text(self, store: Store) -> None:
        pending = await self._seed_two(store)
        assert len(pending) == 2
        result = format_digest(pending)
        for p in pending:
            assert str(p.id) in result
        # Lesson: the full payload text is shown, not the truncated summary label.
        assert "always retry the flaky path at most twice, then give up" in result
        # Finding: no prose payload field → the summary is the fallback display.
        assert "Nightly retro keeps failing" in result

    async def test_deterministic(self, store: Store) -> None:
        pending = await self._seed_two(store)
        assert format_digest(pending) == format_digest(pending)

    async def test_has_header_and_multiple_lines(self, store: Store) -> None:
        pending = await self._seed_two(store)
        result = format_digest(pending)
        # Header + one line per proposal → more than one line.
        assert "\n" in result.strip()
