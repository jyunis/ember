"""ClaudeCliProvider relaunch on the MCP-attach race.

`claude --print` connects MCP servers asynchronously after emitting its
init event. If the model issues a tool call before the in-process agent
bridge finishes connecting, it gets a hard "No such tool available:
mcp__agent__<name>" error and the whole turn loses its agent tools. The
provider detects that signature and relaunches a fresh subprocess (which
almost always wins the race), resuming the advanced session.

Follows the fake-subprocess pattern in test_claude_cli_tool_filter.py.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ember.providers.base import Message
from ember.providers.claude_cli import ClaudeCliProvider


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


def _race_failed_lines() -> list[bytes]:
    """A turn where the agent server never attached: the model's tool call
    came back 'No such tool available' and it gave up (result not an error)."""
    return [
        b'{"type": "assistant", "message": {"content": [{"type": "tool_use", '
        b'"name": "mcp__agent__web_fetch", "input": {"url": "https://x"}}]}}\n',
        b'{"type": "user", "message": {"content": [{"type": "tool_result", '
        b'"is_error": true, "content": "No such tool available: '
        b'mcp__agent__web_fetch"}]}}\n',
        b'{"type": "result", "is_error": false, "result": "Sorry, the web '
        b'tool seems unavailable.", "session_id": "sess-1", '
        b'"usage": {"input_tokens": 1, "output_tokens": 1}}\n',
    ]


def _success_lines(text: str = "200 OK", session: str = "sess-1") -> list[bytes]:
    return [
        (
            '{"type": "result", "is_error": false, "result": "%s", '
            '"session_id": "%s", "usage": {"input_tokens": 2, '
            '"output_tokens": 2}}\n' % (text, session)
        ).encode(),
    ]


@pytest.fixture
def fake_runs(monkeypatch: pytest.MonkeyPatch):
    """Queue of stdout-line-lists; each subprocess launch pops the next."""
    queue: list[list[bytes]] = []
    launches: list[list[str]] = []

    async def _fake_create(*args: Any, **kwargs: Any):  # noqa: ANN401, ARG001
        launches.append(list(args))
        lines = queue.pop(0)
        return _FakeProc(lines)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    return queue, launches


class TestMcpAttachRelaunch:
    async def test_relaunches_on_missing_agent_tool_then_succeeds(self, fake_runs) -> None:
        queue, launches = fake_runs
        queue.append(_race_failed_lines())  # attempt 1: race lost
        queue.append(_success_lines("200 OK"))  # attempt 2: clean

        provider = ClaudeCliProvider(
            allowed_tools="mcp__agent__*",
            mcp_config_paths=["/data/agent-mcp.json"],
        )
        result = await provider.complete(system="", messages=[Message("user", "fetch https://x")])

        assert len(launches) == 2  # relaunched exactly once
        assert result.text == "200 OK"  # returns the successful attempt

    async def test_clean_first_run_does_not_relaunch(self, fake_runs) -> None:
        queue, launches = fake_runs
        queue.append(_success_lines("hello"))

        provider = ClaudeCliProvider(
            allowed_tools="mcp__agent__*",
            mcp_config_paths=["/data/agent-mcp.json"],
        )
        result = await provider.complete(system="", messages=[Message("user", "hi")])

        assert len(launches) == 1  # no wasteful relaunch
        assert result.text == "hello"

    async def test_relaunch_is_bounded(self, fake_runs) -> None:
        # Race lost on every attempt: must stop after the cap, not loop forever,
        # and surface the last result rather than raising.
        queue, launches = fake_runs
        for _ in range(5):
            queue.append(_race_failed_lines())

        provider = ClaudeCliProvider(
            allowed_tools="mcp__agent__*",
            mcp_config_paths=["/data/agent-mcp.json"],
        )
        result = await provider.complete(system="", messages=[Message("user", "fetch https://x")])

        assert len(launches) == 3  # _MCP_ATTACH_MAX_ATTEMPTS
        assert "unavailable" in result.text
