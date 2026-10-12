"""Router prompt assembly in the full agent loop (FC-18).

Exercises the FC-18 contract end-to-end: when `active_skill IS NULL`,
the parent provider's `system` arg must contain the "## Available
skills" block built from the registry; when pinned, it must not.

Wires a real Store, real IdentityStore on `tmp_path`, real
InMemorySkillRegistry, plus FakeProvider + FakeChannel + the Agent.

Spec ambiguities flagged in report:

  - FC-18 §5: when in router mode but the registry is empty, should
    `use_skill` be advertised? The skill-dispatch tool with no skills
    to dispatch to is useless. We assume the implementer hides it
    when the registry is empty AND the skills block is also omitted.
    A test asserts this. If the implementer chooses always-expose,
    flip the assertion.

  - The exact skills block content — only the `## Available skills`
    header is pinned by spec. We assert presence of the header +
    skill name(s); we don't pin internal formatting beyond what the
    unit-level tests cover.

  - Use of `IdentityStore.SECTION_SEPARATOR` between identity and
    skills block is not pinned; we just check substring presence.
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


# --- Fixtures + helpers ------------------------------------------------------


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


SKILL_DESC = "Demo skill description first line."
SKILL_SYSTEM_PROMPT = "PINNED_SKILL_PROMPT_SENTINEL"


async def _set_active_skill(store: Store, conversation_key: str, skill_name: str | None) -> None:
    conv = await store.get_or_create_conversation("discord", conversation_key)
    await store._c.execute(  # type: ignore[attr-defined]
        "UPDATE conversations SET active_skill = ? WHERE id = ?",
        (skill_name, conv.id),
    )
    await store._c.commit()  # type: ignore[attr-defined]


async def _build_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    skills: list[DeclarativeSkill] | None = None,
) -> tuple[Store, FakeProvider, FakeChannel, Agent, InMemorySkillRegistry]:
    settings = _make_settings(monkeypatch, tmp_path)
    store = Store(tmp_path / "conversations.db")
    await store.open()

    identity = IdentityStore(tmp_path / "identity")
    await identity.ensure_initialized()

    skill_registry = InMemorySkillRegistry()
    for skill in skills or []:
        skill_registry.replace(skill)

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
    return store, provider, channel, agent, skill_registry


# --- FC-18 §1 — router mode includes skills block ---------------------------


class TestRouterModeIncludesSkillsBlock:
    async def test_router_mode_system_contains_skills_header_and_skill_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        skill = DeclarativeSkill(
            name="demo",
            description=SKILL_DESC,
            tools=(),
            system_prompt="DEMO_PROMPT",
        )
        store, provider, channel, _, _ = await _build_agent(tmp_path, monkeypatch, skills=[skill])
        try:
            # No active_skill set — router mode.
            provider.queue_text("ok")
            await channel.inject(_msg("hi"))

            assert len(provider.calls) == 1
            system = provider.calls[0].system
            assert "## Available skills" in system
            assert "demo" in system
            # First-line description rendered.
            assert SKILL_DESC in system
        finally:
            await store.close()


# --- FC-18 §2 — pinned mode omits skills block ------------------------------


class TestPinnedModeOmitsSkillsBlock:
    async def test_pinned_mode_system_does_not_contain_skills_header(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        skill = DeclarativeSkill(
            name="demo",
            description=SKILL_DESC,
            tools=(),
            system_prompt=SKILL_SYSTEM_PROMPT,
        )
        store, provider, channel, _, _ = await _build_agent(tmp_path, monkeypatch, skills=[skill])
        try:
            await _set_active_skill(store, "thread-1", "demo")
            provider.queue_text("ok")
            await channel.inject(_msg("hi"))

            assert len(provider.calls) == 1
            system = provider.calls[0].system
            # Pinned mode: skills block is suppressed.
            assert "## Available skills" not in system
            # The skill's own system prompt is what's used instead.
            assert SKILL_SYSTEM_PROMPT in system
        finally:
            await store.close()


# --- FC-18 §3 — empty registry → no skills block ----------------------------


class TestEmptyRegistryOmitsSkillsBlock:
    async def test_router_mode_empty_registry_omits_skills_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, provider, channel, _, _ = await _build_agent(tmp_path, monkeypatch, skills=[])
        try:
            provider.queue_text("ok")
            await channel.inject(_msg("hi"))

            assert len(provider.calls) == 1
            system = provider.calls[0].system
            assert "## Available skills" not in system
        finally:
            await store.close()


# --- FC-18 §4 — determinism / TLC-12 ----------------------------------------


class TestSkillsBlockDeterminism:
    async def test_block_byte_stable_across_consecutive_turns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        skill = DeclarativeSkill(
            name="demo",
            description=SKILL_DESC,
            tools=(),
            system_prompt="DEMO_PROMPT",
        )
        store, provider, channel, _, _ = await _build_agent(tmp_path, monkeypatch, skills=[skill])
        try:
            provider.queue_text("a")
            provider.queue_text("b")
            await channel.inject(_msg("first", mid="m1"))
            await channel.inject(_msg("second", mid="m2"))

            assert len(provider.calls) == 2
            # Whole system prompt is byte-identical (TLC-12).
            assert provider.calls[0].system == provider.calls[1].system
            # And both contain the skills block.
            assert "## Available skills" in provider.calls[0].system

            # Extract the skills block and confirm byte-equality
            # (substring perspective).
            s0 = provider.calls[0].system
            s1 = provider.calls[1].system
            i0 = s0.index("## Available skills")
            i1 = s1.index("## Available skills")
            assert s0[i0:] == s1[i1:]
        finally:
            await store.close()

    async def test_adding_skill_between_turns_changes_block_by_one_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        skill_a = DeclarativeSkill(
            name="alpha",
            description="alpha desc",
            tools=(),
            system_prompt="A_PROMPT",
        )
        skill_b = DeclarativeSkill(
            name="beta",
            description="beta desc",
            tools=(),
            system_prompt="B_PROMPT",
        )
        store, provider, channel, _, registry = await _build_agent(
            tmp_path, monkeypatch, skills=[skill_a]
        )
        try:
            provider.queue_text("a")
            await channel.inject(_msg("first", mid="m1"))

            # Add a skill between turns.
            registry.replace(skill_b)

            provider.queue_text("b")
            await channel.inject(_msg("second", mid="m2"))

            assert len(provider.calls) == 2
            s0 = provider.calls[0].system
            s1 = provider.calls[1].system

            # Turn 1: only alpha; turn 2: alpha + beta.
            assert "alpha" in s0
            assert "beta" not in s0
            assert "alpha" in s1
            assert "beta" in s1
        finally:
            await store.close()


# --- FC-18 §5 — use_skill tool exposure -------------------------------------


class TestUseSkillExposure:
    async def test_router_mode_with_skill_advertises_use_skill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Router mode + non-empty registry → `use_skill` in tools list.

        This restates a slice of `test_use_skill_round_trip.py`'s
        coverage but specifically pins the FC-18 contract that the
        router prompt and the tool exposure travel together.
        """
        skill = DeclarativeSkill(
            name="demo",
            description=SKILL_DESC,
            tools=(),
            system_prompt="DEMO_PROMPT",
        )
        store, provider, channel, _, _ = await _build_agent(tmp_path, monkeypatch, skills=[skill])
        try:
            provider.queue_text("ok")
            await channel.inject(_msg("hi"))

            assert len(provider.calls) == 1
            tools = provider.calls[0].tools
            assert tools is not None, "expected router mode + non-empty registry to advertise tools"
            names = [t.name for t in tools]
            assert "use_skill" in names
        finally:
            await store.close()

    async def test_router_mode_empty_registry_hides_use_skill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ASSUMPTION (flagged in report): with no skills to dispatch
        to, `use_skill` is not exposed. If the implementer chooses to
        always expose it, flip this assertion."""
        store, provider, channel, _, _ = await _build_agent(tmp_path, monkeypatch, skills=[])
        try:
            provider.queue_text("ok")
            await channel.inject(_msg("hi"))

            assert len(provider.calls) == 1
            tools = provider.calls[0].tools
            # Either tools is None, or it's a list that doesn't include
            # use_skill — both are acceptable expressions of "no
            # use_skill in router mode with empty registry".
            if tools is not None:
                names = [t.name for t in tools]
                assert "use_skill" not in names
        finally:
            await store.close()

    async def test_pinned_mode_does_not_advertise_use_skill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        skill = DeclarativeSkill(
            name="demo",
            description=SKILL_DESC,
            tools=(),
            system_prompt=SKILL_SYSTEM_PROMPT,
        )
        store, provider, channel, _, _ = await _build_agent(tmp_path, monkeypatch, skills=[skill])
        try:
            await _set_active_skill(store, "thread-1", "demo")
            provider.queue_text("ok")
            await channel.inject(_msg("hi"))

            assert len(provider.calls) == 1
            tools = provider.calls[0].tools
            # In pinned mode, the tools list (per FC-5/v7) is filtered
            # to the skill's allowlist. `use_skill` must be absent.
            if tools is not None:
                names = [t.name for t in tools]
                assert "use_skill" not in names
        finally:
            await store.close()
