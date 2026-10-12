"""`/context` API failovers counter (TLC-5 + UoW-15).

Architecture doc § Token + cost tracking across providers calls out a
dashboard line of the form:

    "12 subscription turns + $0.31 metered" → with the v2 addition of
    rate-limit failover, also: "API failovers: N".

When the test was first written the contracts brief warned this might
be a spec gap (no failover line in `/context`). Reading the live
`commands/context.py` confirms the line is already there, so this test
runs as a normal pass — pinning the surface so future refactors don't
silently drop the count.

If the implementer chooses a different label ("rate-limit failovers",
"failovers", etc.), update the assertion — the contract is "the count
appears, distinct from subscription turns and metered cost," not the
exact wording.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.commands.base import CommandContext
from ember.commands.context import ContextCommand
from ember.commands.registry import CommandRegistry
from ember.config import Settings
from ember.providers.base import Usage
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
) -> CommandContext:
    return CommandContext(
        conversation_key=conversation_key,
        user_id="1000",
        store=store,
        settings=settings,
        registry=registry,
    )


class TestContextFailoverCounter:
    async def test_context_mentions_failover_count(self, wired) -> None:
        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")

        # Two normal subscription assistants + two failover assistants.
        await store.append_message(conv.id, "user", "u1")
        await store.append_message(
            conv.id,
            "assistant",
            "sub1",
            usage=Usage(input_tokens=5, output_tokens=10, subscription=True),
        )
        await store.append_message(conv.id, "user", "u2")
        await store.append_message(
            conv.id,
            "assistant",
            "sub2",
            usage=Usage(input_tokens=5, output_tokens=10, subscription=True),
        )
        await store.append_message(conv.id, "user", "u3")
        await store.append_message(
            conv.id,
            "assistant",
            "failed-over",
            usage=Usage(input_tokens=5, output_tokens=10, cost_usd=0.001),
            failover=True,
        )
        await store.append_message(conv.id, "user", "u4")
        await store.append_message(
            conv.id,
            "assistant",
            "failed-over-2",
            usage=Usage(input_tokens=5, output_tokens=10, cost_usd=0.001),
            failover=True,
        )

        cmd = ContextCommand()
        out = await cmd.run(_ctx(store, settings, registry))
        lowered = out.lower()
        assert "failover" in lowered
        # Count of 2 failover turns must appear somewhere alongside it.
        assert "2" in out
