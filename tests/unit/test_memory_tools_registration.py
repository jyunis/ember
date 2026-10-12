"""`register_memory_tools` (FC-11) — discovery + registry wiring.

Per FC-11:
- Tool discovery happens at startup; results cached for the process
  lifetime.
- Names are exposed as `<server>__<tool>` (here: `memory__<tool>`).
- The local `Registry` then validates args and dispatches per TLC-6.

These tests use an in-process FastMCP server as the upstream (the
existing `test_bridge.py` pattern), connect a real
`McpOutboundClient`, and assert the registry has the prefixed names
afterwards. Round-trip a call to verify wire connectivity.

Tester-flagged ambiguity:
- "Idempotent for the same client" per the docstring is
  underspecified. Either it succeeds and is a no-op, OR it raises a
  clear error (the underlying `Registry.register` already raises on
  duplicate). We test BOTH possibilities (skip-if-not the chosen
  semantics) and flag for the implementer.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from ember.tools.mcp_client import McpOutboundClient
from ember.tools.memory import register_memory_tools
from ember.tools.registry import Registry


# --- Live-server fixture -------------------------------------------------


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _UpstreamHarness:
    """In-process FastMCP server with a couple of fake "memory" tools."""

    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self.port = _free_port()
        self.mcp = FastMCP(
            name="memory",
            host=self.host,
            port=self.port,
            stateless_http=True,
            log_level="WARNING",
        )

        async def echo(text: str) -> dict[str, Any]:
            return {"text": text}

        async def square(n: int) -> dict[str, Any]:
            return {"result": n * n}

        self.mcp.add_tool(echo, name="echo", description="echo back text")
        self.mcp.add_tool(square, name="square", description="square an int")
        self._task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp/"

    async def start(self) -> None:
        self._task = asyncio.create_task(self.mcp.run_streamable_http_async())
        await asyncio.sleep(0.2)
        if self._task.done():
            exc = self._task.exception()
            if exc is not None:
                raise exc

    async def stop(self) -> None:
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None


@pytest.fixture
async def upstream() -> AsyncIterator[_UpstreamHarness]:
    h = _UpstreamHarness()
    await h.start()
    try:
        yield h
    finally:
        await h.stop()


# --- Discovery and registration -----------------------------------------


class TestRegistration:
    async def test_returns_list_of_registered_names(self, upstream: _UpstreamHarness) -> None:
        client = McpOutboundClient(name="memory", url=upstream.url)
        await client.connect()
        try:
            registry = Registry()
            names = await register_memory_tools(registry, client)
            # FC-11: returns the registered names. Both upstream tools
            # land under the `memory__` prefix.
            assert "memory__echo" in names
            assert "memory__square" in names
        finally:
            await client.disconnect()

    async def test_registry_contains_prefixed_names(self, upstream: _UpstreamHarness) -> None:
        client = McpOutboundClient(name="memory", url=upstream.url)
        await client.connect()
        try:
            registry = Registry()
            await register_memory_tools(registry, client)
            assert "memory__echo" in registry.names()
            assert "memory__square" in registry.names()
        finally:
            await client.disconnect()

    async def test_registered_tool_satisfies_protocol(self, upstream: _UpstreamHarness) -> None:
        from ember.tools.base import Tool

        client = McpOutboundClient(name="memory", url=upstream.url)
        await client.connect()
        try:
            registry = Registry()
            await register_memory_tools(registry, client)
            tool = registry.get("memory__echo")
            assert isinstance(tool, Tool)
            # Schema is the upstream's, byte-stable.
            assert isinstance(tool.input_schema, dict)
        finally:
            await client.disconnect()


# --- Round-trip ---------------------------------------------------------


class TestRoundTrip:
    async def test_call_through_registry_round_trips_to_server(
        self, upstream: _UpstreamHarness
    ) -> None:
        client = McpOutboundClient(name="memory", url=upstream.url)
        await client.connect()
        try:
            registry = Registry()
            await register_memory_tools(registry, client)

            result = await registry.call_tool("memory__echo", {"text": "ping"})
            # Whatever the upstream emitted is reachable through the
            # registry's ToolResult.output.
            assert "ping" in repr(result.output)
        finally:
            await client.disconnect()


# --- Idempotency / re-registration --------------------------------------


class TestReregistration:
    async def test_second_call_is_idempotent_or_raises_clearly(
        self, upstream: _UpstreamHarness
    ) -> None:
        # FLAG: spec is ambiguous. The Registry already rejects duplicate
        # names; whether `register_memory_tools` swallows that or surfaces
        # it is up to the implementer. We assert ONE of the two
        # outcomes and document the choice for resolution.
        client = McpOutboundClient(name="memory", url=upstream.url)
        await client.connect()
        try:
            registry = Registry()
            await register_memory_tools(registry, client)

            try:
                names = await register_memory_tools(registry, client)
            except Exception as e:
                # Acceptable: a clear error referencing duplicate
                # registration. Pin to ValueError because that's what
                # `Registry.register` raises today.
                assert isinstance(e, ValueError)
                return

            # Or: idempotent — the same names are still present, no new
            # ones added.
            assert "memory__echo" in names
            assert "memory__square" in names
            assert registry.names().count("memory__echo") == 1
        finally:
            await client.disconnect()
