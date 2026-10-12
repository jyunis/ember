"""`/lock` (doc #60 UoW-36 c) — ends the conversation's unlock window."""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.commands.base import CommandContext
from ember.commands.lock import LockCommand
from ember.commands.registry import CommandRegistry
from ember.config import Settings
from ember.store.repo import Store
from ember.unlock import UnlockStore


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
    try:
        yield store, settings, CommandRegistry()
    finally:
        await store.close()


def _ctx(store: Store, settings: Settings, registry: CommandRegistry, key: str | None):
    return CommandContext(
        conversation_key=key,
        user_id="1000",
        store=store,
        settings=settings,
        registry=registry,
        args={},
    )


class TestLockCommand:
    async def test_lock_ends_window(self, wired) -> None:
        store, settings, registry = wired
        unlock = UnlockStore(600)
        unlock.unlock("thread-1")
        cmd = LockCommand(unlock_store=unlock)
        out = await cmd.run(_ctx(store, settings, registry, "thread-1"))
        assert out.startswith("🔒 locked")
        assert not unlock.is_unlocked("thread-1")

    async def test_lock_when_already_locked(self, wired) -> None:
        store, settings, registry = wired
        cmd = LockCommand(unlock_store=UnlockStore(600))
        assert "already" in await cmd.run(_ctx(store, settings, registry, "thread-1"))

    async def test_out_of_thread(self, wired) -> None:
        store, settings, registry = wired
        cmd = LockCommand(unlock_store=UnlockStore(600))
        assert "thread" in await cmd.run(_ctx(store, settings, registry, None))
