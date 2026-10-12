"""McpOutboundClient (FC-11) — outbound MCP transport contract.

Per FC-11:
- Per-server connection pool; one connection per upstream MCP server,
  reused across calls.
- Tool discovery at startup; cached for the process lifetime.
- Health: if a server is unreachable at startup, the agent reports
  unavailability and proceeds.

These tests cover:
- Construction signature (name + url kwargs).
- `connect()` against an unreachable URL raises `TransportError` (the
  taxonomy defined in `errors.py`; this is the "unreachable at
  startup" failure mode).
- Successful round-trip via an in-process FastMCP server fixture:
  `list_tools` returns what we registered; `call_tool` round-trips
  args and result.
- `call_tool` on an unknown tool name surfaces as `ToolError` (not a
  raw KeyError or library exception).

We don't read `tools/mcp_client.py`'s body; only its public interface.
The bodies are `...` until the implementer pass.

Tester-flagged ambiguities:
- The exact error type for "unknown tool name" — the spec says
  `ToolError`, the upstream MCP server returns a structured error,
  and how the client maps it is up to the implementer. We assert
  `ToolError` (or any subclass), per the FC-3 / FC-11 taxonomy.
- These tests stand up a live FastMCP server in-process. If that
  proves too slow on CI, mark `@pytest.mark.integration` later.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from ember.errors import ToolError, TransportError
from ember.tools.mcp_client import McpOutboundClient


# --- Fixtures ------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _FastMCPHarness:
    """In-process FastMCP server, started with `run_streamable_http_async`.

    Mirrors the pattern in `bridge/server.py`: spawn a background task,
    sleep briefly so uvicorn binds, and surface bind exceptions
    immediately.
    """

    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self.port = _free_port()
        self.mcp = FastMCP(
            name="harness",
            host=self.host,
            port=self.port,
            stateless_http=True,
            log_level="WARNING",
        )
        self._task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp/"

    def add_echo(self) -> None:
        async def echo(text: str) -> dict[str, Any]:
            return {"text": text}

        self.mcp.add_tool(echo, name="echo", description="echo back text")

    def add_square(self) -> None:
        async def square(n: int) -> dict[str, Any]:
            return {"result": n * n}

        self.mcp.add_tool(square, name="square", description="square an integer")

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
async def harness() -> AsyncIterator[_FastMCPHarness]:
    h = _FastMCPHarness()
    h.add_echo()
    h.add_square()
    await h.start()
    try:
        yield h
    finally:
        await h.stop()


# --- Construction --------------------------------------------------------


class TestConstruction:
    def test_construction_with_name_and_url_does_not_raise(self) -> None:
        McpOutboundClient(name="memory", url="http://localhost:9927/mcp/")

    def test_name_property_round_trips(self) -> None:
        c = McpOutboundClient(name="memory", url="http://localhost:9927/mcp/")
        assert c.name == "memory"

    def test_distinct_names_distinct_clients(self) -> None:
        a = McpOutboundClient(name="memory", url="http://localhost:9927/mcp/")
        b = McpOutboundClient(name="documents", url="http://localhost:9929/mcp/")
        assert a.name == "memory"
        assert b.name == "documents"


# --- Failed connect ------------------------------------------------------


class TestConnectFailure:
    async def test_unreachable_url_raises_transport_error(self) -> None:
        # 127.0.0.1:1 is the canonical "definitely-unused" port. Per
        # FC-11 the unreachable case is "tools unavailable" — implemented
        # as TransportError so main.py can decide whether to abort.
        c = McpOutboundClient(name="dead", url="http://127.0.0.1:1/mcp/")
        with pytest.raises(TransportError):
            await c.connect()


# --- Round-trip via live FastMCP -----------------------------------------


class TestLiveRoundTrip:
    async def test_list_tools_returns_registered_tools(self, harness: _FastMCPHarness) -> None:
        c = McpOutboundClient(name="harness", url=harness.url)
        await c.connect()
        try:
            tools = await c.list_tools()
            names = {t["name"] for t in tools}
            assert "echo" in names
            assert "square" in names
        finally:
            await c.disconnect()

    async def test_list_tools_entries_have_required_fields(self, harness: _FastMCPHarness) -> None:
        # FC-11: each entry has `name`, `description`, `inputSchema`.
        c = McpOutboundClient(name="harness", url=harness.url)
        await c.connect()
        try:
            tools = await c.list_tools()
            for t in tools:
                assert "name" in t
                assert "description" in t
                assert "inputSchema" in t
        finally:
            await c.disconnect()

    async def test_call_tool_round_trips(self, harness: _FastMCPHarness) -> None:
        c = McpOutboundClient(name="harness", url=harness.url)
        await c.connect()
        try:
            result = await c.call_tool("echo", {"text": "hello"})
            # The shape is "whatever the server emitted" (FC-11 docstring).
            # We don't pin the exact wrapper, but the upstream `text`
            # value must round-trip somewhere in the structured output.
            blob = repr(result)
            assert "hello" in blob
        finally:
            await c.disconnect()

    async def test_call_tool_round_trips_typed_args(self, harness: _FastMCPHarness) -> None:
        c = McpOutboundClient(name="harness", url=harness.url)
        await c.connect()
        try:
            result = await c.call_tool("square", {"n": 7})
            assert "49" in repr(result)
        finally:
            await c.disconnect()


# --- Unknown tool surfaces as ToolError ----------------------------------


class TestUnknownTool:
    async def test_unknown_tool_raises_tool_error(self, harness: _FastMCPHarness) -> None:
        # FC-11 docstring: "Raises ToolError on non-success." The server
        # returns an MCP-error which we wrap; do NOT leak KeyError or a
        # raw library exception across module boundaries (see errors.py
        # § "Never raise raw library exceptions across module
        # boundaries").
        c = McpOutboundClient(name="harness", url=harness.url)
        await c.connect()
        try:
            with pytest.raises(ToolError):
                await c.call_tool("does_not_exist", {})
        finally:
            await c.disconnect()


# --- Host-header override scoping ----------------------------------------


class TestHostOverride:
    """`Host: localhost` is a FastMCP-allowlist workaround for Docker DNS
    names and LAN IPs. It must never reach a public FQDN — shared
    frontends (cloudfunctions.net, run.app, …) route on Host and answer
    404, which the MCP SDK reports as the opaque "Session terminated"."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://documents-api:9929/mcp",  # Docker service name
            "http://memory:9927/mcp/",
            "http://192.168.1.50:8080/mcp",  # RFC-1918 LAN IP
            "http://10.0.0.7/mcp",
        ],
        ids=["docker-dns", "docker-dns-2", "lan-ip", "lan-ip-10"],
    )
    def test_dockerish_hosts_get_localhost_override(self, url: str) -> None:
        c = McpOutboundClient(name="x", url=url)
        headers = c._connect_headers()
        assert headers is not None
        assert headers.get("Host") == "localhost"

    @pytest.mark.parametrize(
        "url",
        [
            "https://us-central1-wire-wallet-2395d.cloudfunctions.net/mcpServer",
            "https://example.com/mcp",
            "https://mcp.some-vendor.io/sse",
            "http://8.8.8.8/mcp",  # public IP literal
            "http://localhost:9927/mcp/",
            "http://127.0.0.1:9929/mcp/",
        ],
        ids=["cloudfunctions", "fqdn", "fqdn-sse", "public-ip", "localhost", "loopback"],
    )
    def test_public_and_loopback_hosts_keep_real_host(self, url: str) -> None:
        c = McpOutboundClient(name="x", url=url)
        headers = c._connect_headers()
        assert headers is None or "Host" not in headers

    def test_caller_supplied_host_still_wins(self) -> None:
        c = McpOutboundClient(
            name="x",
            url="http://documents-api:9929/mcp",
            headers={"Host": "custom.example"},
        )
        headers = c._connect_headers()
        assert headers is not None
        assert headers["Host"] == "custom.example"

    def test_caller_headers_preserved_on_public_host(self) -> None:
        c = McpOutboundClient(
            name="x",
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer t"},
        )
        headers = c._connect_headers()
        assert headers == {"Authorization": "Bearer t"}
