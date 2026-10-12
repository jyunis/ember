"""HelpCommand (`/help`) — FC-12.

Per FC-12: `/help` is auto-generated from the registry; never
hand-written. List ordering must be deterministic (the registry
guarantees sorted-by-name).

These tests pin the surface: HelpCommand reads from the registry
attached to its CommandContext, includes every registered command's
name + description, is deterministic, and includes its peer commands
(/help, /context) when they are registered.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.commands.base import CommandContext
from ember.commands.context import ContextCommand
from ember.commands.help import HelpCommand
from ember.commands.registry import CommandRegistry
from ember.config import Settings
from ember.store.repo import Store


# --- Test doubles ---------------------------------------------------------


class _StubCommand:
    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description

    async def run(self, ctx: CommandContext) -> str:
        return ""


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
) -> CommandContext:
    return CommandContext(
        conversation_key=conversation_key,
        user_id="1000",
        store=store,
        settings=settings,
        registry=registry,
    )


# --- Auto-generation from registry ---------------------------------------


class TestHelpAutoGenerates:
    async def test_includes_every_registered_command_name(self, wired) -> None:
        store, settings, registry = wired
        registry.register(_StubCommand("ping", "say pong"))
        registry.register(_StubCommand("zulu", "the last one"))
        registry.register(_StubCommand("alpha", "the first one"))

        cmd = HelpCommand()
        out = await cmd.run(_ctx(store, settings, registry))
        assert "ping" in out
        assert "zulu" in out
        assert "alpha" in out

    async def test_includes_every_registered_command_description(self, wired) -> None:
        store, settings, registry = wired
        registry.register(_StubCommand("ping", "DESC_FOR_PING"))
        registry.register(_StubCommand("alpha", "DESC_FOR_ALPHA"))

        cmd = HelpCommand()
        out = await cmd.run(_ctx(store, settings, registry))
        assert "DESC_FOR_PING" in out
        assert "DESC_FOR_ALPHA" in out

    async def test_output_deterministic_sorted(self, wired) -> None:
        # Determinism: HelpCommand must use registry.list_commands(),
        # which is sorted. Two calls back-to-back with the same registry
        # must produce identical output.
        store, settings, registry = wired
        registry.register(_StubCommand("zulu", "z"))
        registry.register(_StubCommand("alpha", "a"))
        registry.register(_StubCommand("mike", "m"))

        cmd = HelpCommand()
        a = await cmd.run(_ctx(store, settings, registry))
        b = await cmd.run(_ctx(store, settings, registry))
        assert a == b
        # And the names appear in alphabetical order in the rendered
        # text — this is the user-facing determinism guarantee.
        i_alpha = a.index("alpha")
        i_mike = a.index("mike")
        i_zulu = a.index("zulu")
        assert i_alpha < i_mike < i_zulu

    async def test_includes_help_and_context_when_registered(self, wired) -> None:
        # When the v1 commands (/help, /context) are registered, the
        # rendered list must include both — /help auto-generates from
        # the registry and so includes itself.
        store, settings, registry = wired
        registry.register(HelpCommand())
        registry.register(ContextCommand())

        cmd = HelpCommand()
        out = await cmd.run(_ctx(store, settings, registry))
        assert "help" in out
        assert "context" in out

    async def test_empty_registry_returns_string(self, wired) -> None:
        # Edge: HelpCommand on an empty registry doesn't raise; returns
        # some kind of "no commands" string. Spec doesn't pin wording —
        # we just assert it's a string and not an error.
        store, settings, registry = wired
        cmd = HelpCommand()
        out = await cmd.run(_ctx(store, settings, registry))
        assert isinstance(out, str)
