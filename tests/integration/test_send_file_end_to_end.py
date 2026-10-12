"""`send_file` through the agent loop and the MCP bridge.

Two routes reach the channel's `send_attachments`, and the tool has to
work on both:

- in-process tool loop — `Agent._handle_turn_inner` forwards
  `ToolResult.metadata["attachments"]` after each dispatch;
- claude_cli/MCP — the bridge invoker calls
  `bridge.active.forward_attachments`, which posts to whichever
  conversation set the active handle.

A rejected path must upload nothing on either route.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.agent import Agent
from ember.bridge.active import clear_active, forward_attachments, set_active
from ember.channels.base import ChannelMessage
from ember.config import Settings
from ember.identity.store import IdentityStore
from ember.providers.base import CompletionResult, ToolCall, Usage
from ember.store.repo import Store
from ember.tools.registry import Registry
from ember.tools.send_file import OUTBOX_SUBDIR, SendFileTool
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


def _msg(text: str = "send me the report") -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key="thread-1",
        user_id="1000",
        text=text,
        message_id="m1",
    )


def _tool_call(paths: list[str]) -> CompletionResult:
    return CompletionResult(
        text="",
        tool_calls=[ToolCall(id="tu_1", name="send_file", args={"paths": paths})],
        usage=Usage(input_tokens=1, output_tokens=1, subscription=True),
    )


async def _run_turn(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paths: list[str],
) -> FakeChannel:
    settings = _make_settings(monkeypatch, tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)

    store = Store(tmp_path / "conversations.db")
    await store.open()
    try:
        identity = IdentityStore(tmp_path / "identity")
        await identity.ensure_initialized()

        registry = Registry()
        registry.register(SendFileTool(data_dir=data_dir))

        provider = FakeProvider()
        provider.name = "claude_cli"
        provider.queue_response(_tool_call(paths))
        provider.queue_text("Sent it over.")

        channel = FakeChannel()
        agent = Agent(
            store=store,
            provider=provider,
            channel=channel,
            settings=settings,
            identity=identity,
            tool_registry=registry,
        )
        await channel.start(agent.handle_turn)
        await channel.inject(_msg())
        return channel
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_agent_loop_uploads_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report = tmp_path / "data" / OUTBOX_SUBDIR / "report.csv"
    report.parent.mkdir(parents=True)
    report.write_text("a,b\n1,2\n")

    channel = await _run_turn(tmp_path=tmp_path, monkeypatch=monkeypatch, paths=[str(report)])

    assert channel.sent_attachments == [("thread-1", [str(report)])]


@pytest.mark.asyncio
async def test_rejected_path_uploads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "not-data" / "secret.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text("nope")

    channel = await _run_turn(tmp_path=tmp_path, monkeypatch=monkeypatch, paths=[str(outside)])

    assert channel.sent_attachments == []


@pytest.mark.asyncio
async def test_bridge_route_forwards_to_active_conversation(tmp_path: Path) -> None:
    """The claude_cli path posts via the module-level active handle."""
    data_dir = tmp_path / "data"
    doc = data_dir / OUTBOX_SUBDIR / "notes.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# notes")

    tool = SendFileTool(data_dir=data_dir)
    result = await tool.call({"paths": [str(doc)]})

    channel = FakeChannel()
    set_active(channel, "thread-7")
    try:
        await forward_attachments(list(result.metadata["attachments"]))
    finally:
        clear_active()

    assert channel.sent_attachments == [("thread-7", [str(doc)])]
