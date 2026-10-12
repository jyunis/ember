"""mcp_connector — runtime-mounted outbound MCP servers.

Three model-facing tools:

- `mcp_connect`         — open an outbound MCP, run prefix-discovery,
                          persist the row.
- `mcp_disconnect`      — close one and unregister its tools.
- `mcp_list_connected`  — current state of every live outbound MCP.

Live `McpOutboundClient` instances are owned in this module's
process-global `_LIVE_CLIENTS` map, keyed by name. The Store row in
`connected_mcps` is the persistence layer; `main.py` replays it at boot
so connections survive container restarts.

**Security model — bounds, not gates.** `mcp_connect` does NOT use
TLC-3 confirmation. The model has a documented refusal-bias against
calling `requires_confirmation=True` tools (see project memory: same
pattern that broke `gmail_send` and `soul_update`). Instead, we apply
the gmail_send pattern: trust the model on *what*, bound the blast
radius via:

  1. Optional `MCP_CONNECT_ALLOWED_HOSTS` allowlist. Empty = trust the
     model (matches the deployment posture; the host is on a private
     LAN). When set, hosts not matching are rejected before any socket
     opens.
  2. Persistent JSONL audit log at `<data_dir>/mcp_connect_log.jsonl`
     for every attempt (success and failure).
  3. Visible status emission via the channel adapter — the user sees
     `🔌 mcp_connect: <name> -> <url>` in real time.
"""

from __future__ import annotations

import datetime
import fnmatch
import json
import logging
import urllib.parse
from pathlib import Path
from typing import Any

from ember.errors import ToolError, TransportError
from ember.tools.base import ToolResult
from ember.tools.mcp_client import McpOutboundClient
from ember.tools.memory import _register_prefixed
from ember.tools.registry import Registry

_log = logging.getLogger("ember.tools.mcp_connector")

# Process-global live-client map. Owned by this module so all three
# tools see the same view, and main.py's boot-replay can populate it
# without going through a tool call.
_LIVE_CLIENTS: dict[str, McpOutboundClient] = {}

_ALLOWED_TRANSPORTS = ("streamableHttp", "sse")


def live_clients() -> dict[str, McpOutboundClient]:
    """Expose the live-client map (read-only intent) for main.py boot
    replay and shutdown drain. Mutating callers must hold the same
    invariants the tools enforce: prefix-registered tools in `Registry`
    track 1:1 with entries here."""
    return _LIVE_CLIENTS


class _McpConnectorBase:
    requires_confirmation: bool = False

    def __init__(self, *, registry: Registry, store: Any) -> None:
        self._registry = registry
        self._store = store


class McpConnectTool(_McpConnectorBase):
    name = "mcp_connect"
    description = (
        "Open an outbound MCP server connection and register its tools "
        "into this agent's tool registry, prefixed with `<name>__`. "
        "Persisted — the connection is automatically re-attached on "
        "container restart. CALL THIS TOOL DIRECTLY when a skill's "
        "compatibility note or `references/setup.md` declares an MCP "
        "server URL that isn't yet mounted; do not ask permission "
        "first. The host enforces an optional allowlist + writes a "
        "persistent audit log; failed attempts are reported back, not "
        "silenced. After connecting, the upstream MCP's tools are "
        "registered as `<name>__<tool>` and are usually callable in "
        "the same turn. If a call still reports 'No such tool "
        "available', do NOT retry or reconnect — the host refreshes "
        "your tool list at the start of the next turn; say the mount "
        "succeeded and use the tools next turn."
    )
    requires_confirmation = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Short identifier for this connection (used as the "
                    "tool prefix and primary key). Lowercase letters, "
                    "digits, hyphens, and underscores."
                ),
            },
            "transport": {
                "type": "string",
                "enum": list(_ALLOWED_TRANSPORTS),
                "default": "streamableHttp",
                "description": (
                    "MCP transport. `streamableHttp` is the modern "
                    "single-endpoint flow (server URL ends in `/mcp/` "
                    "or similar). `sse` is the legacy two-endpoint "
                    "flow — GET the SSE URL, server emits an `endpoint` "
                    "event naming the POST URL for requests."
                ),
            },
            "url": {
                "type": "string",
                "description": (
                    "Endpoint URL. For `streamableHttp` this is the "
                    "single MCP endpoint; for `sse` this is the SSE "
                    "stream URL (typically ending in `/sse`)."
                ),
            },
            "headers": {
                "type": ["object", "null"],
                "description": (
                    "Optional HTTP headers to send with every request "
                    '(e.g. `{"Authorization": "Bearer ..."}`). '
                    "Stored verbatim. Pass null/omit when the upstream "
                    "MCP exposes its own auth tool (e.g. `connect` for "
                    "exchange-code flows)."
                ),
                "additionalProperties": {"type": "string"},
                "default": None,
            },
        },
        "required": ["name", "url"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        *,
        registry: Registry,
        store: Any,
        allowed_hosts: list[str] | None = None,
        log_path: Path | None = None,
    ) -> None:
        super().__init__(registry=registry, store=store)
        self._allowed_hosts: list[str] = list(allowed_hosts or [])
        self._log_path = log_path

    async def call(self, args: dict[str, Any]) -> ToolResult:
        name = args["name"]
        transport = args.get("transport") or "streamableHttp"
        url = args["url"]
        headers = args.get("headers") or None

        if transport not in _ALLOWED_TRANSPORTS:
            return self._reject(
                name=name,
                url=url,
                error="unsupported_transport",
                message=f"unsupported transport: {transport}",
            )

        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if not self._host_allowed(host):
            return self._reject(
                name=name,
                url=url,
                error="host_not_allowed",
                message=(
                    f"host {host!r} is not in MCP_CONNECT_ALLOWED_HOSTS; "
                    "ask the user to add it before retrying"
                ),
            )

        if name in _LIVE_CLIENTS:
            return self._reject(
                name=name,
                url=url,
                error="already_connected",
                message=(
                    f"an MCP named {name!r} is already connected; "
                    "call mcp_disconnect first if you want to replace it"
                ),
            )

        # Reject names whose prefix would collide with already-registered
        # tools (e.g. another skill's static tools).
        prefix = f"{name}__"
        existing = self._registry.names_with_prefix(prefix)
        if existing:
            return self._reject(
                name=name,
                url=url,
                error="name_collision",
                message=(
                    f"the prefix {prefix!r} would collide with "
                    f"{len(existing)} existing tool(s); pick a different name"
                ),
                extra={"colliding_tools": existing},
            )

        client = McpOutboundClient(name=name, url=url, headers=headers, transport=transport)
        try:
            await client.connect()
        except TransportError as e:
            return self._reject(
                name=name,
                url=url,
                error="connect_failed",
                message=str(e),
            )
        except Exception as e:
            _log.exception("mcp_connect %s raised", name)
            return self._reject(
                name=name,
                url=url,
                error="connect_failed",
                message=f"{type(e).__name__}: {e}",
            )

        try:
            registered = await _register_prefixed(self._registry, client)
        except Exception as e:
            _log.exception("mcp_connect %s tool registration failed", name)
            try:
                await client.disconnect()
            except Exception:
                _log.debug("disconnect after failed registration noisy", exc_info=True)
            return self._reject(
                name=name,
                url=url,
                error="registration_failed",
                message=f"{type(e).__name__}: {e}",
            )

        _LIVE_CLIENTS[name] = client

        try:
            await self._store.save_connected_mcp(
                name=name, transport=transport, url=url, headers=headers
            )
        except Exception:
            # Persistence failure is non-fatal for the in-process state
            # (the connection is live and useful right now), but the row
            # won't replay on restart. Log loudly.
            _log.exception("mcp_connect %s: failed to persist row", name)

        self._audit(
            outcome="connected",
            name=name,
            url=url,
            transport=transport,
            tool_count=len(registered),
        )

        return ToolResult(
            output={
                "status": "connected",
                "name": name,
                "url": url,
                "transport": transport,
                "tools": registered,
                "tool_count": len(registered),
            }
        )

    def _host_allowed(self, host: str) -> bool:
        """Return True if `host` matches any allowlist pattern, OR the
        allowlist is empty (= unrestricted). Patterns are exact OR
        `*.example.com` (matches `example.com` AND any subdomain)."""
        if not self._allowed_hosts:
            return True
        host = (host or "").lower()
        for pattern in self._allowed_hosts:
            pattern = pattern.lower()
            if pattern == host:
                return True
            if pattern.startswith("*.") and (host == pattern[2:] or host.endswith(pattern[1:])):
                return True
            if fnmatch.fnmatch(host, pattern):
                return True
        return False

    def _reject(
        self,
        *,
        name: str,
        url: str,
        error: str,
        message: str,
        extra: dict[str, Any] | None = None,
    ) -> ToolResult:
        self._audit(outcome="rejected", name=name, url=url, error=error, message=message)
        out: dict[str, Any] = {"error": error, "name": name, "url": url, "message": message}
        if extra:
            out.update(extra)
        return ToolResult(output=out, is_error=True)

    def _audit(
        self,
        *,
        outcome: str,
        name: str,
        url: str,
        transport: str | None = None,
        tool_count: int | None = None,
        error: str | None = None,
        message: str | None = None,
    ) -> None:
        if self._log_path is None:
            return
        record: dict[str, Any] = {
            "at_iso": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "outcome": outcome,
            "name": name,
            "url": url,
        }
        if transport is not None:
            record["transport"] = transport
        if tool_count is not None:
            record["tool_count"] = tool_count
        if error is not None:
            record["error"] = error
        if message is not None:
            record["message"] = message
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as e:
            _log.warning("mcp_connect: failed to append audit log: %s", e)


class McpDisconnectTool(_McpConnectorBase):
    name = "mcp_disconnect"
    description = (
        "Close a previously-connected outbound MCP and remove its tools "
        "from the registry. Idempotent: returns `removed=False` if no "
        "such connection exists."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The connection name passed to mcp_connect.",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        name = args["name"]
        client = _LIVE_CLIENTS.pop(name, None)

        tools_removed: list[str] = []
        prefix = f"{name}__"
        for tool_name in self._registry.names_with_prefix(prefix):
            if self._registry.unregister(tool_name):
                tools_removed.append(tool_name)

        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                _log.debug("disconnect %s noisy", name, exc_info=True)

        try:
            row_deleted = await self._store.delete_connected_mcp(name)
        except Exception:
            _log.exception("mcp_disconnect %s: failed to delete row", name)
            row_deleted = False

        return ToolResult(
            output={
                "status": "disconnected",
                "name": name,
                "removed": client is not None or row_deleted,
                "tools_removed": tools_removed,
                "tools_removed_count": len(tools_removed),
            }
        )


class McpListConnectedTool(_McpConnectorBase):
    name = "mcp_list_connected"
    description = (
        "List every outbound MCP currently connected to this agent: "
        "name, URL, status, tool count, and last error (if any). Use "
        "before mcp_connect to check whether a server is already mounted."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        entries: list[dict[str, Any]] = []
        for name in sorted(_LIVE_CLIENTS):
            client = _LIVE_CLIENTS[name]
            prefix = f"{name}__"
            tools = self._registry.names_with_prefix(prefix)
            entries.append(
                {
                    "name": name,
                    "url": client.url,
                    "status": client.status,
                    "last_error": client.last_error,
                    "tool_count": len(tools),
                    "tools": tools,
                }
            )
        return ToolResult(output={"connected": entries, "count": len(entries)})


# Suppress unused-import warning — ToolError is part of the public
# error surface used by callers indirectly via _register_prefixed.
_ = ToolError
