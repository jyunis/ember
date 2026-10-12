"""Lessons writer (FC-24 / FC-26, TLC-21b, TLC-12).

`lessons.md` is the capped residual channel of the learning loop: durable
insight that compiles into neither a profile line, a trigger, nor a skill.

**v13 (Closure) inverted the source of truth (Decision #23).** Lessons now
live as durable rows in the `lessons` table so their reinforcement/age can be
tracked; `lessons.md` is a *deterministic render* of the live rows. The file is
still at `/data/identity/lessons.md`, git-versioned exactly like `profile.md`,
injected into the system block after profile, and re-read every turn — but it
is a **generated artifact** (seam ②): hand-edits are overwritten on the next
render, so the file carries a header saying so. Soul/profile keep their
file-primary hand-edit semantics; lessons does not (this supersedes FC-24's
"human hand-edits are legal" clause for lessons only).

This writer owns three invariants:

- **8 KB hard cap (TLC-21b), enforced over the RENDER.** An apply that would
  push the rendered file past `LESSONS_CAP_BYTES` inserts NOTHING and returns a
  pruning hint; the table and file are left untouched. The agent may never
  raise the cap.
- **Byte-stable render (TLC-12).** The render is a pure function of the live
  rows in the order given (`Store.live_lessons()` returns them id-ASC), with a
  constant header and the deterministic `format_entry`. Same rows → same bytes,
  so the cached system block stays stable between changes. Zero live rows →
  empty render → the file is empty and `system_prompt()` omits it, byte-identical
  to `soul + profile`.
- **Normalized-text dedup key (seam ① / Decision #24).** `dedup_key(text)`
  lowercases, collapses internal whitespace, and strips surrounding
  punctuation/whitespace before hashing — so cheap-tier paraphrase variance in
  casing/spacing/punctuation still reinforces one row instead of duplicating.
  It does NOT catch semantic paraphrase; a genuinely-recurring lesson that gets
  pruned is simply re-proposed fresh (self-correcting, no data loss).

Persistence mirrors the *current* `ProfileWriter`: atomic file write → git
commit (body references the proposal id). No documents-MCP mirror (removed from
the profile path 2026-05-06; the lessons file follows the same live contract).
All SQL stays in `repo.py` (FC-7) — this writer calls `Store` methods.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ember.identity.store import IdentityStore

if TYPE_CHECKING:
    from ember.store.repo import Store
    from ember.store.types import Lesson

_log = logging.getLogger("ember.identity.lessons")

# Title + generated-artifact banner, written once at the top of the render.
# Constant and byte-stable so the system block stays cacheable (TLC-12). The
# banner (seam ②) tells a human editor the file is generated from the table.
_HEADER = "# Lessons\n<!-- generated from the lessons table; edit via /proposal, not here -->\n"

# Strips surrounding punctuation/whitespace for the normalized dedup key.
_EDGE_PUNCT = re.compile(r"^[\W_]+|[\W_]+$")
_WS = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class LessonSyncResult:
    """Outcome of a `LessonsWriter.apply_lesson` / `prune_lesson` call.

    `committed` is True iff the table changed AND lessons.md was re-rendered and
    git-committed. On the cap-exceeded refusal (TLC-21b) `committed` is False,
    nothing was inserted, and `error` carries a pruning hint (the word "prune"
    appears). `reinforced` is True when an apply matched an existing live row
    (bumped it) instead of inserting. `lesson_id` is the affected row's id when
    known.
    """

    committed: bool
    lessons_path: Path
    git_commit_sha: str | None
    error: str | None
    reinforced: bool = False
    lesson_id: int | None = None


class LessonsWriter:
    """Owns the lessons.md render + cap + git-commit flow over the `lessons`
    table (FC-26)."""

    LESSONS_CAP_BYTES = 8192

    def __init__(self, *, identity: IdentityStore, store: "Store") -> None:
        self._identity = identity
        self._store = store

    # --- Normalized dedup key (seam ①) ------------------------------------

    @staticmethod
    def dedup_key(text: str) -> str:
        """Stable key for reinforcement matching: normalize the lesson text
        (lowercase, collapse internal whitespace, strip surrounding
        punctuation), then sha256. Deterministic; identical normalized text →
        identical key."""
        norm = _WS.sub(" ", text).strip().lower()
        norm = _EDGE_PUNCT.sub("", norm)
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()

    # --- Deterministic render (TLC-12) ------------------------------------

    @staticmethod
    def _render_evidence(evidence: dict[str, list[int]]) -> str:
        """A compact, deterministic evidence pointer. Only non-empty channels
        are shown; ids are sorted so the string is stable."""
        parts: list[str] = []
        for label, key in (("msgs", "messages"), ("feedback", "feedback"), ("runs", "runs")):
            ids = sorted({int(x) for x in evidence.get(key, [])})
            if ids:
                parts.append(f"{label} {', '.join(str(i) for i in ids)}")
        return "; ".join(parts) if parts else "—"

    @classmethod
    def format_entry(
        cls,
        *,
        date: str,
        text: str,
        evidence: dict[str, list[int]],
        reinforced_count: int,
    ) -> str:
        """One deterministic entry block (FC-24 format): date + text + evidence
        pointer + reinforcement count. No trailing newline. Deterministic:
        identical args → identical string."""
        ev = cls._render_evidence(evidence)
        return f"- **{date}** — {text.strip()}\n  _evidence: {ev} · reinforced {reinforced_count}×_"

    @classmethod
    def render(cls, lessons: list["Lesson"]) -> str:
        """Full-file render of the given live lessons, IN THE ORDER GIVEN (the
        caller passes them id-ASC — this does not re-sort). Byte-stable for a
        given row list (TLC-12). Empty list → "" so the file is empty and the
        system block stays byte-identical to soul+profile."""
        if not lessons:
            return ""
        blocks = [
            cls.format_entry(
                date=lesson.created_at.date().isoformat(),
                text=lesson.text,
                evidence=_loads_evidence(lesson.evidence_json),
                reinforced_count=lesson.reinforced_count,
            )
            for lesson in lessons
        ]
        return _HEADER + "\n" + "\n\n".join(blocks) + "\n"

    # --- Write paths (table → render → file → git) ------------------------

    async def apply_lesson(
        self,
        *,
        text: str,
        evidence: dict[str, list[int]],
        proposal_id: int,
        reinforced_count: int,
        now: datetime,
    ) -> LessonSyncResult:
        """Reinforce-or-insert a lesson, then re-render lessons.md + commit.

        If a live row already shares the normalized dedup key, bump it (defensive
        — the nightly usually reinforces before a duplicate proposal is created)
        and re-render. Otherwise enforce the 8 KB cap OVER THE PROSPECTIVE RENDER
        before inserting: over-cap → nothing inserted, pruning hint returned,
        file untouched."""
        key = self.dedup_key(text)

        reinforced = await self._store.reinforce_lesson(key, now=now)
        if reinforced is not None:
            result = await self._materialize(proposal_id=proposal_id, action="reinforce")
            return _with(result, reinforced=True, lesson_id=reinforced.id)

        # Cap is enforced over the render (TLC-21b): build the prospective file
        # WITHOUT touching the table, so an over-cap apply inserts nothing.
        from ember.store.types import Lesson  # local import: dataclass only

        live = await self._store.live_lessons()
        candidate = Lesson(
            id=0,
            text=text,
            evidence_json=json.dumps(evidence, sort_keys=True, separators=(",", ":")),
            dedup_key=key,
            created_at=now,
            last_reinforced_at=now,
            reinforced_count=reinforced_count,
            source_proposal_id=proposal_id,
            removed_at=None,
        )
        prospective = self.render(live + [candidate])
        size = len(prospective.encode("utf-8"))
        if size > self.LESSONS_CAP_BYTES:
            over = size - self.LESSONS_CAP_BYTES
            hint = (
                f"lessons.md would reach {size} bytes, over the {self.LESSONS_CAP_BYTES} "
                f"B cap by {over} B. Prune stale lessons first (`/proposal approve` a "
                "prune candidate from the digest), then re-approve this one."
            )
            _log.info("lesson apply refused (cap): proposal=%s over=%dB", proposal_id, over)
            return LessonSyncResult(
                committed=False,
                lessons_path=self._identity._lessons_path,
                git_commit_sha=None,
                error=hint,
            )

        lesson_id = await self._store.insert_lesson(
            text=text,
            evidence=evidence,
            dedup_key=key,
            created_at=now,
            reinforced_count=reinforced_count,
            source_proposal_id=proposal_id,
        )
        result = await self._materialize(proposal_id=proposal_id, action="apply")
        return _with(result, lesson_id=lesson_id)

    async def prune_lesson(
        self,
        *,
        lesson_id: int,
        proposal_id: int,
        now: datetime,
    ) -> LessonSyncResult:
        """Soft-delete a live lesson row (UoW-25 prune), then re-render + commit.
        A no-op (already-removed / unknown id) returns `committed=False` with a
        note — nothing is written."""
        removed = await self._store.soft_delete_lesson(lesson_id, now=now)
        if removed is None:
            return LessonSyncResult(
                committed=False,
                lessons_path=self._identity._lessons_path,
                git_commit_sha=None,
                error=f"lesson {lesson_id} is unknown or already pruned.",
                lesson_id=lesson_id,
            )
        result = await self._materialize(proposal_id=proposal_id, action="prune")
        return _with(result, lesson_id=lesson_id)

    async def _materialize(self, *, proposal_id: int, action: str) -> LessonSyncResult:
        """Re-render lessons.md from the live rows and git-commit. The table has
        already been mutated; this reflects it to the file. An empty render
        (no live lessons left) writes an empty file so the system block reverts
        to byte-identical soul+profile (TLC-12)."""
        lessons_path = self._identity._lessons_path
        live = await self._store.live_lessons()
        content = self.render(live)
        previous = self._identity.read_lessons()
        self._identity.write_lessons(content)
        body = (
            f"ember: lessons {action} from proposal #{proposal_id}\n\nproposal_id: {proposal_id}\n"
        )
        sha = self._identity.git_commit_lessons(body=body)
        if sha is None:
            # Git failed (or nothing to commit). If the content actually changed,
            # roll the file back so file + table don't diverge silently.
            if content != previous:
                try:
                    self._identity.write_lessons(previous)
                except Exception:
                    _log.exception("rollback of lessons.md after git failure also failed")
            return LessonSyncResult(
                committed=False,
                lessons_path=lessons_path,
                git_commit_sha=None,
                error="git_failed",
            )
        return LessonSyncResult(
            committed=True, lessons_path=lessons_path, git_commit_sha=sha, error=None
        )


def _loads_evidence(evidence_json: str) -> dict[str, list[int]]:
    try:
        ev = json.loads(evidence_json)
    except (TypeError, ValueError):
        return {}
    return ev if isinstance(ev, dict) else {}


def _with(result: LessonSyncResult, **overrides: object) -> LessonSyncResult:
    from dataclasses import replace

    return replace(result, **overrides)  # type: ignore[arg-type]
