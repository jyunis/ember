"""Profile update tool.

Wraps `identity.ProfileWriter` so the agent can directly modify
`profile.md` in response to explicit user requests, distinct from the
auto-proposer flow (UoW-8) which gates on classifier-detected
self-revealing turns.

Usage shape (LLM-side):
    1. Read the user's current profile from the system block.
    2. Construct the FULL updated profile.md content (preserving any
       sections the user didn't ask to change).
    3. Call `profile_update(content="<full updated text>")`.

The tool is `requires_confirmation=True` (TLC-3) — destructive in the
sense that it auto-commits to the identity git repo. Reversible via
`git revert` on the host. For the auto-proposer's classifier-gated
flow, see `identity.proposer`.
"""

from __future__ import annotations

import difflib
import logging
from typing import Any

from ember.identity.store import IdentityStore
from ember.identity.writer import ProfileWriter
from ember.tools.base import ToolResult

_log = logging.getLogger("ember.tools.profile")

# Sentinel ids for tool-driven commits — there's no conversation context
# threaded into Tool.call(args) today. Git commit body will reference
# "conversation #0" for tool-driven updates; real proposer-driven commits
# (UoW-8) carry the actual ids. Refactor if cross-referencing becomes
# load-bearing.
_TOOL_DRIVEN_CONVERSATION_ID = 0
_TOOL_DRIVEN_MESSAGE_ID = 0


class ProfileReadTool:
    """Read profile.md verbatim. The profile is already in the system
    block every turn; this tool makes the access path explicit when
    the user asks 'what's in my profile' or similar."""

    name: str = "profile_read"
    description: str = "Return the full current contents of the user's profile.md."
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, *, identity: IdentityStore) -> None:
        self._identity = identity

    async def call(self, args: dict[str, Any]) -> ToolResult:
        try:
            content = self._identity.read_profile()
        except FileNotFoundError:
            return ToolResult(
                output={"content": "", "error": "profile.md not found"}, is_error=True
            )
        return ToolResult(output={"content": content})


class ProfileUpdateTool:
    """Direct-modify tool for profile.md.

    Distinct from the auto-proposer (which classifies turns and
    proposes diffs at end-of-turn). Use this when the user explicitly
    asks to add/remove/change something in their profile.
    """

    name: str = "profile_update"
    description: str = (
        "Modify the user's profile.md directly. Required steps in order: "
        "(1) read current content via profile_read (or from the system "
        "block); (2) APPLY the user's requested change to that content "
        "in your head; (3) pass the FULL modified file as `content`. "
        "The host computes a unified diff between current and your "
        "`content` arg and shows it to the user — if the diff is "
        "'<no changes>' you forgot step (2). CALL THIS TOOL when the "
        "user asks for a change — do not ask permission first. The "
        "system gates the call and shows the user a confirmation prompt "
        "automatically; the actual write only happens after the user "
        "replies 'yes'. Auto-commits to the identity git repo and "
        "mirrors to docs MCP."
    )
    requires_confirmation: bool = True
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "FULL new profile.md content. Replaces the file entirely.",
            },
        },
        "required": ["content"],
    }

    def __init__(self, *, writer: ProfileWriter, identity: IdentityStore) -> None:
        self._writer = writer
        self._identity = identity

    def summarize_action(self, args: dict[str, Any]) -> str:
        """Render a unified diff between the current profile.md and the
        proposed new content, so the 🔒 confirm prompt shows the actual
        change rather than the first 200 chars of the unchanged file.
        Called by the registry's confirmation gate (TLC-3)."""
        new_content = args.get("content")
        if not isinstance(new_content, str):
            return "profile_update: <missing or non-string content>"
        try:
            current = self._identity.read_profile()
        except FileNotFoundError:
            current = ""
        diff_lines = list(
            difflib.unified_diff(
                current.splitlines(),
                new_content.splitlines(),
                fromfile="profile.md (current)",
                tofile="profile.md (proposed)",
                n=1,
                lineterm="",
            )
        )
        if not diff_lines:
            return "profile_update: <no changes>"
        # Never capped: the user is approving exactly this change, so
        # every diff line must be visible. Long diffs span several
        # Discord messages via the channel's fence-aware chunker.
        return "profile_update:\n" + "\n".join(diff_lines)

    async def call(self, args: dict[str, Any]) -> ToolResult:
        content = args["content"]
        if not isinstance(content, str) or not content.strip():
            return ToolResult(
                output={"committed": False, "error": "empty content"},
                is_error=True,
            )
        # Snapshot the diff BEFORE the writer mutates profile.md so the
        # success reply can echo what was actually committed. (After
        # commit, summarize_action would compare new vs new == empty.)
        pre_diff = self.summarize_action(args)
        result = await self._writer.commit(
            content,
            conversation_id=_TOOL_DRIVEN_CONVERSATION_ID,
            source_message_id=_TOOL_DRIVEN_MESSAGE_ID,
        )
        # No-op signal from writer: committed=False AND error=None means
        # the proposed content matched the current file byte-for-byte.
        # Surface as not-an-error with a clear message so the model can
        # see what happened and the user-facing reply isn't a red ❌.
        is_noop = (not result.committed) and (result.error is None)
        output: dict[str, Any] = {
            "committed": result.committed,
            "git_commit_sha": result.git_commit_sha,
            "error": result.error,
            # Snapshot of what changed; surfaced in the user-facing
            # confirmation reply so the user sees what was committed.
            "diff_summary": pre_diff,
        }
        if is_noop:
            output["noop"] = True
            output["message"] = (
                "no changes — the `content` arg matched the current "
                "profile.md byte-for-byte. If the user asked for a "
                "change, you forgot to apply it: re-read profile_read "
                "and modify that content before sending again."
            )
        return ToolResult(
            output=output,
            is_error=(not result.committed) and (result.error is not None),
        )
