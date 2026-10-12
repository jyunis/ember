"""Ember v13 "Closure" — lessons store as source of truth (FC-26 / TLC-12 / TLC-21b).

Tester-authored from the CONTRACT + SIGNATURES only (implementer/tester
barrier): these exercise the rewritten `LessonsWriter` surface — the
normalized-text `dedup_key` (seam ①), the deterministic `format_entry` /
`render` (seam ②, the generated artifact), and the store-backed
`apply_lesson` / `prune_lesson` — never reading their bodies.

The pure-unit surfaces (`dedup_key`, `format_entry`, `render`) are NON-git so
they always run. The `apply_lesson` / `prune_lesson` paths need BOTH a real git
`IdentityStore` AND a `Store`, so they are guarded on git availability.

Invariants pinned here:

- **seam ①**: `dedup_key` normalizes text (case, whitespace, surrounding
  punctuation) before hashing — equivalent phrasings collapse.
- **seam ② / TLC-12**: `render` is byte-stable and order-preserving; an empty
  live set renders to "" so `system_prompt()` is byte-identical to soul+profile.
- **TLC-21b 8 KB cap**: `apply_lesson` enforces the cap over the PROSPECTIVE
  render before inserting; over-cap writes NOTHING and returns a pruning hint.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ember.identity.lessons import LessonSyncResult, LessonsWriter
from ember.identity.store import IdentityStore
from ember.store.repo import Store
from ember.store.types import Lesson

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


# --- Fixtures -------------------------------------------------------------


def _has_git() -> bool:
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


_skip_no_git = pytest.mark.skipif(not _has_git(), reason="git not available")


@pytest.fixture
async def identity(tmp_path: Path) -> IdentityStore:
    """A fresh git-initialized IdentityStore on a per-test directory."""
    s = IdentityStore(tmp_path / "identity")
    await s.ensure_initialized()
    return s


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


def _lesson(
    *,
    id: int,
    text: str,
    dedup_key: str = "",
    reinforced_count: int = 0,
) -> Lesson:
    return Lesson(
        id=id,
        text=text,
        evidence_json='{"messages": [1]}',
        dedup_key=dedup_key or f"k{id}",
        created_at=_T0,
        last_reinforced_at=_T0,
        reinforced_count=reinforced_count,
        source_proposal_id=None,
        removed_at=None,
    )


# --- LessonSyncResult shape -----------------------------------------------


class TestLessonSyncResultShape:
    def test_success_shape(self, tmp_path: Path) -> None:
        r = LessonSyncResult(
            committed=True,
            lessons_path=tmp_path / "lessons.md",
            git_commit_sha="abcdef",
            error=None,
            reinforced=False,
            lesson_id=7,
        )
        assert r.committed is True
        assert r.lessons_path == tmp_path / "lessons.md"
        assert r.git_commit_sha == "abcdef"
        assert r.error is None
        assert r.reinforced is False
        assert r.lesson_id == 7

    def test_defaults(self, tmp_path: Path) -> None:
        r = LessonSyncResult(
            committed=False,
            lessons_path=tmp_path / "lessons.md",
            git_commit_sha=None,
            error="please prune",
        )
        assert r.reinforced is False
        assert r.lesson_id is None


# --- dedup_key (seam ①, pure) ---------------------------------------------


class TestDedupKey:
    def test_hex_shape(self) -> None:
        key = LessonsWriter.dedup_key("prefers repair")
        assert isinstance(key, str)
        assert len(key) == 64
        int(key, 16)  # valid hex

    def test_case_whitespace_and_punctuation_equivalence(self) -> None:
        base = LessonsWriter.dedup_key("prefers repair")
        assert LessonsWriter.dedup_key("Prefers repair.") == base
        assert LessonsWriter.dedup_key("  prefers   repair  ") == base
        assert LessonsWriter.dedup_key("PREFERS REPAIR") == base

    def test_distinct_text_distinct_key(self) -> None:
        assert LessonsWriter.dedup_key("prefers repair") != LessonsWriter.dedup_key(
            "prefers replacement"
        )

    def test_deterministic(self) -> None:
        assert LessonsWriter.dedup_key("be terse") == LessonsWriter.dedup_key("be terse")


# --- format_entry (deterministic) -----------------------------------------


class TestFormatEntry:
    def test_contains_date_text_evidence_and_count(self) -> None:
        entry = LessonsWriter.format_entry(
            date="2026-07-06",
            text="avoid X",
            evidence={"messages": [2075]},
            reinforced_count=2,
        )
        assert "2026-07-06" in entry
        assert "avoid X" in entry
        assert "2075" in entry
        assert ("2" in entry) or ("3" in entry)

    def test_deterministic(self) -> None:
        kwargs = dict(
            date="2026-07-06",
            text="avoid X",
            evidence={"messages": [2075]},
            reinforced_count=2,
        )
        assert LessonsWriter.format_entry(**kwargs) == LessonsWriter.format_entry(**kwargs)


# --- render (seam ②, pure) ------------------------------------------------


class TestRender:
    def test_empty_list_is_empty_string(self) -> None:
        assert LessonsWriter.render([]) == ""

    def test_non_empty_contains_each_text_and_is_non_empty(self) -> None:
        rows = [
            _lesson(id=1, text="prefers metric units"),
            _lesson(id=2, text="be terse in replies"),
        ]
        out = LessonsWriter.render(rows)
        assert out != ""
        assert "prefers metric units" in out
        assert "be terse in replies" in out

    def test_byte_stable(self) -> None:
        rows = [_lesson(id=1, text="alpha"), _lesson(id=2, text="beta")]
        assert LessonsWriter.render(rows) == LessonsWriter.render(rows)
        # Identical content in fresh objects → identical render.
        rows2 = [_lesson(id=1, text="alpha"), _lesson(id=2, text="beta")]
        assert LessonsWriter.render(rows) == LessonsWriter.render(rows2)

    def test_order_preserving_no_resort(self) -> None:
        forward = [_lesson(id=1, text="aaa"), _lesson(id=2, text="bbb")]
        reverse = [_lesson(id=2, text="bbb"), _lesson(id=1, text="aaa")]
        out_fwd = LessonsWriter.render(forward)
        out_rev = LessonsWriter.render(reverse)
        assert out_fwd != out_rev
        assert out_fwd.index("aaa") < out_fwd.index("bbb")
        assert out_rev.index("bbb") < out_rev.index("aaa")


# --- apply_lesson (store-backed, git) -------------------------------------


@_skip_no_git
class TestApplyLesson:
    async def test_insert_happy_path(self, identity: IdentityStore, store: Store) -> None:
        w = LessonsWriter(identity=identity, store=store)
        pid = await _seed_proposal(store, summary="metric")
        result = await w.apply_lesson(
            text="prefers metric units",
            evidence={"messages": [2075]},
            proposal_id=pid,
            reinforced_count=0,
            now=_T0,
        )
        assert result.committed is True
        assert result.reinforced is False
        assert result.git_commit_sha is not None
        assert result.error is None
        assert result.lesson_id is not None
        # A live row now exists.
        live = await store.live_lessons()
        assert len(live) == 1
        assert live[0].id == result.lesson_id
        assert live[0].text == "prefers metric units"
        # lessons.md carries the text and respects the cap.
        content = identity.read_lessons()
        assert "prefers metric units" in content
        assert len(content.encode("utf-8")) <= LessonsWriter.LESSONS_CAP_BYTES

    async def test_reinforce_path_no_second_row(
        self, identity: IdentityStore, store: Store
    ) -> None:
        w = LessonsWriter(identity=identity, store=store)
        pid1 = await _seed_proposal(store, summary="repair1")
        pid2 = await _seed_proposal(store, summary="repair2")
        first = await w.apply_lesson(
            text="prefers repair",
            evidence={"messages": [1]},
            proposal_id=pid1,
            reinforced_count=0,
            now=_T0,
        )
        assert first.reinforced is False
        # A normalized-equivalent phrasing must reinforce, not insert.
        second = await w.apply_lesson(
            text="  Prefers   repair. ",
            evidence={"messages": [2]},
            proposal_id=pid2,
            reinforced_count=0,
            now=datetime(2026, 2, 1, tzinfo=UTC),
        )
        assert second.reinforced is True
        assert second.committed is True
        # Still exactly one live row, with a bumped reinforcement count.
        live = await store.live_lessons()
        assert len(live) == 1
        assert live[0].id == first.lesson_id
        assert live[0].reinforced_count == 1

    async def test_cap_refusal_writes_nothing(self, identity: IdentityStore, store: Store) -> None:
        w = LessonsWriter(identity=identity, store=store)
        # Seed many large lessons so the prospective render exceeds 8192.
        for i in range(40):
            pid = await _seed_proposal(store, summary=f"pad{i}")
            r = await w.apply_lesson(
                text=f"lesson number {i} " + "padding words to grow the render " * 6,
                evidence={"messages": [1]},
                proposal_id=pid,
                reinforced_count=0,
                now=_T0,
            )
            if not r.committed:
                break  # cap already reached mid-seed
        before_count = len(await store.live_lessons())
        before_file = identity.read_lessons()
        assert before_count > 0

        over_pid = await _seed_proposal(store, summary="over")
        result = await w.apply_lesson(
            text="one more lesson that should push the render over the cap " * 3,
            evidence={"messages": [1]},
            proposal_id=over_pid,
            reinforced_count=0,
            now=_T0,
        )
        assert result.committed is False
        assert result.error is not None
        assert "prune" in result.error.lower()
        # Nothing inserted, file byte-identical.
        assert len(await store.live_lessons()) == before_count
        assert identity.read_lessons() == before_file


# --- prune_lesson (store-backed, git) -------------------------------------


@_skip_no_git
class TestPruneLesson:
    async def test_prune_drops_text_from_file(self, identity: IdentityStore, store: Store) -> None:
        w = LessonsWriter(identity=identity, store=store)
        keep = await w.apply_lesson(
            text="keep this lesson",
            evidence={"messages": [1]},
            proposal_id=await _seed_proposal(store, summary="keep"),
            reinforced_count=0,
            now=_T0,
        )
        drop = await w.apply_lesson(
            text="drop this lesson",
            evidence={"messages": [2]},
            proposal_id=await _seed_proposal(store, summary="drop"),
            reinforced_count=0,
            now=_T0,
        )
        assert "drop this lesson" in identity.read_lessons()

        result = await w.prune_lesson(lesson_id=drop.lesson_id, proposal_id=2, now=_T0)
        assert result.committed is True
        content = identity.read_lessons()
        assert "drop this lesson" not in content
        assert "keep this lesson" in content
        # Store reflects the soft-delete.
        live_ids = {r.id for r in await store.live_lessons()}
        assert keep.lesson_id in live_ids
        assert drop.lesson_id not in live_ids

    async def test_prune_last_lesson_restores_soul_plus_profile(
        self, identity: IdentityStore, store: Store
    ) -> None:
        expected = identity.read_soul() + "\n\n" + identity.read_profile()
        w = LessonsWriter(identity=identity, store=store)
        only = await w.apply_lesson(
            text="the only lesson",
            evidence={"messages": [1]},
            proposal_id=await _seed_proposal(store, summary="only"),
            reinforced_count=0,
            now=_T0,
        )
        # With a lesson present the prompt differs.
        assert identity.system_prompt() != expected

        result = await w.prune_lesson(lesson_id=only.lesson_id, proposal_id=1, now=_T0)
        assert result.committed is True
        # Empty live set → lessons.md == "" → prompt byte-identical again (TLC-12).
        assert identity.read_lessons() == ""
        assert identity.system_prompt() == expected

    async def test_unknown_id_not_committed(self, identity: IdentityStore, store: Store) -> None:
        w = LessonsWriter(identity=identity, store=store)
        result = await w.prune_lesson(lesson_id=999999, proposal_id=1, now=_T0)
        assert result.committed is False

    async def test_already_removed_id_not_committed(
        self, identity: IdentityStore, store: Store
    ) -> None:
        w = LessonsWriter(identity=identity, store=store)
        made = await w.apply_lesson(
            text="short lived",
            evidence={"messages": [1]},
            proposal_id=await _seed_proposal(store, summary="short"),
            reinforced_count=0,
            now=_T0,
        )
        first = await w.prune_lesson(lesson_id=made.lesson_id, proposal_id=1, now=_T0)
        assert first.committed is True
        second = await w.prune_lesson(lesson_id=made.lesson_id, proposal_id=1, now=_T0)
        assert second.committed is False
