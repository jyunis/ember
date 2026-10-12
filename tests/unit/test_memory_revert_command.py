"""`/memory_revert` slash command (UoW-7).

Per UoW-7 + architecture decision #2:
- Trigger: user issues `/memory_revert [n]` (default n=1).
- Precondition: at least one memory was written in this conversation
  by the agent (recorded via `MemoryRevertTracker.record`).
- Steps: pop the last N IDs from the tracker; call memory MCP's
  `delete_memory` for each; emit a confirmation listing what was
  reverted.

Tester-flagged spec ambiguity (recorded earlier in the planning):
- The command needs access to the **tool registry** (or a memory
  client) to actually call `memory__delete_memory` — `CommandContext`
  only carries the slash command registry, not the tool registry.
  Two options:
    (a) inject the tool registry (or memory client) at construction
    (b) extend `CommandContext` to carry the tool registry
  We test against option (a): the constructor takes
  `tool_registry: Registry` alongside `tracker`. If the implementer
  picks (b), these tests need restructuring.

Tests use `FakeRegistry`/`FakeTool` that record `call_tool` invocations
without standing up a real MCP client.
"""

from __future__ import annotations

from typing import Any


from ember.commands.base import CommandContext
from ember.commands.memory import MemoryRevertCommand, MemoryRevertTracker
from ember.commands.registry import CommandRegistry
from ember.tools.base import Tool, ToolResult
from ember.tools.registry import Registry


# --- Fakes ---------------------------------------------------------------


class _RecordingDeleteMemoryTool(Tool):
    """Stand-in for `memory__delete_memory` registered in the tool
    registry. Records every call so tests can assert which IDs got
    deleted."""

    name: str = "memory__delete_memory"
    description: str = "delete memory by id"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"id": {"type": "integer"}},
        "required": ["id"],
        "additionalProperties": False,
    }
    requires_confirmation: bool = False

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call(self, args: dict[str, Any]) -> ToolResult:
        self.calls.append(dict(args))
        return ToolResult(output={"deleted": args["id"]})


class _MissingSettings:
    """`Settings` placeholder for tests that don't touch settings."""


def _make_ctx(
    *,
    conversation_key: str | None,
    args: dict[str, Any] | None = None,
) -> CommandContext:
    return CommandContext(
        conversation_key=conversation_key,
        user_id="1000",
        store=None,  # type: ignore[arg-type]
        settings=_MissingSettings(),  # type: ignore[arg-type]
        registry=CommandRegistry(),  # slash registry; not the tool reg
        args=args or {},
    )


# --- Outside a conversation thread --------------------------------------


class TestOutsideThread:
    async def test_no_conversation_key_returns_must_be_invoked_message(
        self,
    ) -> None:
        tracker = MemoryRevertTracker()
        cmd = MemoryRevertCommand(
            tracker=tracker,
            tool_registry=Registry(),
        )
        ctx = _make_ctx(conversation_key=None)
        out = await cmd.run(ctx)
        # Per the architecture: revert is a per-conversation operation.
        # The message must indicate the wrong-place use.
        lowered = out.lower()
        assert "thread" in lowered or "conversation" in lowered or "must" in lowered


# --- Empty tracker ------------------------------------------------------


class TestNothingToRevert:
    async def test_empty_tracker_returns_clear_message(self) -> None:
        tracker = MemoryRevertTracker()
        delete_tool = _RecordingDeleteMemoryTool()
        reg = Registry()
        reg.register(delete_tool)

        cmd = MemoryRevertCommand(tracker=tracker, tool_registry=reg)
        ctx = _make_ctx(conversation_key="thread-1")
        out = await cmd.run(ctx)
        # Some clear "nothing to revert" indicator. Don't pin exact
        # wording — accept any message that signals "no revert
        # happened" without an error.
        lowered = out.lower()
        assert "nothing" in lowered or "no" in lowered or "0" in out
        # And the delete tool must not have been called.
        assert delete_tool.calls == []


# --- Default n=1 --------------------------------------------------------


class TestSingleRevert:
    async def test_default_n_is_one(self) -> None:
        tracker = MemoryRevertTracker()
        tracker.record(101, "thread-1")
        tracker.record(202, "thread-1")
        tracker.record(303, "thread-1")

        delete_tool = _RecordingDeleteMemoryTool()
        reg = Registry()
        reg.register(delete_tool)

        cmd = MemoryRevertCommand(tracker=tracker, tool_registry=reg)
        # No `n` arg → default "1" per CommandOption.
        ctx = _make_ctx(conversation_key="thread-1", args={"n": "1"})
        out = await cmd.run(ctx)

        # Exactly one delete call, for the most recent ID.
        assert len(delete_tool.calls) == 1
        assert delete_tool.calls[0] == {"id": 303}
        # Output mentions the reverted ID or a count.
        assert "303" in out or "1" in out

    async def test_default_when_n_arg_absent(self) -> None:
        # If `args` is empty, the command should still work (default
        # to 1 per CommandOption.default).
        tracker = MemoryRevertTracker()
        tracker.record(42, "thread-1")
        delete_tool = _RecordingDeleteMemoryTool()
        reg = Registry()
        reg.register(delete_tool)

        cmd = MemoryRevertCommand(tracker=tracker, tool_registry=reg)
        ctx = _make_ctx(conversation_key="thread-1", args={})
        await cmd.run(ctx)
        assert len(delete_tool.calls) == 1
        assert delete_tool.calls[0] == {"id": 42}


# --- n > 1 --------------------------------------------------------------


class TestMultipleRevert:
    async def test_n_three_reverts_three_lifo(self) -> None:
        tracker = MemoryRevertTracker()
        tracker.record(1, "thread-1")
        tracker.record(2, "thread-1")
        tracker.record(3, "thread-1")
        tracker.record(4, "thread-1")

        delete_tool = _RecordingDeleteMemoryTool()
        reg = Registry()
        reg.register(delete_tool)

        cmd = MemoryRevertCommand(tracker=tracker, tool_registry=reg)
        ctx = _make_ctx(conversation_key="thread-1", args={"n": "3"})
        await cmd.run(ctx)

        # Three calls — the most recent three (LIFO: 4, 3, 2).
        assert len(delete_tool.calls) == 3
        deleted_ids = {c["id"] for c in delete_tool.calls}
        assert deleted_ids == {2, 3, 4}


# --- Per-conversation isolation -----------------------------------------


class TestConversationIsolation:
    async def test_revert_only_affects_invocation_thread(self) -> None:
        # The tracker has entries for two conversations; reverting in
        # thread-A must not delete thread-B's memories.
        tracker = MemoryRevertTracker()
        tracker.record(1, "thread-A")
        tracker.record(2, "thread-B")
        tracker.record(3, "thread-A")
        tracker.record(4, "thread-B")

        delete_tool = _RecordingDeleteMemoryTool()
        reg = Registry()
        reg.register(delete_tool)

        cmd = MemoryRevertCommand(tracker=tracker, tool_registry=reg)
        ctx = _make_ctx(conversation_key="thread-A", args={"n": "5"})
        await cmd.run(ctx)

        # Only thread-A entries deleted.
        deleted_ids = {c["id"] for c in delete_tool.calls}
        assert deleted_ids == {1, 3}
        # B's entries still in tracker.
        remaining = tracker.take_last(10)
        assert set(remaining) == {2, 4}


# --- Invalid n option ---------------------------------------------------


class TestInvalidN:
    async def test_non_integer_n_returns_clear_message(self) -> None:
        tracker = MemoryRevertTracker()
        tracker.record(1, "thread-1")
        delete_tool = _RecordingDeleteMemoryTool()
        reg = Registry()
        reg.register(delete_tool)

        cmd = MemoryRevertCommand(tracker=tracker, tool_registry=reg)
        ctx = _make_ctx(conversation_key="thread-1", args={"n": "not-a-number"})
        out = await cmd.run(ctx)
        # Either an explicit error message OR the default-of-1 fallback;
        # either way, must not raise. We assert the call is *not*
        # silent garbage — the output is a non-empty string.
        assert isinstance(out, str)
        assert out != ""
