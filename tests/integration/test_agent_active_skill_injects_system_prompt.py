"""Agent prompt assembly with active skill (FC-5, TLC-12, v7).

When `conversation.active_skill` is non-NULL and the registry holds
the skill, `Agent._system_prompt` appends the skill's `system_prompt`
to the soul+profile block, separated by `IdentityStore.SECTION_SEPARATOR`.

This test wires a real Store, a real `IdentityStore` on `tmp_path`, a
real `InMemorySkillRegistry`, FakeProvider + FakeChannel. It then sets
`conversation.active_skill` directly via SQL and asserts the next turn
passes a `system` arg through to `provider.complete` that contains the
skill's system_prompt.

TLC-12: byte-stable across consecutive turns when nothing changed.

Spec ambiguity flagged in report:
- Brief says "separated by `IdentityStore.SECTION_SEPARATOR`". We assert
  the skill prompt is present AND it is preceded by the separator. We
  do not pin the exact byte sequence — only that:
    1. The full identity prompt appears.
    2. The skill's system_prompt appears.
    3. They are separated by something containing the separator chars.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.agent import Agent
from ember.channels.base import ChannelMessage
from ember.config import Settings
from ember.identity.store import IdentityStore
from ember.skills.base import DeclarativeSkill
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


SKILL_SYSTEM_PROMPT = "SKILL_PROMPT_SENTINEL: you are the demo skill."


async def _set_active_skill(store: Store, conversation_key: str, skill_name: str) -> None:
    """Write `active_skill` directly via SQL since `Store.set_active_skill`
    is the implementer's job to add."""
    conv = await store.get_or_create_conversation("discord", conversation_key)
    await store._c.execute(  # type: ignore[attr-defined]
        "UPDATE conversations SET active_skill = ? WHERE id = ?",
        (skill_name, conv.id),
    )
    await store._c.commit()  # type: ignore[attr-defined]


class TestAgentActiveSkillInjectsSystemPrompt:
    async def test_skill_system_prompt_appears_in_provider_system_arg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        store = Store(tmp_path / "conversations.db")
        await store.open()
        try:
            identity = IdentityStore(tmp_path / "identity")
            await identity.ensure_initialized()

            skill_registry = InMemorySkillRegistry()
            skill_registry.replace(
                DeclarativeSkill(
                    name="demo",
                    description="A demo skill.",
                    tools=(),
                    system_prompt=SKILL_SYSTEM_PROMPT,
                )
            )

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

            await _set_active_skill(store, "thread-1", "demo")
            provider.queue_text("ok")

            await channel.inject(_msg("hi"))

            assert len(provider.calls) == 1
            system = provider.calls[0].system
            # Skill prompt must appear in the assembled system block.
            assert SKILL_SYSTEM_PROMPT in system
            # Identity prompt must still be present.
            identity_prompt = identity.system_prompt()
            assert identity_prompt in system
            # The skill block follows the identity block (we append).
            assert system.index(identity_prompt) < system.index(SKILL_SYSTEM_PROMPT)
            # Separator chars present between them — at minimum the
            # double-newline `IdentityStore.SECTION_SEPARATOR` ("\n\n").
            sep = IdentityStore.SECTION_SEPARATOR
            # The window between identity end and skill start contains
            # the separator.
            id_end = system.index(identity_prompt) + len(identity_prompt)
            skill_start = system.index(SKILL_SYSTEM_PROMPT)
            window = system[id_end:skill_start]
            assert sep in window
        finally:
            await store.close()

    async def test_no_active_skill_means_unchanged_system_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same wiring, but no active_skill set. system arg is just the
        # identity block (v1 behavior).
        settings = _make_settings(monkeypatch, tmp_path)
        store = Store(tmp_path / "conversations.db")
        await store.open()
        try:
            identity = IdentityStore(tmp_path / "identity")
            await identity.ensure_initialized()

            skill_registry = InMemorySkillRegistry()
            skill_registry.replace(
                DeclarativeSkill(
                    name="demo",
                    description="d",
                    system_prompt=SKILL_SYSTEM_PROMPT,
                )
            )
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

            provider.queue_text("ok")
            await channel.inject(_msg("hi"))

            assert len(provider.calls) == 1
            system = provider.calls[0].system
            # Identity present.
            assert identity.system_prompt() in system
            # Skill prompt absent.
            assert SKILL_SYSTEM_PROMPT not in system
        finally:
            await store.close()

    async def test_active_skill_system_prompt_byte_stable_across_turns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # TLC-12: across consecutive turns with no underlying change,
        # the system block is byte-identical.
        settings = _make_settings(monkeypatch, tmp_path)
        store = Store(tmp_path / "conversations.db")
        await store.open()
        try:
            identity = IdentityStore(tmp_path / "identity")
            await identity.ensure_initialized()

            skill_registry = InMemorySkillRegistry()
            skill_registry.replace(
                DeclarativeSkill(
                    name="demo",
                    description="d",
                    system_prompt=SKILL_SYSTEM_PROMPT,
                )
            )
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

            await _set_active_skill(store, "thread-1", "demo")
            provider.queue_text("a")
            provider.queue_text("b")

            await channel.inject(_msg("first", mid="m1"))
            await channel.inject(_msg("second", mid="m2"))

            assert len(provider.calls) == 2
            assert provider.calls[0].system == provider.calls[1].system
        finally:
            await store.close()
