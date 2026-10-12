"""mcp_connector — runtime-mounted outbound MCP servers.

These tests stand up an in-process FastMCP "upstream" (mirroring
`test_memory_tools_registration.py`'s harness), then exercise the
three connector tools end-to-end:

- `mcp_connect` — registers prefixed tools, persists the row,
  appends an audit log line.
- `mcp_list_connected` — surfaces the live entry with status + tools.
- `mcp_disconnect` — closes, unregisters, deletes the row.

`mcp_connect.requires_confirmation` is False (model-side refusal-bias
on TLC-3 tools made it unusable; replaced with audit-log + optional
host allowlist + visible status, matching the gmail_send pattern).
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from ember.store.repo import Store
from ember.tools.mcp_connector import (
    McpConnectTool,
    McpDisconnectTool,
    McpListConnectedTool,
    live_clients,
)
from ember.tools.registry import Registry


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _UpstreamHarness:
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

        async def echo(text: str) -> dict[str, Any]:
            return {"text": text}

        self.mcp.add_tool(echo, name="echo", description="echo back text")
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


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def registry() -> Registry:
    return Registry()


@pytest.fixture(autouse=True)
def _clear_live_clients() -> None:
    """The connector module owns a process-global live-client map.
    Tests run sequentially in the same process, so reset between tests
    to avoid cross-pollination."""
    live_clients().clear()
    yield
    live_clients().clear()


class TestConnect:
    async def test_connect_registers_prefixed_tools_and_persists(
        self, upstream: _UpstreamHarness, registry: Registry, store: Store, tmp_path: Path
    ) -> None:
        log_path = tmp_path / "mcp_connect_log.jsonl"
        tool = McpConnectTool(registry=registry, store=store, log_path=log_path)
        registry.register(tool)

        result = await registry.call_tool(
            "mcp_connect",
            {"name": "harness", "url": upstream.url},
        )
        assert result.is_error is False
        assert result.output["status"] == "connected"
        assert "harness__echo" in result.output["tools"]
        assert "harness__echo" in registry.names()
        # Live client recorded.
        assert "harness" in live_clients()
        # Persisted.
        rows = await store.list_connected_mcps()
        assert rows[0]["name"] == "harness"
        assert rows[0]["url"] == upstream.url
        # Audit logged.
        assert log_path.exists()
        line = log_path.read_text().strip().splitlines()[-1]
        assert '"outcome": "connected"' in line
        assert '"name": "harness"' in line

    async def test_connect_does_not_require_confirmation(
        self, upstream: _UpstreamHarness, registry: Registry, store: Store
    ) -> None:
        # Model-side refusal-bias broke TLC-3 on outbound-action tools
        # (see project memory: gmail_send / soul_update). The connector
        # uses audit-log + allowlist instead — connect must fire on
        # first call without a "yes" round trip.
        tool = McpConnectTool(registry=registry, store=store)
        assert tool.requires_confirmation is False
        registry.register(tool)
        result = await registry.call_tool(
            "mcp_connect",
            {"name": "harness", "url": upstream.url},
        )
        assert result.is_error is False
        assert "harness" in live_clients()

    async def test_connect_rejects_duplicate_name(
        self, upstream: _UpstreamHarness, registry: Registry, store: Store
    ) -> None:
        registry.register(McpConnectTool(registry=registry, store=store))
        first = await registry.call_tool(
            "mcp_connect",
            {"name": "harness", "url": upstream.url},
        )
        assert first.is_error is False
        second = await registry.call_tool(
            "mcp_connect",
            {"name": "harness", "url": upstream.url},
        )
        assert second.is_error
        assert second.output["error"] == "already_connected"

    async def test_connect_unreachable_url_returns_error(
        self, registry: Registry, store: Store
    ) -> None:
        registry.register(McpConnectTool(registry=registry, store=store))
        # Bind a port and never listen — connect attempts will fail.
        port = _free_port()
        result = await registry.call_tool(
            "mcp_connect",
            {"name": "dead", "url": f"http://127.0.0.1:{port}/mcp/"},
        )
        assert result.is_error
        assert result.output["error"] == "connect_failed"
        assert "dead" not in live_clients()
        assert await store.list_connected_mcps() == []

    async def test_allowlist_rejects_unknown_host(
        self, upstream: _UpstreamHarness, registry: Registry, store: Store, tmp_path: Path
    ) -> None:
        log_path = tmp_path / "log.jsonl"
        registry.register(
            McpConnectTool(
                registry=registry,
                store=store,
                allowed_hosts=["api.example.net", "*.example.com"],
                log_path=log_path,
            )
        )
        # 127.0.0.1 isn't in the allowlist.
        result = await registry.call_tool(
            "mcp_connect",
            {"name": "harness", "url": upstream.url},
        )
        assert result.is_error
        assert result.output["error"] == "host_not_allowed"
        assert "harness" not in live_clients()
        # Audit captured the rejection.
        line = log_path.read_text().strip().splitlines()[-1]
        assert '"outcome": "rejected"' in line
        assert '"error": "host_not_allowed"' in line

    async def test_allowlist_wildcard_matches_subdomain(
        self, upstream: _UpstreamHarness, registry: Registry, store: Store
    ) -> None:
        # Allow the loopback so the upstream harness URL is acceptable.
        registry.register(
            McpConnectTool(
                registry=registry,
                store=store,
                allowed_hosts=["*.example.com", "127.0.0.1"],
            )
        )
        result = await registry.call_tool(
            "mcp_connect",
            {"name": "harness", "url": upstream.url},
        )
        assert result.is_error is False
        assert "harness" in live_clients()


class TestList:
    async def test_list_includes_status_and_tools(
        self, upstream: _UpstreamHarness, registry: Registry, store: Store
    ) -> None:
        registry.register(McpConnectTool(registry=registry, store=store))
        registry.register(McpListConnectedTool(registry=registry, store=store))

        await registry.call_tool(
            "mcp_connect",
            {"name": "harness", "url": upstream.url},
        )
        listing = await registry.call_tool("mcp_list_connected", {})
        assert listing.is_error is False
        assert listing.output["count"] == 1
        entry = listing.output["connected"][0]
        assert entry["name"] == "harness"
        assert entry["status"] == "connected"
        assert "harness__echo" in entry["tools"]


class TestDisconnect:
    async def test_disconnect_removes_tools_and_row(
        self, upstream: _UpstreamHarness, registry: Registry, store: Store
    ) -> None:
        registry.register(McpConnectTool(registry=registry, store=store))
        registry.register(McpDisconnectTool(registry=registry, store=store))

        await registry.call_tool(
            "mcp_connect",
            {"name": "harness", "url": upstream.url},
        )
        assert "harness__echo" in registry.names()

        result = await registry.call_tool("mcp_disconnect", {"name": "harness"})
        assert result.is_error is False
        assert result.output["removed"] is True
        assert "harness__echo" in result.output["tools_removed"]
        assert "harness__echo" not in registry.names()
        assert "harness" not in live_clients()
        assert await store.list_connected_mcps() == []

    async def test_disconnect_unknown_is_no_op(self, registry: Registry, store: Store) -> None:
        registry.register(McpDisconnectTool(registry=registry, store=store))
        result = await registry.call_tool("mcp_disconnect", {"name": "does-not-exist"})
        assert result.is_error is False
        assert result.output["removed"] is False
        assert result.output["tools_removed"] == []
