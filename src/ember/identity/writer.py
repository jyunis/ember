"""Profile writer (FC-8).

Persists a confirmed profile update:
  1. atomic write of `profile.md` (temp + rename);
  2. `git add profile.md && git commit -m <structured body>`.

If git fails the file is rolled back (all-or-nothing).

Earlier versions also mirrored the file to the documents MCP, but that
mirror had no consumer — the profile is read fresh from disk into the
system block every turn, and git provides revision history. Removed in
2026-05-06 to drop a write-only failure path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ember.identity.store import IdentityStore

_log = logging.getLogger("ember.identity.writer")


@dataclass(frozen=True, slots=True)
class CommitResult:
    """Outcome of a `ProfileWriter.commit` call.

    `committed` is True iff the file write + git commit succeeded.
    `error` is set when `committed` is False and a real failure occurred;
    `committed=False` with `error=None` is the no-op signal (proposed
    text matched current byte-for-byte).
    """

    committed: bool
    profile_path: Path
    git_commit_sha: str | None
    error: str | None


class ProfileWriter:
    """Owns the file → git commit flow."""

    def __init__(
        self,
        *,
        identity: IdentityStore,
    ) -> None:
        self._identity = identity

    async def commit(
        self,
        proposed_text: str,
        *,
        conversation_id: int | None = None,
        source_message_id: int | None = None,
        proposal_id: int | None = None,
    ) -> CommitResult:
        """Persist a confirmed profile update: atomic write + git commit, or a
        clean no-op when the proposed text matches the file byte-for-byte.

        The commit body identifies the source: a live conversation
        (`conversation_id` + `source_message_id`, the interactive
        `profile_update` path) or an approved retrospective proposal
        (`proposal_id`, the v12.5 applier path — so `git log` references the
        proposal id per FC-23 acceptance). Passing `proposal_id` takes
        precedence for the body."""
        profile_path = self._identity._profile_path

        # 1. Snapshot current text for rollback on git failure.
        try:
            current = self._identity.read_profile()
        except FileNotFoundError:
            current = ""

        # Short-circuit: identical content. The model called
        # profile_update but its `content` arg matches the current file
        # byte-for-byte — nothing to write. Return a clean no-op result
        # instead of writing the file, attempting `git commit` (which
        # fails with 'nothing to commit'), and reporting git_failed.
        # `committed=False, error=None` is the no-op signal; callers
        # should treat error=None as not-an-error.
        if proposed_text == current:
            return CommitResult(
                committed=False,
                profile_path=profile_path,
                git_commit_sha=None,
                error=None,
            )

        # 2. Atomic file write.
        self._identity.write_profile(proposed_text)

        # 3. Git commit.
        if proposal_id is not None:
            body = (
                f"ember: profile update from proposal #{proposal_id}\n"
                f"\n"
                f"proposal_id: {proposal_id}\n"
            )
        else:
            body = (
                f"ember: profile update from conversation #{conversation_id}\n"
                f"\n"
                f"conversation_id: {conversation_id}\n"
                f"source_message_id: {source_message_id}\n"
            )
        sha = self._identity.git_commit_profile(body=body)
        if sha is None:
            # Roll the file back to the pre-write content.
            try:
                self._identity.write_profile(current)
            except Exception:
                _log.exception("rollback of profile.md after git failure also failed")
            return CommitResult(
                committed=False,
                profile_path=profile_path,
                git_commit_sha=None,
                error="git_failed",
            )

        return CommitResult(
            committed=True,
            profile_path=profile_path,
            git_commit_sha=sha,
            error=None,
        )
