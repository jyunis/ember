"""TrimCommand (`/trim N`) — v9 FC-12.

Drops the oldest N non-compacted messages by marking them `compacted=1`.
Bound `N`:
- positive integer required;
- if N >= count(non_compacted), trim them all (clamp).

Reply: `🪒 trimmed K messages` where K is the actual count clamped.

Also covers the new repo helper `Store.trim_oldest_active(conversation_id, n)`.
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


# --- Repo helper: Store.trim_oldest_active --------------------------------


class TestTrimOldestActive:
    async def test_trims_oldest_first(self, wired) -> None:
        store, _settings, _registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        ids: list[int] = []
        for i in range(5):
            ids.append(await store.append_message(conv.id, "user", f"m{i}"))

        marked = await store.trim_oldest_active(conv.id, 2)
        assert marked == 2

        active = await store.recent_messages(conv.id, limit=100)
        assert len(active) == 3
        # The 2 oldest are gone.
        assert ids[0] not in [m.id for m in active]
        assert ids[1] not in [m.id for m in active]
        assert ids[2] in [m.id for m in active]

    async def test_clamps_when_n_exceeds_count(self, wired) -> None:
        store, _settings, _registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        for i in range(3):
            await store.append_message(conv.id, "user", f"m{i}")

        marked = await store.trim_oldest_active(conv.id, 99)
        assert marked == 3
        active = await store.recent_messages(conv.id, limit=100)
        assert active == []

    async def test_zero_returns_zero_no_writes(self, wired) -> None:
        store, _settings, _registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        for i in range(3):
            await store.append_message(conv.id, "user", f"m{i}")

        marked = await store.trim_oldest_active(conv.id, 0)
        assert marked == 0
        active = await store.recent_messages(conv.id, limit=100)
        assert len(active) == 3

    async def test_skips_already_compacted(self, wired) -> None:
        """Already-compacted messages don't count toward the trim."""
        store, _settings, _registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        ids: list[int] = []
        for i in range(5):
            ids.append(await store.append_message(conv.id, "user", f"m{i}"))
        # Mark the first 2 already compacted.
        await store.mark_messages_compacted(ids[:2])

        marked = await store.trim_oldest_active(conv.id, 2)
        assert marked == 2

        active = await store.recent_messages(conv.id, limit=100)
        # 5 total - 2 pre-compacted - 2 trimmed = 1 active left.
        assert len(active) == 1
        assert active[0].id == ids[4]


# --- TrimCommand surface --------------------------------------------------


class TestStaticSurface:
    def test_name(self) -> None:
        from ember.commands.trim import TrimCommand

        assert TrimCommand.name == "trim"

    def test_description_present(self) -> None:
        from ember.commands.trim import TrimCommand

        assert isinstance(TrimCommand.description, str)
        assert TrimCommand.description.strip() != ""

    def test_n_option_declared(self) -> None:
        from ember.commands.trim import TrimCommand

        opts = {o.name: o for o in TrimCommand.options}
        assert "n" in opts
        assert isinstance(opts["n"], CommandOption)


# --- Behavior ------------------------------------------------------------


class TestOutOfThread:
    async def test_no_thread_returns_message(self, wired) -> None:
        from ember.commands.trim import TrimCommand

        store, settings, registry = wired
        cmd = TrimCommand()
        out = await cmd.run(_ctx(store, settings, registry, conversation_key=None, args={"n": "1"}))
        assert isinstance(out, str)
        lowered = out.lower()
        assert "conversation" in lowered or "thread" in lowered or "must" in lowered


class TestTrimHappyPath:
    async def test_trims_n_oldest(self, wired) -> None:
        from ember.commands.trim import TrimCommand

        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        ids = []
        for i in range(5):
            ids.append(await store.append_message(conv.id, "user", f"m{i}"))

        cmd = TrimCommand()
        out = await cmd.run(_ctx(store, settings, registry, args={"n": "2"}))

        active = await store.recent_messages(conv.id, limit=100)
        assert len(active) == 3
        assert ids[0] not in [m.id for m in active]
        assert ids[1] not in [m.id for m in active]
        # Reply mentions the count.
        assert "2" in out

    async def test_trim_clamps_to_active_count(self, wired) -> None:
        from ember.commands.trim import TrimCommand

        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        for i in range(3):
            await store.append_message(conv.id, "user", f"m{i}")

        cmd = TrimCommand()
        out = await cmd.run(_ctx(store, settings, registry, args={"n": "10"}))

        active = await store.recent_messages(conv.id, limit=100)
        assert active == []
        # Reply reflects the clamped count.
        assert "3" in out


class TestInvalidN:
    async def test_zero_returns_usage(self, wired) -> None:
        from ember.commands.trim import TrimCommand

        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        for i in range(3):
            await store.append_message(conv.id, "user", f"m{i}")

        cmd = TrimCommand()
        out = await cmd.run(_ctx(store, settings, registry, args={"n": "0"}))
        # Should not raise; should return a usage hint.
        assert isinstance(out, str)
        assert out != ""
        # Nothing was trimmed.
        active = await store.recent_messages(conv.id, limit=100)
        assert len(active) == 3

    async def test_negative_returns_usage(self, wired) -> None:
        from ember.commands.trim import TrimCommand

        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        for i in range(3):
            await store.append_message(conv.id, "user", f"m{i}")

        cmd = TrimCommand()
        out = await cmd.run(_ctx(store, settings, registry, args={"n": "-3"}))
        assert isinstance(out, str)
        assert out != ""
        active = await store.recent_messages(conv.id, limit=100)
        assert len(active) == 3

    async def test_non_integer_returns_usage(self, wired) -> None:
        from ember.commands.trim import TrimCommand

        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")

        cmd = TrimCommand()
        out = await cmd.run(_ctx(store, settings, registry, args={"n": "abc"}))
        assert isinstance(out, str)
        assert out != ""

    async def test_missing_n_returns_usage(self, wired) -> None:
        from ember.commands.trim import TrimCommand

        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")

        cmd = TrimCommand()
        # No 'n' arg at all.
        out = await cmd.run(_ctx(store, settings, registry, args={}))
        assert isinstance(out, str)
        assert out != ""


class TestEmptyConversation:
    async def test_empty_conversation_returns_zero_count(self, wired) -> None:
        from ember.commands.trim import TrimCommand

        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")

        cmd = TrimCommand()
        out = await cmd.run(_ctx(store, settings, registry, args={"n": "5"}))
        assert isinstance(out, str)
        # Count should be 0.
        assert "0" in out
