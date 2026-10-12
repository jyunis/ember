"""ProfileUpdateTool — `summarize_action` diff rendering.

The 🔒 confirm prompt (TLC-3) shows the tool's `summarize_action(args)`
output to the user. profile_update overrides this so the user sees a
unified diff between current profile.md and the proposed new content,
rather than the first 200 chars of `args["content"]` (which is the
unchanged file header — useless for a y/n decision).

These tests pin the diff-summary contract directly; the registry-level
gate-and-render path is covered elsewhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ember.identity.store import IdentityStore
from ember.identity.writer import CommitResult
from ember.tools.profile import ProfileUpdateTool


class _StubWriter:
    """Stand-in for ProfileWriter — summarize_action never invokes it."""

    async def commit(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("summarize_action must not call writer.commit")


class _NoopWriter:
    """Returns a no-op CommitResult (committed=False, error=None) — the
    signal the writer emits when proposed_text matches current."""

    def __init__(self, profile_path: Path) -> None:
        self._profile_path = profile_path

    async def commit(self, *args: Any, **kwargs: Any) -> CommitResult:
        return CommitResult(
            committed=False,
            profile_path=self._profile_path,
            git_commit_sha=None,
            error=None,
        )


class _GitFailedWriter:
    """Returns a real failure (committed=False, error='git_failed')."""

    def __init__(self, profile_path: Path) -> None:
        self._profile_path = profile_path

    async def commit(self, *args: Any, **kwargs: Any) -> CommitResult:
        return CommitResult(
            committed=False,
            profile_path=self._profile_path,
            git_commit_sha=None,
            error="git_failed",
        )


@pytest.fixture
async def identity(tmp_path: Path) -> IdentityStore:
    store = IdentityStore(tmp_path / "identity")
    await store.ensure_initialized()
    return store


@pytest.fixture
def tool(identity: IdentityStore) -> ProfileUpdateTool:
    return ProfileUpdateTool(writer=_StubWriter(), identity=identity)  # type: ignore[arg-type]


class TestSummarizeActionDiff:
    def test_added_line_appears_with_plus_marker(
        self,
        tool: ProfileUpdateTool,
        identity: IdentityStore,
    ) -> None:
        # Append one new line — the canonical "add a news source" case.
        current = identity.read_profile()
        proposed = current + "\n- Democracy Now (https://www.democracynow.org/democracynow.rss)\n"
        summary = tool.summarize_action({"content": proposed})
        assert summary.startswith("profile_update:")
        assert "+- Democracy Now" in summary
        # And the entire unchanged head of profile.md must NOT be dumped.
        # n=1 context means at most a couple lines of surrounding content.
        assert summary.count("\n") < 12

    def test_no_changes_returns_no_changes_summary(
        self,
        tool: ProfileUpdateTool,
        identity: IdentityStore,
    ) -> None:
        current = identity.read_profile()
        summary = tool.summarize_action({"content": current})
        assert "no changes" in summary.lower()

    def test_missing_content_arg_does_not_raise(
        self,
        tool: ProfileUpdateTool,
    ) -> None:
        # Defensive: registry validates the schema before reaching the
        # gate, but summarize_action shouldn't crash if invoked with bad
        # args (registry catches and falls back to generic render).
        summary = tool.summarize_action({})
        assert "missing" in summary.lower() or "non-string" in summary.lower()

    def test_full_rewrite_shows_every_diff_line(
        self,
        tool: ProfileUpdateTool,
        identity: IdentityStore,
    ) -> None:
        # A full rewrite produces a long diff. It must NOT be capped —
        # the user is approving the whole change, so every added line
        # has to be visible in the 🔒 prompt (the channel chunks it).
        proposed = "\n".join(f"line {i}" for i in range(100))
        summary = tool.summarize_action({"content": proposed})
        assert summary.startswith("profile_update:")
        assert "more diff lines" not in summary
        for i in range(100):
            assert f"+line {i}\n" in summary + "\n"

    def test_uses_profile_update_label_not_full_args(
        self,
        tool: ProfileUpdateTool,
        identity: IdentityStore,
    ) -> None:
        # Regression: pre-fix, the 🔒 line was `profile_update({"content":
        # "# User profile..."})` — useless. The new summary must lead with
        # `profile_update:` and contain diff markers, not the raw args
        # JSON.
        proposed = identity.read_profile() + "\n- New thing\n"
        summary = tool.summarize_action({"content": proposed})
        assert '{"content"' not in summary
        assert "---" in summary or "+++" in summary


class TestCallNoopHandling:
    """Regression: when the writer returns its no-op signal
    (committed=False, error=None), ProfileUpdateTool.call must surface
    it as not-an-error with a `noop=True` flag and a `message` field
    explaining what happened. Real git failures stay as errors."""

    async def test_noop_writer_yields_non_error_result_with_message(
        self, identity: IdentityStore
    ) -> None:
        writer = _NoopWriter(identity._profile_path)
        tool = ProfileUpdateTool(writer=writer, identity=identity)  # type: ignore[arg-type]
        result = await tool.call({"content": "anything"})
        assert result.is_error is False
        assert isinstance(result.output, dict)
        assert result.output.get("noop") is True
        assert "no changes" in result.output.get("message", "").lower()

    async def test_real_git_failure_still_flagged_as_error(self, identity: IdentityStore) -> None:
        writer = _GitFailedWriter(identity._profile_path)
        tool = ProfileUpdateTool(writer=writer, identity=identity)  # type: ignore[arg-type]
        result = await tool.call({"content": "anything"})
        assert result.is_error is True
        assert isinstance(result.output, dict)
        assert result.output.get("error") == "git_failed"
        # Real failures don't get a noop flag.
        assert "noop" not in result.output


class TestCallSnapshotsDiff:
    """The success reply renders the diff that was committed; tool.call
    must snapshot it BEFORE delegating to writer.commit (afterwards,
    current==proposed and the diff would be empty)."""

    async def test_diff_summary_present_on_noop(self, identity: IdentityStore) -> None:
        writer = _NoopWriter(identity._profile_path)
        tool = ProfileUpdateTool(writer=writer, identity=identity)  # type: ignore[arg-type]
        result = await tool.call({"content": identity.read_profile()})
        assert isinstance(result.output, dict)
        # No-op path still snapshots; diff is "<no changes>".
        assert "diff_summary" in result.output
        assert "no changes" in result.output["diff_summary"].lower()
