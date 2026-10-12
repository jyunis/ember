"""`/memory_revert [n]` — undo the last N agent-written memory entries.

Per architecture decision #2: agent may write memories autonomously,
but every write is announced and reversible.

Reversion uses an in-process pointer list (lost on container restart —
acceptable for a "oops" command).
"""

from __future__ import annotations

import logging
from collections import deque

from ember.commands.base import CommandContext, CommandOption
from ember.tools.registry import Registry

_log = logging.getLogger("ember.commands.memory")


class MemoryRevertTracker:
    """Bounded LIFO of (memory_id, conversation_key) tuples."""

    def __init__(self, max_size: int = 256) -> None:
        self._entries: deque[tuple[int, str]] = deque(maxlen=max_size)

    def record(self, memory_id: int, conversation_key: str) -> None:
        self._entries.append((int(memory_id), conversation_key))

    def take_last(self, n: int, conversation_key: str | None = None) -> list[int]:
        if n <= 0:
            return []
        taken: list[int] = []
        # Walk from the right; collect matching entries, remove them.
        new_entries: deque[tuple[int, str]] = deque(maxlen=self._entries.maxlen)
        # Materialize to list for indexed iteration; rebuild the deque
        # without the taken entries.
        items = list(self._entries)
        # Iterate rightmost-first.
        kept_reversed: list[tuple[int, str]] = []
        for entry in reversed(items):
            if len(taken) < n and (conversation_key is None or entry[1] == conversation_key):
                taken.append(entry[0])
            else:
                kept_reversed.append(entry)
        # Rebuild in original order.
        for entry in reversed(kept_reversed):
            new_entries.append(entry)
        self._entries = new_entries
        return taken

    def __len__(self) -> int:
        return len(self._entries)


_OUT_OF_THREAD = "ℹ️ `/memory_revert` must be invoked in a conversation thread."
_NOTHING_TO_REVERT = "Nothing to revert in this thread."
_DELETE_TOOL_NAMES = ("memory__delete_memory", "memory__delete")


class MemoryRevertCommand:
    """Slash command body."""

    name: str = "memory_revert"
    description: str = "Undo the last N agent-written memory entries (in this session)."
    options: list[CommandOption] = [
        CommandOption(
            name="n",
            description="How many recent agent-written memories to undo (default 1).",
            default="1",
        ),
    ]

    def __init__(self, *, tracker: MemoryRevertTracker, tool_registry: Registry) -> None:
        self._tracker = tracker
        self._tool_registry = tool_registry

    async def run(self, ctx: CommandContext) -> str:
        if ctx.conversation_key is None:
            return _OUT_OF_THREAD

        try:
            n = int(ctx.args.get("n") or "1")
        except (TypeError, ValueError):
            return "Usage: `/memory_revert n:<integer>`"
        if n <= 0:
            return "Usage: `/memory_revert n:<positive integer>`"

        ids = self._tracker.take_last(n, ctx.conversation_key)
        if not ids:
            return _NOTHING_TO_REVERT

        delete_tool_name = self._find_delete_tool()
        if delete_tool_name is None:
            return (
                "❌ memory delete tool not registered; "
                f"reverted entries (couldn't actually delete): {ids}"
            )

        succeeded: list[int] = []
        failed: list[tuple[int, str]] = []
        for mid in ids:
            try:
                await self._tool_registry.call_tool(delete_tool_name, {"id": mid})
                succeeded.append(mid)
            except Exception as e:
                _log.warning("memory revert failed for id=%s: %s", mid, e)
                failed.append((mid, str(e)))

        lines = [f"✅ reverted {len(succeeded)} memory entr{'y' if len(succeeded) == 1 else 'ies'}"]
        if succeeded:
            lines.append(f"  ids: {succeeded}")
        if failed:
            lines.append(f"⚠️ failed: {failed}")
        return "\n".join(lines)

    def _find_delete_tool(self) -> str | None:
        names = self._tool_registry.names()
        for candidate in _DELETE_TOOL_NAMES:
            if candidate in names:
                return candidate
        # Fall back: any name containing "delete_memory" under memory namespace.
        for name in names:
            if name.startswith("memory__") and "delete" in name.lower():
                return name
        return None
