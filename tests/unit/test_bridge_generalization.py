"""Bridge generalization (FC-4) — post-v3 contract.

Pre-v3 the bridge's `_add_one` had an `if tool.name == "echo"` branch
and raised `NotImplementedError` for everything else. v3 generalizes
that: any tool registered on the local `Registry` is exposed over the
MCP bridge with byte-identical schema (FC-4 schema parity).

These tests:
- Register two tools (echo + a synthetic memory-like tool with a
  non-trivial schema) on the local registry.
- Construct the Bridge — must not raise. (Pre-v3 it would have raised
  `NotImplementedError` on the second tool.)
- Optional: use a real MCP client to hit the bridge's HTTP endpoint
  and assert both tools are advertised; their `inputSchema` must equal
  what the local registry has.

Tester-flagged ambiguity:
- "Schema parity" — the spec says byte-equal. JSON-schema "byte-equal"
  is interpreted as "deep equality of the dict structure" — passing
  through `json.dumps` round-trip gives the same string. Any
  implementer-side normalization (e.g. adding a `title` from the
  Python signature) is a parity violation.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from ember.bridge.server import Bridge
from ember.tools.base import Tool, ToolResult
from ember.tools.echo import EchoTool
from ember.tools.registry import Registry


# --- Synthetic memory-like tool used to exercise the generalized branch -


class _MemorySearchTool(Tool):
    """Hand-rolled stand-in for what an MCP-discovered memory tool
    would look like — non-trivial schema with required + optional
    fields. Used to verify the bridge no longer hard-codes `echo`."""

    name: str = "memory__semantic_search"
    description: str = "Search the memory store"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "what to search"},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    requires_confirmation: bool = False

    async def call(self, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output={"hits": [{"q": args.get("query")}]})


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --- Construction ------------------------------------------------------


class TestConstructionWithMultipleTools:
    def test_construction_with_echo_plus_memory_tool_does_not_raise(self) -> None:
        # Pre-v3 this raised NotImplementedError. Post-v3 the bridge
        # accepts any Tool.
        reg = Registry()
        reg.register(EchoTool())
        reg.register(_MemorySearchTool())
        Bridge(reg, host="127.0.0.1", port=_free_port())

    def test_construction_with_only_memory_like_tool_does_not_raise(self) -> None:
        # The `if tool.name == "echo"` shortcut going away means non-
        # echo tools work in isolation.
        reg = Registry()
        reg.register(_MemorySearchTool())
        Bridge(reg, host="127.0.0.1", port=_free_port())


# --- Live wire-protocol round-trip ------------------------------------


@pytest.fixture
async def started_bridge() -> AsyncIterator[Bridge]:
    reg = Registry()
    reg.register(EchoTool())
    reg.register(_MemorySearchTool())
    bridge = Bridge(reg, host="127.0.0.1", port=_free_port())
    await bridge.start()
    # Give the server a moment beyond bridge.start()'s 0.1s sleep so
    # the streamable-HTTP endpoint is ready for requests.
    await asyncio.sleep(0.2)
    try:
        yield bridge
    finally:
        await bridge.stop()


def _expected_schema(tool: Tool) -> dict[str, Any]:
    return tool.input_schema


class TestBridgeAdvertisesAllTools:
    async def test_both_tools_listed_over_mcp(self, started_bridge: Bridge) -> None:
        # Hit the bridge's HTTP endpoint with a real MCP client and
        # confirm both tools are advertised.
        url = started_bridge.url + "/"  # FastMCP expects a trailing /
        async with streamablehttp_client(url) as (read, write, _get_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                resp = await session.list_tools()
                names = {t.name for t in resp.tools}
                assert "echo" in names
                assert "memory__semantic_search" in names


class TestBridgeSchemaParity:
    async def test_advertised_schema_matches_registry_input_schema(
        self, started_bridge: Bridge
    ) -> None:
        # FC-4: byte-equal schema between local registry and the MCP
        # the bridge advertises.
        url = started_bridge.url + "/"
        async with streamablehttp_client(url) as (read, write, _get_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                resp = await session.list_tools()
                advertised = {t.name: t.inputSchema for t in resp.tools}

        echo_tool = EchoTool()
        memory_tool = _MemorySearchTool()
        assert advertised["echo"] == _expected_schema(echo_tool)
        assert advertised["memory__semantic_search"] == _expected_schema(memory_tool)


class TestBridgeCallsThroughToRegistry:
    async def test_call_tool_on_memory_search_round_trips(self, started_bridge: Bridge) -> None:
        # The bridge dispatches MCP `call_tool` back to the registry's
        # `call_tool` (which validates against the schema and invokes
        # the underlying Tool).
        url = started_bridge.url + "/"
        async with streamablehttp_client(url) as (read, write, _get_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "memory__semantic_search",
                    {"query": "ping"},
                )
        # The result blob carries our synthetic output — `query` round-
        # trips back as `q` (per `_MemorySearchTool.call`).
        blob = repr(result)
        assert "ping" in blob


# --- Confirmation marker round-trip (TLC-3 regression) ---------------


class _ConfirmGatedTool(Tool):
    """Stand-in for a `requires_confirmation=True` tool (e.g. profile_update).
    `call` is a no-op because the registry short-circuits before reaching
    it on the unconfirmed path."""

    name: str = "confirm_gated_noop"
    description: str = "Confirmation-gated no-op for round-trip testing"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": ["x"],
        "additionalProperties": False,
    }
    requires_confirmation: bool = True

    async def call(self, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output={"called": True, "x": args["x"]})


@pytest.fixture
async def started_bridge_with_confirm_tool() -> AsyncIterator[Bridge]:
    reg = Registry()
    reg.register(_ConfirmGatedTool())
    bridge = Bridge(reg, host="127.0.0.1", port=_free_port())
    await bridge.start()
    await asyncio.sleep(0.2)
    try:
        yield bridge
    finally:
        await bridge.stop()


class TestBridgeConfirmationMarkerPassthrough:
    """Regression: the bridge must NOT raise on the confirmation_required
    marker. The registry returns it as `is_error=True` so in-process
    callers can short-circuit, but raising at the bridge boundary turns
    the structured marker into `repr(dict)` text in the MCP error body
    — the agent observer then can't parse it as JSON, so the 🔒 confirm
    prompt never emits and pending_tool_confirmation is never persisted.
    Pass the marker through as a normal MCP result instead.
    """

    async def test_confirmation_marker_returned_as_structured_result(
        self, started_bridge_with_confirm_tool: Bridge
    ) -> None:
        url = started_bridge_with_confirm_tool.url + "/"
        async with streamablehttp_client(url) as (read, write, _get_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "confirm_gated_noop",
                    {"x": "hello"},
                )

        # Must NOT be flagged as an MCP-level error — that's what would
        # cause FastMCP to serialize it as repr(dict) text.
        assert result.isError is False, (
            "confirmation marker leaked through as MCP-level error; "
            "agent observer will see Python repr text and fail to detect it"
        )
        # Bridge reshapes the registry's `error` marker into a non-error
        # status shape so the model doesn't read the "error" field and
        # narrate failure. Both fields the observer keys off + the model-
        # facing instruction must be present and JSON-parseable.
        import json as _json

        text_blocks = [getattr(c, "text", "") for c in result.content if hasattr(c, "text")]
        joined = "\n".join(text_blocks)
        parsed = _json.loads(joined)
        assert parsed.get("status") == "awaiting_user_confirmation"
        assert "confirm_gated_noop" in parsed.get("action", "")
        assert "do not retry" in parsed.get("message", "").lower(), (
            "model-facing message must instruct the model not to retry/narrate failure"
        )
