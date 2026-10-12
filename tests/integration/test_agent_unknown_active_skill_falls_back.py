"""Agent gracefully handles a stale `active_skill` reference (FC-5, TLC-7, v7).

Per the brief: "Unknown active_skill (e.g. skill file was deleted
between activation and this turn): the agent loop logs a warning,
treats as 'no skill active,' and continues. Does not crash the turn."

Setup: set `conversation.active_skill = "ghost"` but do NOT register
"ghost" in the skill registry. Then drive a turn and assert:

- Turn completes without raising out of `handle_turn`.
- A "final" message is emitted (the assistant reply).
- The provider was called with `tools=None` (treated as no skill
  active — see brief).
- A WARNING log line is emitted naming the unknown skill.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ember.agent import Agent
from ember.channels.base import ChannelMessage
from ember.config import Settings
from ember.identity.store import IdentityStore
from ember.skills.registry import InMemorySkillRegistry
from ember.store.repo import Store
from ember.tools.registry import Registry
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


def _msg(text: str = "hi", *, key: str = "thread-1", mid: str = "m1") -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key=key,
        user_id="1000",
        text=text,
        message_id=mid,
    )


async def _set_active_skill(store: Store, conversation_key: str, skill_name: str) -> None:
    conv = await store.get_or_create_conversation("discord", conversation_key)
    await store._c.execute(  # type: ignore[attr-defined]
        "UPDATE conversations SET active_skill = ? WHERE id = ?",
        (skill_name, conv.id),
    )
    await store._c.commit()  # type: ignore[attr-defined]


class TestUnknownActiveSkillFallsBack:
    async def test_turn_completes_without_raising(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        store = Store(tmp_path / "conversations.db")
        await store.open()
        try:
            identity = IdentityStore(tmp_path / "identity")
            await identity.ensure_initialized()

            skill_registry = InMemorySkillRegistry()
            tool_registry = Registry()
            provider = FakeProvider()
            channel = FakeChannel()
            agent = Agent(
                store=store,
                provider=provider,
                channel=channel,
                settings=settings,
                identity=identity,
                skill_registry=skill_registry,
                tool_registry=tool_registry,
            )
            await channel.start(agent.handle_turn)

            await _set_active_skill(store, "thread-1", "ghost")
            provider.queue_text("ok")

            with caplog.at_level(logging.WARNING):
                # Should not raise.
                await channel.inject(_msg("hi"))

            # Final reply was emitted.
            finals = [s for s in channel.sent if s.kind == "final"]
            assert len(finals) == 1

            # Provider was called once. With unknown skill, the agent
            # behaves as if no skill is active → tools=None.
            assert len(provider.calls) == 1
            assert provider.calls[0].tools is None

            # A warning log mentioning the missing skill name landed.
            ghost_warnings = [
                rec
                for rec in caplog.records
                if rec.levelno >= logging.WARNING and "ghost" in rec.getMessage().lower()
            ]
            assert ghost_warnings, "expected a WARNING log mentioning 'ghost'"
        finally:
            await store.close()

    async def test_unknown_skill_does_not_corrupt_system_prompt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The system block should match the no-skill-active baseline.
        settings = _make_settings(monkeypatch, tmp_path)
        store = Store(tmp_path / "conversations.db")
        await store.open()
        try:
            identity = IdentityStore(tmp_path / "identity")
            await identity.ensure_initialized()

            skill_registry = InMemorySkillRegistry()
            tool_registry = Registry()
            provider = FakeProvider()
            channel = FakeChannel()
            agent = Agent(
                store=store,
                provider=provider,
                channel=channel,
                settings=settings,
                identity=identity,
                skill_registry=skill_registry,
                tool_registry=tool_registry,
            )
            await channel.start(agent.handle_turn)

            await _set_active_skill(store, "thread-1", "ghost")
            provider.queue_text("ok")

            await channel.inject(_msg("hi"))

            assert len(provider.calls) == 1
            system = provider.calls[0].system
            # Identity content present; no stray skill prompt or error
            # text from the missing reference.
            assert identity.system_prompt() in system
        finally:
            await store.close()
