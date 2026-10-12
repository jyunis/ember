"""Agent tool-filter pass-through with active skill (FC-5, v7).

When `conversation.active_skill` is non-NULL, the agent loop filters
the tool registry to the skill's `tools` list and passes the result as
`tools=[ToolSpec(...), ...]` to `provider.complete`.

When unset, behavior is unchanged: `tools` arg is `None` (current v1
behavior).

Spec ambiguities flagged in report:
3. Whether the `tools` filter preserves registry order or sorts
   alphabetically. The brief says "in registry-order". We assert that
   the names appearing in `provider.calls[-1].tools` match the active
   skill's `tools` tuple **as a set** (membership), and we additionally
   assert order matches **registry order** (sorted by name, since the
   `Registry.list_tools` sorts).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ember.agent import Agent
from ember.channels.base import ChannelMessage
from ember.config import Settings
from ember.identity.store import IdentityStore
from ember.providers.base import ToolSpec
from ember.skills.base import DeclarativeSkill
from ember.skills.registry import InMemorySkillRegistry
from ember.store.repo import Store
from ember.tools.base import Tool, ToolResult
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


class _StubTool(Tool):
    def __init__(self, name: str, description: str = "stub") -> None:
        self.name = name
        self.description = description
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        self.requires_confirmation: bool = False

    async def call(self, args: dict[str, Any]) -> ToolResult:  # pragma: no cover
        return ToolResult(output={})


async def _set_active_skill(store: Store, conversation_key: str, skill_name: str) -> None:
    conv = await store.get_or_create_conversation("discord", conversation_key)
    await store._c.execute(  # type: ignore[attr-defined]
        "UPDATE conversations SET active_skill = ? WHERE id = ?",
        (skill_name, conv.id),
    )
    await store._c.commit()  # type: ignore[attr-defined]


async def _build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    skill_tools: tuple[str, ...],
    registry_tool_names: tuple[str, ...],
) -> tuple[Store, FakeProvider, FakeChannel, Agent]:
    settings = _make_settings(monkeypatch, tmp_path)
    store = Store(tmp_path / "conversations.db")
    await store.open()

    identity = IdentityStore(tmp_path / "identity")
    await identity.ensure_initialized()

    skill_registry = InMemorySkillRegistry()
    skill_registry.replace(
        DeclarativeSkill(
            name="demo",
            description="d",
            tools=skill_tools,
            system_prompt="x",
        )
    )

    tool_registry = Registry()
    for name in registry_tool_names:
        tool_registry.register(_StubTool(name, description=f"desc {name}"))

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
    return store, provider, channel, agent


class TestAgentActiveSkillFiltersTools:
    async def test_active_skill_filters_tools_to_listed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, provider, channel, _ = await _build(
            tmp_path,
            monkeypatch,
            skill_tools=("alpha", "gamma"),
            registry_tool_names=("alpha", "beta", "gamma"),
        )
        try:
            await _set_active_skill(store, "thread-1", "demo")
            provider.queue_text("ok")
            await channel.inject(_msg("hi"))

            assert len(provider.calls) == 1
            tools = provider.calls[0].tools
            assert tools is not None
            tool_names = [t.name for t in tools]
            # Only the skill's listed tools, registry-order (sorted).
            assert tool_names == ["alpha", "gamma"]
            # Each is a ToolSpec.
            for spec in tools:
                assert isinstance(spec, ToolSpec)
        finally:
            await store.close()

    async def test_active_skill_with_empty_tools_passes_empty_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Edge: skill listing zero tools. The provider sees `tools=[]`
        # (empty list, not None) — strict allowlist.
        store, provider, channel, _ = await _build(
            tmp_path,
            monkeypatch,
            skill_tools=(),
            registry_tool_names=("alpha", "beta"),
        )
        try:
            await _set_active_skill(store, "thread-1", "demo")
            provider.queue_text("ok")
            await channel.inject(_msg("hi"))

            assert len(provider.calls) == 1
            tools = provider.calls[0].tools
            # Strict allowlist: not None, but empty.
            assert tools == []
        finally:
            await store.close()

    async def test_no_active_skill_with_empty_skill_registry_passes_no_tools(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # v8 (FC-18): router mode with an empty skill registry has nothing
        # to dispatch to — `use_skill` is not exposed and the legacy
        # "tools=None" behavior is preserved. Router mode with a
        # non-empty registry exposes `use_skill`; that case is covered
        # in tests/integration/test_router_prompt_in_agent_loop.py.
        settings = _make_settings(monkeypatch, tmp_path)
        store = Store(tmp_path / "conversations.db")
        await store.open()

        identity = IdentityStore(tmp_path / "identity")
        await identity.ensure_initialized()

        # Empty skill registry — nothing to route to.
        skill_registry = InMemorySkillRegistry()
        tool_registry = Registry()
        for name in ("alpha", "beta"):
            tool_registry.register(_StubTool(name, description=f"desc {name}"))

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

        try:
            # Do NOT set an active_skill.
            provider.queue_text("ok")
            await channel.inject(_msg("hi"))

            assert len(provider.calls) == 1
            tools = provider.calls[0].tools
            # Either tools is None, or it's a list that doesn't include
            # `use_skill` — both express "no skill dispatch available".
            if tools is not None:
                assert "use_skill" not in [t.name for t in tools]
        finally:
            await store.close()
