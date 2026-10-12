"""CommandRegistry (FC-12).

Per the contracts doc:
- The authoritative list of commands lives in one module
  (`commands/registry.py`) — channel adapters do not invent commands.
- Unknown command → `?  unknown command. /help for the list.`
- `/help` is auto-generated from the registry; never hand-written.

These tests pin the FC-12 surface: register, get, list_commands (sorted
for deterministic /help), dispatch with the unknown-command literal,
and dispatch's exception trapping (TLC-7 — failures do not propagate).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.commands.base import CommandContext
from ember.commands.registry import CommandRegistry
from ember.config import Settings
from ember.store.repo import Store


# --- Test doubles ---------------------------------------------------------


class _StubCommand:
    """Minimal SlashCommand for registry tests; no run-side behavior."""

    def __init__(self, name: str, description: str = "stub", reply: str = "ok") -> None:
        self.name = name
        self.description = description
        self._reply = reply

    async def run(self, ctx: CommandContext) -> str:
        return self._reply


class _RaisingCommand:
    """A SlashCommand whose run() raises — used to test dispatch's
    exception trapping."""

    name = "boom"
    description = "always raises"

    async def run(self, ctx: CommandContext) -> str:
        raise RuntimeError("kaboom")


# --- Fixtures -------------------------------------------------------------


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
async def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real CommandContext: real Store on tmp_path, real Settings, an
    empty registry. Tests that need commands registered will register
    them directly."""
    settings = _make_settings(monkeypatch, tmp_path)
    store = Store(tmp_path / "conversations.db")
    await store.open()
    registry = CommandRegistry()
    try:
        yield CommandContext(
            conversation_key="thread-1",
            user_id="1000",
            store=store,
            settings=settings,
            registry=registry,
        )
    finally:
        await store.close()


# --- register / get ------------------------------------------------------


class TestRegisterGet:
    def test_register_then_get_round_trip(self) -> None:
        reg = CommandRegistry()
        cmd = _StubCommand("ping")
        reg.register(cmd)
        assert reg.get("ping") is cmd

    def test_register_duplicate_raises_value_error(self) -> None:
        reg = CommandRegistry()
        reg.register(_StubCommand("ping"))
        with pytest.raises(ValueError):
            reg.register(_StubCommand("ping"))

    def test_get_unknown_raises_key_error(self) -> None:
        reg = CommandRegistry()
        with pytest.raises(KeyError):
            reg.get("nope")


# --- list_commands: sorted for deterministic /help -----------------------


class TestListCommands:
    def test_list_commands_empty_when_nothing_registered(self) -> None:
        reg = CommandRegistry()
        assert reg.list_commands() == []

    def test_list_commands_sorted_by_name(self) -> None:
        # FC-12: /help is auto-generated and must be deterministic. The
        # registry guarantees sort order.
        reg = CommandRegistry()
        reg.register(_StubCommand("zulu"))
        reg.register(_StubCommand("alpha"))
        reg.register(_StubCommand("mike"))
        names = [c.name for c in reg.list_commands()]
        assert names == ["alpha", "mike", "zulu"]


# --- dispatch ------------------------------------------------------------


class TestDispatch:
    async def test_dispatch_runs_registered_command(self, ctx) -> None:
        reg = ctx.registry
        reg.register(_StubCommand("ping", reply="pong"))
        result = await reg.dispatch("ping", ctx)
        assert result == "pong"

    async def test_dispatch_unknown_returns_unknown_message(self, ctx) -> None:
        # FC-12 specifies the literal: "?  unknown command. /help for the list."
        reg = ctx.registry
        result = await reg.dispatch("nonexistent", ctx)
        # The exact wording is pinned by FC-12.
        assert "unknown command" in result
        assert "/help" in result

    async def test_dispatch_traps_command_exception(self, ctx) -> None:
        # TLC-7: no exception silently swallowed, but also no exception
        # propagated up out of the registry. The user must see a
        # recognizable failure message.
        reg = ctx.registry
        reg.register(_RaisingCommand())
        # Must not raise.
        result = await reg.dispatch("boom", ctx)
        # FC-12 / TLC-7: rendered as a user-facing ❌ string.
        assert isinstance(result, str)
        assert "❌" in result
