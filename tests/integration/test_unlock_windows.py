"""Unlock windows end to end (doc #60 TLC-31 / UoW-36).

A locked-set tool's 🔒 prompt does not approve one call: a valid TOTP code
opens the conversation for the TTL, the pending call is NOT executed by
the redeem path (no value reaches the screen), and the model gets a
follow-up turn to re-issue its calls, which now pass the gate.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from ember.agent import Agent
from ember.bridge import active as bridge_active
from ember.identity.store import IdentityStore
from ember.providers.routing import ProviderRouter
from ember.store.repo import Store
from ember.tools.registry import Registry
from ember.unlock import UnlockStore
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider
from tests.integration.test_agent_tlc3 import _tool_use_event
from tests.integration.test_confirm_totp_gate import (
    _current_code,
    _make_settings,
    _msg,
    _Recording,
)


@pytest.fixture
async def opened_store(tmp_path: Path):
    store = Store(tmp_path / "conversations.db")
    await store.open()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
async def identity(tmp_path: Path) -> IdentityStore:
    s = IdentityStore(tmp_path / "identity")
    await s.ensure_initialized()
    return s


LOCKED_TOOL = "locked_read"


def _unlock_marker_event(tool_use_id: str, *, action: str) -> dict:
    marker = json.dumps({"error": "confirmation_required", "action": action, "unlock": True})
    return {
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": marker}]
        },
    }


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    store: Store,
    identity: IdentityStore,
    ttl: int = 600,
) -> tuple[Agent, FakeChannel, FakeProvider, Registry, _Recording, UnlockStore]:
    monkeypatch.setenv("LOCKED_TOOLS", LOCKED_TOOL)
    monkeypatch.setenv("UNLOCK_TTL_SECONDS", str(ttl))
    settings = _make_settings(monkeypatch, tmp_path)
    unlock = UnlockStore(settings.unlock_ttl_seconds)
    tool = _Recording(name=LOCKED_TOOL)
    registry = Registry()
    registry.register(tool)
    registry.configure_unlock(
        settings.locked_tools,
        lambda: unlock.is_unlocked(bridge_active.active_conversation_key()),
    )
    primary = FakeProvider()
    primary.name = "claude_cli"
    channel = FakeChannel()
    agent = Agent(
        store=store,
        channel=channel,
        settings=settings,
        router=ProviderRouter({"claude_cli": primary}),
        identity=identity,
        tool_registry=registry,
        unlock_store=unlock,
    )
    return agent, channel, primary, registry, tool, unlock


def _sent(channel: FakeChannel) -> list[str]:
    return [s.content for s in channel.sent if s.kind in ("status", "final")]


class TestUnlockFlow:
    async def test_prompt_unlock_and_continue(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        agent, channel, primary, registry, tool, unlock = _wire(
            monkeypatch, tmp_path, store=opened_store, identity=identity
        )
        await channel.start(agent.handle_turn)

        # Turn 1: the model hits the locked tool → unlock prompt.
        primary.queue_text_with_events(
            "I need you to unlock the vault.",
            events=[
                _tool_use_event("tu_1", LOCKED_TOOL, {"to": "a"}),
                _unlock_marker_event("tu_1", action="locked_read(to='a')"),
            ],
        )
        await channel.inject(_msg("get the key", mid="m1"))
        prompts = [c for c in _sent(channel) if "🔒" in c]
        assert len(prompts) == 1
        assert "unlock" in prompts[0] and "10m" in prompts[0], prompts[0]
        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        cur = await opened_store._c.execute(
            "SELECT pending_tool_confirmation FROM conversations WHERE id = ?", (conv.id,)
        )
        row = await cur.fetchone()
        await cur.close()
        assert json.loads(row[0])["mode"] == "unlock"

        # Turn 2: code → window opens, tool NOT executed here, model follows up.
        primary.queue_text("Got it — using the key now.")
        await channel.inject(_msg(f"y {_current_code()}", mid="m2"))
        assert tool.calls == [], "redeem must not execute the call (no value to the screen)"
        assert unlock.is_unlocked("thread-1")
        assert any(c.startswith("🔓") for c in _sent(channel))
        assert len(primary.calls) == 2, "one follow-up provider turn after the unlock"
        assert _sent(channel)[-1] == "Got it — using the key now."
        msgs = await opened_store.recent_messages(conv.id, limit=200, include_compacted=True)
        users = [m.content for m in msgs if m.role == "user"]
        assert "[Automatic follow-up]" in users[-1] and "unlocked" in users[-1]
        assert _current_code() not in users[-1]

        # Inside the window the gate lets the call through (active turn = thread-1).
        bridge_active.set_active(channel, "thread-1")
        try:
            result = await registry.call_tool(LOCKED_TOOL, {"to": "a"})
        finally:
            bridge_active.clear_active()
        assert not result.is_error and tool.calls == [{"to": "a"}]

        # Another conversation is still locked; /lock re-locks this one.
        bridge_active.set_active(channel, "thread-2")
        try:
            other = await registry.call_tool(LOCKED_TOOL, {"to": "b"})
        finally:
            bridge_active.clear_active()
        assert other.is_error and other.output["unlock"] is True
        unlock.lock("thread-1")
        bridge_active.set_active(channel, "thread-1")
        try:
            relocked = await registry.call_tool(LOCKED_TOOL, {"to": "a"})
        finally:
            bridge_active.clear_active()
        assert relocked.is_error

    async def test_bare_yes_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        agent, channel, primary, registry, tool, unlock = _wire(
            monkeypatch, tmp_path, store=opened_store, identity=identity
        )
        await channel.start(agent.handle_turn)
        primary.queue_text_with_events(
            "unlock please",
            events=[
                _tool_use_event("tu_1", LOCKED_TOOL, {"to": "a"}),
                _unlock_marker_event("tu_1", action="locked_read(to='a')"),
            ],
        )
        await channel.inject(_msg("go", mid="m1"))
        await channel.inject(_msg("y", mid="m2"))
        assert not unlock.is_unlocked("thread-1")
        assert tool.calls == []
        assert "code" in _sent(channel)[-1].lower()

    async def test_two_locked_calls_one_prompt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        agent, channel, primary, registry, tool, unlock = _wire(
            monkeypatch, tmp_path, store=opened_store, identity=identity
        )
        await channel.start(agent.handle_turn)
        primary.queue_text_with_events(
            "unlock please",
            events=[
                _tool_use_event("tu_1", LOCKED_TOOL, {"to": "a"}),
                _unlock_marker_event("tu_1", action="locked_read(to='a')"),
                _tool_use_event("tu_2", LOCKED_TOOL, {"to": "b"}),
                _unlock_marker_event("tu_2", action="locked_read(to='b')"),
            ],
        )
        await channel.inject(_msg("go", mid="m1"))
        assert len([c for c in _sent(channel) if "🔒" in c]) == 1


class TestProactiveUnlock:
    async def test_unlock_code_opens_window(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        agent, channel, primary, registry, tool, unlock = _wire(
            monkeypatch, tmp_path, store=opened_store, identity=identity
        )
        await channel.start(agent.handle_turn)
        await channel.inject(_msg(f"unlock {_current_code()}", mid="m1"))
        assert unlock.is_unlocked("thread-1")
        assert _sent(channel)[-1].startswith("🔓")
        assert primary.calls == []

    async def test_bad_code_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        agent, channel, primary, registry, tool, unlock = _wire(
            monkeypatch, tmp_path, store=opened_store, identity=identity
        )
        await channel.start(agent.handle_turn)
        await channel.inject(_msg("unlock 000000", mid="m1"))
        assert not unlock.is_unlocked("thread-1")
        assert _sent(channel)[-1].startswith("❌")
        assert primary.calls == []
