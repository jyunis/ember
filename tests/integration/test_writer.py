"""Profile writer (FC-8).

These tests exercise the contract surface of `ember.identity.writer`
without reading the implementation.

Surface under test:

- `CommitResult` dataclass with fields:
  `committed: bool`, `profile_path: Path`, `git_commit_sha: str | None`,
  `error: str | None`.
- `ProfileWriter.commit(proposed_text, *, conversation_id, source_message_id)`
  performs:
    1. write `profile.md` (atomic via temp+rename);
    2. `git add profile.md && git commit -m ...` in the identity dir.
- Per FC-8: file → git is "all-or-rolled-back". If git fails, the file
  content is restored.
- Commit message body references conversation_id and source_message_id
  (FC-8: "structured commit body: source conversation ID, originating
  message ID, and the diff context").
- No-op short-circuit: when `proposed_text` matches current,
  `committed=False, error=None` (no file write, no git commit).

The pre-2026-05-06 docs-MCP mirror surface (`docs_mcp` param,
`mirror_status` field, mirror error/disabled paths) was removed when
the mirror was deleted — it had no consumer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ember.identity.store import IdentityStore
from ember.identity.writer import (
    CommitResult,
    ProfileWriter,
)


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
async def identity(tmp_path: Path) -> IdentityStore:
    """A fresh git-initialized IdentityStore on a per-test directory."""
    s = IdentityStore(tmp_path / "identity")
    await s.ensure_initialized()
    return s


def _git_log_messages(repo: Path) -> list[str]:
    """Return all commit messages (full body) in the repo, newest first."""
    out = subprocess.run(
        ["git", "log", "--format=%B%x1f"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [chunk.strip() for chunk in out.split("\x1f") if chunk.strip()]


def _git_head_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _has_git() -> bool:
    try:
        subprocess.run(
            ["git", "--version"],
            check=True,
            capture_output=True,
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


_skip_no_git = pytest.mark.skipif(not _has_git(), reason="git not available")


# --- CommitResult shape ---------------------------------------------------


class TestCommitResultShape:
    def test_fields_exist(self, tmp_path: Path) -> None:
        r = CommitResult(
            committed=True,
            profile_path=tmp_path / "profile.md",
            git_commit_sha="abcdef",
            error=None,
        )
        assert r.committed is True
        assert r.profile_path == tmp_path / "profile.md"
        assert r.git_commit_sha == "abcdef"
        assert r.error is None

    def test_failure_shape(self, tmp_path: Path) -> None:
        r = CommitResult(
            committed=False,
            profile_path=tmp_path / "profile.md",
            git_commit_sha=None,
            error="git_failed",
        )
        assert r.committed is False
        assert r.git_commit_sha is None
        assert r.error == "git_failed"


# --- Happy path -----------------------------------------------------------


@_skip_no_git
class TestHappyPath:
    async def test_writes_proposed_text_to_profile_md(self, identity: IdentityStore) -> None:
        new_text = "# new profile\n\n- updated by ember\n"
        w = ProfileWriter(identity=identity)
        result = await w.commit(new_text, conversation_id=42, source_message_id=99)

        assert result.committed is True
        profile_path = identity._dir / IdentityStore.PROFILE_FILENAME
        assert profile_path.read_text() == new_text
        assert result.profile_path == profile_path

    async def test_returns_git_commit_sha(self, identity: IdentityStore) -> None:
        new_text = "# new profile\n"
        w = ProfileWriter(identity=identity)
        result = await w.commit(new_text, conversation_id=1, source_message_id=2)

        assert result.committed is True
        assert result.git_commit_sha is not None
        assert isinstance(result.git_commit_sha, str)
        head = _git_head_sha(identity._dir)
        assert (
            result.git_commit_sha == head
            or result.git_commit_sha.startswith(head)
            or head.startswith(result.git_commit_sha)
        )

    async def test_commit_body_references_conversation_and_message_ids(
        self, identity: IdentityStore
    ) -> None:
        """FC-8: commit body includes source conversation ID and
        originating message ID. Tests assert both ids appear somewhere
        in the most recent commit message; format is open."""
        w = ProfileWriter(identity=identity)
        await w.commit("# new\n", conversation_id=987654321, source_message_id=123456789)

        messages = _git_log_messages(identity._dir)
        head_msg = messages[0]
        assert "987654321" in head_msg, f"conversation_id missing from commit message: {head_msg!r}"
        assert "123456789" in head_msg, (
            f"source_message_id missing from commit message: {head_msg!r}"
        )

    async def test_two_commits_advance_head(self, identity: IdentityStore) -> None:
        """Sanity: each commit produces a new SHA."""
        w = ProfileWriter(identity=identity)

        r1 = await w.commit("# v1\n", conversation_id=1, source_message_id=10)
        r2 = await w.commit("# v2\n", conversation_id=1, source_message_id=11)

        assert r1.committed is True
        assert r2.committed is True
        assert r1.git_commit_sha != r2.git_commit_sha
        profile_path = identity._dir / IdentityStore.PROFILE_FILENAME
        assert profile_path.read_text() == "# v2\n"


# --- Git failure rolls back file -----------------------------------------


@_skip_no_git
class TestGitFailureRollsBack:
    async def test_git_failure_restores_file(
        self, identity: IdentityStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Force the writer's git step to fail; the on-disk profile.md
        must be restored to its pre-write content."""
        profile_path = identity._dir / IdentityStore.PROFILE_FILENAME
        original = profile_path.read_text()

        real_run = subprocess.run

        def patched_run(cmd, *a, **kw):  # type: ignore[no-untyped-def]
            if (
                isinstance(cmd, (list, tuple))
                and len(cmd) >= 2
                and cmd[0] == "git"
                and "commit" in cmd
            ):
                raise subprocess.CalledProcessError(
                    1, cmd, output=b"", stderr=b"simulated commit failure"
                )
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr(subprocess, "run", patched_run)

        w = ProfileWriter(identity=identity)
        new_text = "# would-be new content; should be rolled back\n"
        result = await w.commit(new_text, conversation_id=1, source_message_id=2)

        assert result.committed is False
        assert result.git_commit_sha is None
        assert result.error is not None

        # File observably restored to its pre-write content.
        assert profile_path.read_text() == original


# --- No-op short-circuit -------------------------------------------------


@_skip_no_git
class TestNoopShortCircuit:
    """Regression: when proposed_text matches profile.md byte-for-byte,
    the writer must NOT attempt `git commit` (which fails with 'nothing
    to commit'). It must return a clean no-op CommitResult with
    `committed=False, error=None` so callers can distinguish no-op from
    failure.

    Backstory: model called profile_update with the unchanged file, the
    user said yes to the gate, and the writer returned `error=git_failed`
    — confusing and wrong, since nothing was actually broken.
    """

    async def test_identical_content_returns_committed_false_error_none(
        self, identity: IdentityStore
    ) -> None:
        w = ProfileWriter(identity=identity)
        current = identity.read_profile()
        head_before = _git_head_sha(identity._dir)

        result = await w.commit(current, conversation_id=1, source_message_id=1)

        assert result.committed is False
        assert result.error is None
        assert result.git_commit_sha is None

        # Disk file unchanged AND no new git commit was created.
        assert identity.read_profile() == current
        assert _git_head_sha(identity._dir) == head_before

    async def test_distinct_content_after_noop_still_commits(self, identity: IdentityStore) -> None:
        # Defensive: a no-op call must not corrupt subsequent commits.
        w = ProfileWriter(identity=identity)

        current = identity.read_profile()
        noop = await w.commit(current, conversation_id=1, source_message_id=1)
        assert noop.committed is False and noop.error is None

        new_text = current + "\n- Democracy Now — https://example.org/rss\n"
        real = await w.commit(new_text, conversation_id=1, source_message_id=2)
        assert real.committed is True
        assert real.git_commit_sha is not None
        assert identity.read_profile() == new_text


# --- Atomicity (observable) ----------------------------------------------


@_skip_no_git
class TestAtomicity:
    async def test_concurrent_reads_during_commit_see_consistent_text(
        self, identity: IdentityStore
    ) -> None:
        """Weak atomicity smoke test: a sequence of (commit) calls leaves
        `profile.md` in a fully-written state — never partial."""
        w = ProfileWriter(identity=identity)

        async def go(idx: int) -> str:
            text = f"# version {idx}\n" + ("body line\n" * 50)
            await w.commit(text, conversation_id=1, source_message_id=idx)
            return text

        for i in range(5):
            expected = await go(i)
            on_disk = (identity._dir / IdentityStore.PROFILE_FILENAME).read_text()
            assert on_disk == expected
