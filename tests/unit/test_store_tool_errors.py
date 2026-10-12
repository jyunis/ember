"""`Store.tool_errors_in_window` — silent tool-error evidence read (doc #36
Decisions #26).

- Only tool calls with `status == "error"` on assistant rows are returned;
  ok / status-less calls and non-assistant rows are not.
- Each row is paired with the nearest EARLIER user message in the same
  conversation (the evidence pointer, TLC-20); None when there is none.
- The window is `[since, until)` on the assistant row's `created_at`.
- A row with several failing calls yields one entry per failing call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ember.providers.base import ToolCall
from ember.store.repo import Store


@pytest.fixture
async def store(tmp_path: Path):
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


def _call(name: str, status: str | None, **args) -> ToolCall:
    return ToolCall(id=f"t-{name}-{status}", name=name, args=args, status=status)


def _window() -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    return now - timedelta(hours=1), now + timedelta(hours=1)


class TestToolErrorsInWindow:
    async def test_error_calls_paired_with_preceding_user_message(self, store: Store) -> None:
        conv = await store.get_or_create_conversation("discord", "t1")
        uid = await store.append_message(conv.id, "user", "crop this photo")
        aid = await store.append_message(
            conv.id,
            "assistant",
            "trying",
            tool_calls=[
                _call("Read", "ok", file_path="/x.jpg"),
                _call("Bash", "error", command="python3 -c 'from PIL import Image'"),
                _call("Bash", "error", command="pip install pillow"),
            ],
        )
        rows = await store.tool_errors_in_window(*_window())
        assert [(r.message_id, r.tool, r.user_message_id) for r in rows] == [
            (aid, "Bash", uid),
            (aid, "Bash", uid),
        ]
        assert rows[0].args["command"].startswith("python3")
        assert rows[1].args["command"] == "pip install pillow"
        assert rows[0].conversation_id == conv.id

    async def test_ok_and_statusless_calls_excluded(self, store: Store) -> None:
        conv = await store.get_or_create_conversation("discord", "t2")
        await store.append_message(conv.id, "user", "hi")
        await store.append_message(
            conv.id, "assistant", "fine", tool_calls=[_call("web_fetch", "ok", url="u")]
        )
        await store.append_message(
            conv.id, "assistant", "fine", tool_calls=[_call("web_fetch", None, url="u")]
        )
        await store.append_message(conv.id, "assistant", "no tools at all")
        assert await store.tool_errors_in_window(*_window()) == []

    async def test_no_earlier_user_turn_gives_none_pointer(self, store: Store) -> None:
        conv = await store.get_or_create_conversation("discord", "t3")
        await store.append_message(
            conv.id, "assistant", "scheduled", tool_calls=[_call("vault_list", "error")]
        )
        # A LATER user message must not be picked up as the pointer.
        await store.append_message(conv.id, "user", "later")
        rows = await store.tool_errors_in_window(*_window())
        assert len(rows) == 1
        assert rows[0].user_message_id is None

    async def test_pointer_is_nearest_user_turn_in_same_conversation(self, store: Store) -> None:
        other = await store.get_or_create_conversation("discord", "other")
        conv = await store.get_or_create_conversation("discord", "t4")
        await store.append_message(conv.id, "user", "first")
        nearest = await store.append_message(conv.id, "user", "second")
        await store.append_message(other.id, "user", "unrelated, newer id")
        await store.append_message(
            conv.id, "assistant", "x", tool_calls=[_call("tiller_query", "error", sql="bad")]
        )
        rows = await store.tool_errors_in_window(*_window())
        assert [r.user_message_id for r in rows] == [nearest]

    async def test_respects_window(self, store: Store) -> None:
        conv = await store.get_or_create_conversation("discord", "t5")
        await store.append_message(conv.id, "user", "hi")
        await store.append_message(
            conv.id, "assistant", "x", tool_calls=[_call("Bash", "error", command="false")]
        )
        past = datetime.now(UTC) - timedelta(days=2)
        assert await store.tool_errors_in_window(past - timedelta(hours=1), past) == []
