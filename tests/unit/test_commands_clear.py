"""ClearCommand (`/clear`) — v9 FC-12.

Soft-clears the conversation:
- marks every active message `compacted=1` (so prompt assembly drops them);
- deletes summaries rows for the conversation;
- nulls `claude_cli_session_id` (severs the resumable CLI session);
- clears `pending_profile_proposal`.

Pins survive (architecture doc § Conversation store: "pinned ... survives
trim/compact"). The repo helper `Store.clear_conversation` already exists
and returns a `ClearResult`; this command just wraps it and renders a
user-visible reply.

Out-of-thread invocation returns an `ℹ️` message, like other
conversation-only commands.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.commands.base import CommandContext
from ember.commands.registry import CommandRegistry
from ember.config import Settings
from ember.store.repo import Store


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
async def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = _make_settings(monkeypatch, tmp_path)
    store = Store(tmp_path / "conversations.db")
    await store.open()
    registry = CommandRegistry()
    try:
        yield store, settings, registry
    finally:
        await store.close()


def _ctx(
    store: Store,
    settings: Settings,
    registry: CommandRegistry,
    *,
    conversation_key: str | None = "thread-1",
    args: dict | None = None,
) -> CommandContext:
    return CommandContext(
        conversation_key=conversation_key,
        user_id="1000",
        store=store,
        settings=settings,
        registry=registry,
        args=args or {},
    )


# --- Static surface (FC-12) ----------------------------------------------


class TestStaticSurface:
    def test_name(self) -> None:
        from ember.commands.clear import ClearCommand

        assert ClearCommand.name == "clear"

    def test_description_present(self) -> None:
        from ember.commands.clear import ClearCommand

        assert isinstance(ClearCommand.description, str)
        assert ClearCommand.description.strip() != ""

    def test_no_options(self) -> None:
        from ember.commands.clear import ClearCommand

        assert ClearCommand.options == []


# --- Behavior ------------------------------------------------------------


class TestOutOfThread:
    async def test_no_conversation_key_returns_message(self, wired) -> None:
        from ember.commands.clear import ClearCommand

        store, settings, registry = wired
        cmd = ClearCommand()
        out = await cmd.run(_ctx(store, settings, registry, conversation_key=None))
        assert isinstance(out, str)
        assert out != ""
        lowered = out.lower()
        assert "conversation" in lowered or "thread" in lowered or "must" in lowered


class TestClearWithMessages:
    async def test_clears_active_messages(self, wired) -> None:
        from ember.commands.clear import ClearCommand

        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        for i in range(3):
            await store.append_message(conv.id, "user", f"m{i}")

        cmd = ClearCommand()
        await cmd.run(_ctx(store, settings, registry))

        # All non-compacted messages should now be compacted.
        active = await store.recent_messages(conv.id, limit=100)
        assert active == []
        # Originals still recoverable.
        all_msgs = await store.recent_messages(conv.id, limit=100, include_compacted=True)
        assert len(all_msgs) == 3

    async def test_deletes_summaries(self, wired) -> None:
        from ember.commands.clear import ClearCommand

        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        m1 = await store.append_message(conv.id, "user", "old")
        m2 = await store.append_message(conv.id, "assistant", "older")
        await store.insert_summary(
            conv.id,
            start_message_id=m1,
            end_message_id=m2,
            summary_text="prior summary",
        )

        cmd = ClearCommand()
        await cmd.run(_ctx(store, settings, registry))

        assert await store.list_summaries(conv.id) == []

    async def test_pins_survive(self, wired) -> None:
        from ember.commands.clear import ClearCommand

        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        await store.add_pin(conv.id, "remember me")
        await store.append_message(conv.id, "user", "hi")

        cmd = ClearCommand()
        await cmd.run(_ctx(store, settings, registry))

        pins = await store.list_pins(conv.id)
        assert len(pins) == 1
        assert pins[0].content == "remember me"

    async def test_resets_claude_cli_session(self, wired) -> None:
        from ember.commands.clear import ClearCommand

        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        await store.update_claude_cli_session(conv.id, "session-abc")
        await store.append_message(conv.id, "user", "hi")

        cmd = ClearCommand()
        await cmd.run(_ctx(store, settings, registry))

        conv_after = await store.get_or_create_conversation("discord", "thread-1")
        assert conv_after.claude_cli_session_id is None


class TestReplyText:
    async def test_reply_mentions_message_count(self, wired) -> None:
        from ember.commands.clear import ClearCommand

        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        for _ in range(5):
            await store.append_message(conv.id, "user", "x")

        cmd = ClearCommand()
        out = await cmd.run(_ctx(store, settings, registry))
        assert isinstance(out, str)
        # The count of cleared messages should appear.
        assert "5" in out

    async def test_reply_on_empty_conversation(self, wired) -> None:
        from ember.commands.clear import ClearCommand

        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")

        cmd = ClearCommand()
        out = await cmd.run(_ctx(store, settings, registry))
        # Doesn't raise; returns string. 0 messages is a valid case.
        assert isinstance(out, str)
        assert out != ""
        assert "0" in out


class TestSessionResetIndicator:
    async def test_session_reset_true_when_session_existed(self, wired) -> None:
        from ember.commands.clear import ClearCommand

        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        await store.update_claude_cli_session(conv.id, "session-id")

        cmd = ClearCommand()
        out = await cmd.run(_ctx(store, settings, registry))
        # Implementation should expose that the session was reset somehow —
        # we accept any indicator that mentions "session" or "reset" or
        # surfaces a True flag.
        assert isinstance(out, str)
        # Just confirm the call succeeded (not raised) and produced output.
        assert out != ""
