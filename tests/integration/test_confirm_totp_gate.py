"""Second-factor gate on tool confirmations.

The in-band `y` that redeems a `requires_confirmation` prompt arrives over
the same conversation that proposed the action, so it authenticates
nothing an attacker holding that conversation can't also produce. Tools
listed in `CONFIRM_TOTP_TOOLS` additionally require a rotating code from a
separate device.

Properties under test:
  - a gated tool refuses a bare `y` and executes on `y <valid code>`
  - a wrong code refuses, and the pending call is NOT executed
  - a code cannot be replayed (the scrollback-reading attacker)
  - an ungated tool keeps the historical bare `y`
  - `no` never needs a code — declining stays at least as easy as approving
  - fail closed: a gated tool with no secret configured refuses, rather
    than silently falling back to the bare `y`
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ember.agent import Agent
from ember.channels.base import ChannelMessage
from ember.config import Settings
from ember.identity.store import IdentityStore
from ember.providers.routing import ProviderRouter
from ember.store.repo import Store
from ember.tools.base import ToolResult
from ember.tools.registry import Registry
from ember.totp import _code_for_counter, _decode_secret
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider

SECRET = "JBSWY3DPEHPK3PXP"
GATED_TOOL = "schwab_place_orders"
UNGATED_TOOL = "harmless_tool"


def _current_code() -> str:
    """A code valid for the verifier's real clock — the agent path doesn't
    expose an `at` hook, so tests ride wall-clock like production does."""
    import time

    return _code_for_counter(_decode_secret(SECRET), int(time.time() // 30), 6)


def _make_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    secret: str | None = SECRET,
    gated: str = GATED_TOOL,
) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    monkeypatch.delenv("CONFIRM_TOTP_SECRET", raising=False)
    if secret is not None:
        monkeypatch.setenv("CONFIRM_TOTP_SECRET", secret)
    monkeypatch.setenv("CONFIRM_TOTP_TOOLS", gated)
    return Settings()  # type: ignore[call-arg]


def _msg(text: str, *, key: str = "thread-1", mid: str = "m1") -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key=key,
        user_id="1000",
        text=text,
        message_id=mid,
    )


class _Recording:
    """A confirmation-gated tool that records every execution."""

    description: str = "Pretends to place orders."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"to": {"type": "string"}},
        "additionalProperties": False,
    }
    requires_confirmation: bool = True

    def __init__(self, name: str = GATED_TOOL) -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []

    async def call(self, args: dict[str, Any]) -> ToolResult:
        self.calls.append(args)
        return ToolResult(output={"ok": True}, is_error=False)


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


def _make_agent(
    *, store: Store, settings: Settings, identity: IdentityStore, tool: _Recording
) -> tuple[Agent, FakeChannel, FakeProvider]:
    registry = Registry()
    registry.register(tool)
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
    )
    return agent, channel, primary


async def _set_pending(store: Store, tool_name: str, *, key: str = "thread-1") -> int:
    conv = await store.get_or_create_conversation("discord", key)
    await store.set_pending_tool_confirmation(
        conv.id,
        json.dumps(
            {
                "tool_name": tool_name,
                "args": {"to": "x"},
                "action": f"{tool_name}(...)",
                "set_at_iso": "2026-08-24T12:00:00Z",
            }
        ),
    )
    return conv.id


def _replies(channel: FakeChannel) -> str:
    return "\n".join(s.content for s in channel.sent if s.kind in ("status", "final"))


class TestGatedTool:
    async def test_bare_yes_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        tool = _Recording()
        agent, channel, primary = _make_agent(
            store=opened_store, settings=settings, identity=identity, tool=tool
        )
        await channel.start(agent.handle_turn)
        await _set_pending(opened_store, GATED_TOOL)

        await channel.inject(_msg("y"))

        assert tool.calls == [], "bare `y` must not execute a gated tool"
        assert "code" in _replies(channel).lower()
        assert primary.calls == []

    async def test_valid_code_executes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        tool = _Recording()
        agent, channel, primary = _make_agent(
            store=opened_store, settings=settings, identity=identity, tool=tool
        )
        await channel.start(agent.handle_turn)
        await _set_pending(opened_store, GATED_TOOL)

        await channel.inject(_msg(f"y {_current_code()}"))

        assert tool.calls == [{"to": "x"}]
        assert primary.calls == []

    async def test_wrong_code_does_not_execute(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        tool = _Recording()
        agent, channel, _ = _make_agent(
            store=opened_store, settings=settings, identity=identity, tool=tool
        )
        await channel.start(agent.handle_turn)
        await _set_pending(opened_store, GATED_TOOL)

        await channel.inject(_msg("y 000000"))

        assert tool.calls == []
        assert "reject" in _replies(channel).lower()

    async def test_code_cannot_be_replayed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """The attacker reading scrollback: the same code, used again on a
        freshly-proposed call, must not execute it."""
        settings = _make_settings(monkeypatch, tmp_path)
        tool = _Recording()
        agent, channel, _ = _make_agent(
            store=opened_store, settings=settings, identity=identity, tool=tool
        )
        await channel.start(agent.handle_turn)
        code = _current_code()

        await _set_pending(opened_store, GATED_TOOL)
        await channel.inject(_msg(f"y {code}", mid="m1"))
        assert len(tool.calls) == 1

        await _set_pending(opened_store, GATED_TOOL)
        await channel.inject(_msg(f"y {code}", mid="m2"))
        assert len(tool.calls) == 1, "a redeemed code must not work twice"

    async def test_no_needs_no_code(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        tool = _Recording()
        agent, channel, primary = _make_agent(
            store=opened_store, settings=settings, identity=identity, tool=tool
        )
        await channel.start(agent.handle_turn)
        await _set_pending(opened_store, GATED_TOOL)

        await channel.inject(_msg("no"))

        assert tool.calls == []
        assert "cancel" in _replies(channel).lower()
        assert primary.calls == []

    async def test_prompt_asks_for_a_code(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """The 🔒 prompt must tell the user a code is needed — and must not
        contain one."""
        from ember.agent import _StreamObserver

        prompt = _StreamObserver._format_confirm_prompt(f"{GATED_TOOL}(...)", needs_code=True)
        assert "code" in prompt.lower()
        plain = _StreamObserver._format_confirm_prompt(f"{UNGATED_TOOL}(...)")
        assert "code" not in plain.lower()


class TestUngatedTool:
    async def test_bare_yes_still_works(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """Only listed tools change behavior; everything else keeps the
        historical one-word confirmation."""
        settings = _make_settings(monkeypatch, tmp_path)
        tool = _Recording(name=UNGATED_TOOL)
        agent, channel, _ = _make_agent(
            store=opened_store, settings=settings, identity=identity, tool=tool
        )
        await channel.start(agent.handle_turn)
        await _set_pending(opened_store, UNGATED_TOOL)

        await channel.inject(_msg("y"))

        assert tool.calls == [{"to": "x"}]


class TestFailClosed:
    async def test_gated_tool_with_no_secret_refuses(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """Listing a tool as gated and then losing the secret must refuse,
        never silently reopen the bare-`y` path."""
        settings = _make_settings(monkeypatch, tmp_path, secret=None)
        tool = _Recording()
        agent, channel, _ = _make_agent(
            store=opened_store, settings=settings, identity=identity, tool=tool
        )
        await channel.start(agent.handle_turn)
        await _set_pending(opened_store, GATED_TOOL)

        await channel.inject(_msg("y"))
        assert tool.calls == []
        await _set_pending(opened_store, GATED_TOOL)
        await channel.inject(_msg(f"y {_current_code()}", mid="m2"))
        assert tool.calls == []

    async def test_malformed_secret_refuses(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path, secret="not!base32")
        tool = _Recording()
        agent, channel, _ = _make_agent(
            store=opened_store, settings=settings, identity=identity, tool=tool
        )
        await channel.start(agent.handle_turn)
        await _set_pending(opened_store, GATED_TOOL)

        await channel.inject(_msg(f"y {_current_code()}"))
        assert tool.calls == []
