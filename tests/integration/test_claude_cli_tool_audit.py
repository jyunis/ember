"""Bridged tool-call audit (UoW-4 step 5 / TLC-8).

`claude_cli` turns execute tools via the MCP bridge; historically none
of those calls reached `messages.tool_calls_json` — bridge logs were
the only record. The provider now parses the stream's `tool_use` /
`tool_result` events into `CompletionResult.executed_tool_calls`
(bare names, observed status), and the agent loop persists them on the
assistant row WITHOUT ever dispatching them (they already ran).

Covers:
  - stream parsing: name prefix strip, args capture, ok/error status,
    id-based and FIFO pairing, multiple calls, stream order;
  - `tool_calls` (dispatch-requesting) stays empty for claude_cli;
  - MCP-attach relaunch accumulates both attempts' calls;
  - agent loop: assistant row carries the audit; a registry tool named
    by an executed call is NOT re-dispatched;
  - store round-trip: `status` survives; legacy rows without it load.

Fake-subprocess pattern follows test_claude_cli_mcp_retry.py.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ember.agent import Agent
from ember.channels.base import ChannelMessage
from ember.config import Settings
from ember.providers.base import CompletionResult, Message, ToolCall, Usage
from ember.providers.claude_cli import ClaudeCliProvider
from ember.store.repo import Store
from ember.tools.base import ToolResult
from ember.tools.registry import Registry
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider

# --- Fake subprocess plumbing (pattern: test_claude_cli_mcp_retry) ---------


class _FakeStream:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def __aiter__(self) -> "_FakeStream":
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)

    async def read(self) -> bytes:
        return b""


class _FakeProc:
    def __init__(self, stdout_lines: list[bytes]) -> None:
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream([])

    async def wait(self) -> int:
        return 0


@pytest.fixture
def fake_runs(monkeypatch: pytest.MonkeyPatch):
    queue: list[list[bytes]] = []
    launches: list[list[str]] = []

    async def _fake_create(*args: Any, **kwargs: Any):  # noqa: ANN401, ARG001
        launches.append(list(args))
        lines = queue.pop(0)
        return _FakeProc(lines)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    return queue, launches


def _ev(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _tool_use(name: str, args: dict, *, tu_id: str | None = None) -> bytes:
    block: dict[str, Any] = {"type": "tool_use", "name": name, "input": args}
    if tu_id is not None:
        block["id"] = tu_id
    return _ev({"type": "assistant", "message": {"content": [block]}})


def _tool_result(*, tu_id: str | None = None, is_error: bool = False) -> bytes:
    block: dict[str, Any] = {"type": "tool_result", "content": "…", "is_error": is_error}
    if tu_id is not None:
        block["tool_use_id"] = tu_id
    return _ev({"type": "user", "message": {"content": [block]}})


def _result(text: str = "done", session: str = "sess-1") -> bytes:
    return _ev(
        {
            "type": "result",
            "is_error": False,
            "result": text,
            "session_id": session,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )


def _provider() -> ClaudeCliProvider:
    return ClaudeCliProvider(
        allowed_tools="mcp__agent__*",
        mcp_config_paths=["/data/agent-mcp.json"],
    )


async def _complete(queue: list[list[bytes]], lines: list[bytes]) -> CompletionResult:
    queue.append(lines)
    return await _provider().complete(system="", messages=[Message("user", "go")])


# --- Provider stream parsing ------------------------------------------------


class TestExecutedToolCallParsing:
    async def test_call_with_id_paired_and_prefix_stripped(self, fake_runs) -> None:
        queue, _ = fake_runs
        result = await _complete(
            queue,
            [
                _tool_use("mcp__agent__history_search", {"query": "mites"}, tu_id="tu_1"),
                _tool_result(tu_id="tu_1"),
                _result(),
            ],
        )
        assert result.tool_calls == []  # nothing to dispatch
        assert len(result.executed_tool_calls) == 1
        call = result.executed_tool_calls[0]
        assert call.name == "history_search"  # bare name, like in-process rows
        assert call.args == {"query": "mites"}
        assert call.id == "tu_1"
        assert call.status == "ok"

    async def test_error_result_marks_status_error(self, fake_runs) -> None:
        queue, _ = fake_runs
        result = await _complete(
            queue,
            [
                _tool_use("mcp__agent__web_fetch", {"url": "https://x"}, tu_id="tu_1"),
                _tool_result(tu_id="tu_1", is_error=True),
                _result(),
            ],
        )
        assert result.executed_tool_calls[0].status == "error"

    async def test_idless_events_pair_fifo(self, fake_runs) -> None:
        # The CLI has emitted tool_use blocks without ids (see the
        # mcp-retry fixture); results then pair in stream order.
        queue, _ = fake_runs
        result = await _complete(
            queue,
            [
                _tool_use("mcp__agent__a", {"n": 1}),
                _tool_use("mcp__agent__b", {"n": 2}),
                _tool_result(is_error=True),  # → a
                _tool_result(),  # → b
                _result(),
            ],
        )
        assert [(c.name, c.status) for c in result.executed_tool_calls] == [
            ("a", "error"),
            ("b", "ok"),
        ]

    async def test_unpaired_call_keeps_status_none(self, fake_runs) -> None:
        queue, _ = fake_runs
        result = await _complete(
            queue,
            [_tool_use("mcp__agent__slow", {}, tu_id="tu_9"), _result()],
        )
        assert result.executed_tool_calls[0].status is None

    async def test_multiple_calls_keep_stream_order(self, fake_runs) -> None:
        queue, _ = fake_runs
        result = await _complete(
            queue,
            [
                _tool_use("mcp__agent__memory__semantic_search", {"q": "x"}, tu_id="t1"),
                _tool_result(tu_id="t1"),
                _tool_use("mcp__agent__history_search", {"query": "y"}, tu_id="t2"),
                _tool_result(tu_id="t2"),
                _result(),
            ],
        )
        assert [c.name for c in result.executed_tool_calls] == [
            "memory__semantic_search",
            "history_search",
        ]

    async def test_non_bridge_tool_name_kept_verbatim(self, fake_runs) -> None:
        queue, _ = fake_runs
        result = await _complete(
            queue,
            [
                _tool_use("WebSearch", {"query": "x"}, tu_id="t1"),
                _tool_result(tu_id="t1"),
                _result(),
            ],
        )
        assert result.executed_tool_calls[0].name == "WebSearch"

    async def test_text_only_turn_has_no_executed_calls(self, fake_runs) -> None:
        queue, _ = fake_runs
        result = await _complete(queue, [_result("hello")])
        assert result.executed_tool_calls == []

    async def test_mcp_relaunch_accumulates_both_attempts(self, fake_runs) -> None:
        # Attempt 1 loses the MCP-attach race (its failed call is still
        # part of the turn's audit); attempt 2 succeeds.
        queue, launches = fake_runs
        queue.append(
            [
                _tool_use("mcp__agent__web_fetch", {"url": "https://x"}, tu_id="t1"),
                _ev(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "t1",
                                    "is_error": True,
                                    "content": "No such tool available: mcp__agent__web_fetch",
                                }
                            ]
                        },
                    }
                ),
                _result("gave up"),
            ]
        )
        queue.append(
            [
                _tool_use("mcp__agent__web_fetch", {"url": "https://x"}, tu_id="t2"),
                _tool_result(tu_id="t2"),
                _result("200 OK"),
            ]
        )
        result = await _provider().complete(system="", messages=[Message("user", "fetch")])
        assert len(launches) == 2
        assert [(c.name, c.status) for c in result.executed_tool_calls] == [
            ("web_fetch", "error"),
            ("web_fetch", "ok"),
        ]


# --- Agent loop: audit persisted, never dispatched --------------------------


class _CountingTool:
    name = "history_search"
    description = "test stub"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}
    requires_confirmation = False

    def __init__(self) -> None:
        self.calls = 0

    async def call(self, args: dict[str, Any]) -> ToolResult:
        self.calls += 1
        return ToolResult(output={"ok": True})


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


class TestAgentPersistsExecutedCalls:
    async def test_assistant_row_carries_audit_and_no_dispatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _make_settings(monkeypatch, tmp_path)
        store = Store(tmp_path / "conversations.db")
        await store.open()
        try:
            provider = FakeProvider()
            channel = FakeChannel()
            stub = _CountingTool()
            registry = Registry()
            registry.register(stub)
            agent = Agent(
                store=store,
                provider=provider,
                channel=channel,
                settings=settings,
                tool_registry=registry,
            )
            await channel.start(agent.handle_turn)

            executed = [
                ToolCall(id="t1", name="history_search", args={"query": "mites"}, status="ok")
            ]
            provider.queue_response(
                CompletionResult(
                    text="found it",
                    executed_tool_calls=executed,
                    usage=Usage(1, 1, subscription=True),
                )
            )
            await channel.inject(
                ChannelMessage(
                    channel="discord",
                    conversation_key="thread-1",
                    user_id="1000",
                    text="search old convos",
                    message_id="m1",
                )
            )

            # Executed calls are audit-only: the registry tool whose bare
            # name matches must NOT have been re-dispatched.
            assert stub.calls == 0

            conv = await store.get_or_create_conversation("discord", "thread-1")
            msgs = await store.recent_messages(conv.id, limit=10)
            assistant = [m for m in msgs if m.role == "assistant"][-1]
            assert assistant.tool_calls is not None
            assert [(c.name, c.args, c.status) for c in assistant.tool_calls] == [
                ("history_search", {"query": "mites"}, "ok")
            ]
        finally:
            await store.close()


# --- Store round-trip -------------------------------------------------------


class TestStatusRoundTrip:
    async def test_status_survives_persist_and_hydrate(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "conversations.db")
        await store.open()
        try:
            conv = await store.get_or_create_conversation("discord", "t")
            await store.append_message(
                conv.id,
                "assistant",
                "x",
                tool_calls=[ToolCall(id="a", name="n", args={"k": 1}, status="error")],
            )
            msgs = await store.recent_messages(conv.id, limit=5)
            call = msgs[-1].tool_calls[0]
            assert (call.id, call.name, call.args, call.status) == ("a", "n", {"k": 1}, "error")
        finally:
            await store.close()

    async def test_legacy_json_without_status_loads_as_none(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "conversations.db")
        await store.open()
        try:
            conv = await store.get_or_create_conversation("discord", "t")
            mid = await store.append_message(conv.id, "assistant", "x")
            await store._c.execute(  # pre-status row shape (May/June 2026 era)
                "UPDATE messages SET tool_calls_json = ? WHERE id = ?",
                (json.dumps([{"id": "a", "name": "n", "args": {}}]), mid),
            )
            await store._c.commit()
            msgs = await store.recent_messages(conv.id, limit=5)
            assert msgs[-1].tool_calls[0].status is None
        finally:
            await store.close()
