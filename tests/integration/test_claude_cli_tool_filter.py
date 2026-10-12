"""ClaudeCliProvider per-call tool filter (FC-2, v7).

When `tools` is non-None on `complete()`, the provider builds
`--allowed-tools` from `[f"mcp__agent__{t.name}" for t in tools]`
(comma-joined) for that call. When `tools` is None, the constructor's
`_allowed_tools` stays in effect (current v0.5 behavior).

The test follows the existing pattern in
`tests/unit/test_claude_cli_args.py` — monkeypatch
`asyncio.create_subprocess_exec` to capture argv.
"""

from __future__ import annotations

from typing import Any

import pytest

from ember.providers.base import Message, ToolSpec
from ember.providers.claude_cli import ClaudeCliProvider


@pytest.fixture
def captured_args(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    captured: list[list[str]] = []

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
        def __init__(self) -> None:
            result_event = (
                b'{"type": "result", "is_error": false, "result": "ok", '
                b'"session_id": "fake-sess", "usage": {"input_tokens": 1, '
                b'"output_tokens": 1}}\n'
            )
            self.stdout = _FakeStream([result_event])
            self.stderr = _FakeStream([])

        async def wait(self) -> int:
            return 0

    async def _fake_create(*args: Any, **kwargs: Any):  # noqa: ANN401, ARG001
        captured.append(list(args))
        return _FakeProc()

    import asyncio

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    return captured


def _spec(name: str, description: str = "stub") -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )


class TestPerCallToolsFilter:
    async def test_tools_arg_overrides_allowed_tools(self, captured_args: list[list[str]]) -> None:
        # Constructor's `_allowed_tools` is "*" but the per-call tools
        # arg should narrow to a specific allowlist.
        provider = ClaudeCliProvider(
            allowed_tools="mcp__agent__*",
            mcp_config_paths=["/data/agent-mcp.json"],
        )
        await provider.complete(
            system="",
            messages=[Message("user", "hi")],
            tools=[_spec("mealie_search")],
        )

        assert len(captured_args) == 1
        argv = captured_args[0]
        assert "--allowed-tools" in argv
        i = argv.index("--allowed-tools")
        assert argv[i + 1] == "mcp__agent__mealie_search"

    async def test_multiple_tools_comma_joined(self, captured_args: list[list[str]]) -> None:
        provider = ClaudeCliProvider(
            allowed_tools="mcp__agent__*",
            mcp_config_paths=["/data/agent-mcp.json"],
        )
        await provider.complete(
            system="",
            messages=[Message("user", "hi")],
            tools=[
                _spec("alpha"),
                _spec("beta"),
                _spec("gamma"),
            ],
        )
        argv = captured_args[0]
        i = argv.index("--allowed-tools")
        assert argv[i + 1] == "mcp__agent__alpha,mcp__agent__beta,mcp__agent__gamma"

    async def test_tools_none_keeps_constructor_allowlist(
        self, captured_args: list[list[str]]
    ) -> None:
        # With `tools=None`, the constructor-supplied allowlist is what
        # ends up on the wire (current v0.5 behavior preserved).
        provider = ClaudeCliProvider(
            allowed_tools="mcp__agent__*",
            mcp_config_paths=["/data/agent-mcp.json"],
        )
        await provider.complete(system="", messages=[Message("user", "hi")])

        argv = captured_args[0]
        assert "--allowed-tools" in argv
        i = argv.index("--allowed-tools")
        assert argv[i + 1] == "mcp__agent__*"

    async def test_empty_tools_list_yields_empty_allowlist(
        self, captured_args: list[list[str]]
    ) -> None:
        # Strict allowlist semantics: `tools=[]` means "no tools allowed
        # for this call." Per the brief, the provider should pass
        # `--allowed-tools` with an empty value (or omit it / use the v0
        # blanket disable). We assert that the prior constructor-level
        # `mcp__agent__*` does NOT leak through unchanged when `tools=[]`.
        provider = ClaudeCliProvider(
            allowed_tools="mcp__agent__*",
            mcp_config_paths=["/data/agent-mcp.json"],
        )
        await provider.complete(
            system="",
            messages=[Message("user", "hi")],
            tools=[],
        )

        argv = captured_args[0]
        # Either: no `--allowed-tools mcp__agent__*` (constructor leak)
        # OR `--allowed-tools` followed by an empty/absent value.
        # The strict interpretation: `mcp__agent__*` is absent.
        if "--allowed-tools" in argv:
            i = argv.index("--allowed-tools")
            assert argv[i + 1] != "mcp__agent__*"
