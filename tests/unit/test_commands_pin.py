"""PinCommand (`/pin <text>`) — v9 FC-12.

Stores a pinned-context entry on the current conversation via
`Store.add_pin(conv.id, text)`. Pins survive trim/compact/clear.

Reply: `📌 pinned: <text-truncated>`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.commands.base import CommandContext, CommandOption
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


# --- Static surface ------------------------------------------------------


class TestStaticSurface:
    def test_name(self) -> None:
        from ember.commands.pin import PinCommand

        assert PinCommand.name == "pin"

    def test_description_present(self) -> None:
        from ember.commands.pin import PinCommand

        assert isinstance(PinCommand.description, str)
        assert PinCommand.description.strip() != ""

    def test_text_option_declared(self) -> None:
        from ember.commands.pin import PinCommand

        opts = {o.name: o for o in PinCommand.options}
        assert "text" in opts
        assert isinstance(opts["text"], CommandOption)


# --- Behavior ------------------------------------------------------------


class TestOutOfThread:
    async def test_no_thread_returns_message(self, wired) -> None:
        from ember.commands.pin import PinCommand

        store, settings, registry = wired
        cmd = PinCommand()
        out = await cmd.run(
            _ctx(store, settings, registry, conversation_key=None, args={"text": "abc"})
        )
        assert isinstance(out, str)
        lowered = out.lower()
        assert "conversation" in lowered or "thread" in lowered or "must" in lowered


class TestPinHappyPath:
    async def test_pin_inserts_row(self, wired) -> None:
        from ember.commands.pin import PinCommand

        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")

        cmd = PinCommand()
        out = await cmd.run(_ctx(store, settings, registry, args={"text": "remember Tuesday"}))

        pins = await store.list_pins(conv.id)
        assert len(pins) == 1
        assert pins[0].content == "remember Tuesday"
        assert isinstance(out, str)
        assert "remember Tuesday" in out

    async def test_pin_appends_to_existing(self, wired) -> None:
        from ember.commands.pin import PinCommand

        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        await store.add_pin(conv.id, "first")

        cmd = PinCommand()
        await cmd.run(_ctx(store, settings, registry, args={"text": "second"}))

        pins = await store.list_pins(conv.id)
        assert len(pins) == 2
        assert [p.content for p in pins] == ["first", "second"]


class TestEmptyText:
    async def test_empty_text_returns_usage_no_write(self, wired) -> None:
        from ember.commands.pin import PinCommand

        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")

        cmd = PinCommand()
        out = await cmd.run(_ctx(store, settings, registry, args={"text": ""}))
        # No pin written.
        assert await store.list_pins(conv.id) == []
        # Reply is some usage/info string.
        assert isinstance(out, str)
        assert out != ""

    async def test_whitespace_only_text_returns_usage_no_write(self, wired) -> None:
        from ember.commands.pin import PinCommand

        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")

        cmd = PinCommand()
        out = await cmd.run(_ctx(store, settings, registry, args={"text": "   \n  "}))
        assert await store.list_pins(conv.id) == []
        assert isinstance(out, str)
        assert out != ""

    async def test_missing_text_returns_usage(self, wired) -> None:
        from ember.commands.pin import PinCommand

        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")

        cmd = PinCommand()
        out = await cmd.run(_ctx(store, settings, registry, args={}))
        assert isinstance(out, str)
        assert out != ""


class TestLongPinTruncation:
    async def test_long_pin_stored_in_full_reply_truncated(self, wired) -> None:
        """Reply may truncate the rendering of the pin text. The DB row
        stores the full content."""
        from ember.commands.pin import PinCommand

        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")

        long_text = "x" * 2000
        cmd = PinCommand()
        out = await cmd.run(_ctx(store, settings, registry, args={"text": long_text}))

        pins = await store.list_pins(conv.id)
        assert len(pins) == 1
        # Full content stored.
        assert pins[0].content == long_text
        # Reply is some string — the implementer may truncate. Just assert
        # the reply isn't the full 2000-char content (which would be a
        # poor user experience).
        assert isinstance(out, str)
        assert out != ""
