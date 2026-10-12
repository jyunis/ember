"""Memory MCP outbound tools.

Discovers tools on an `McpOutboundClient` connected to the memory MCP
server and registers each in the local `Registry` under the
`memory__<tool>` prefix.
"""

from __future__ import annotations

import logging
from typing import Any

from ember.tools.dynamic import make_dynamic_tool
from ember.tools.mcp_client import McpOutboundClient
from ember.tools.registry import Registry

_log = logging.getLogger("ember.tools.memory")


async def register_memory_tools(
    registry: Registry,
    client: McpOutboundClient,
) -> list[str]:
    return await _register_prefixed(registry, client)


async def _register_prefixed(
    registry: Registry,
    client: McpOutboundClient,
) -> list[str]:
    """Register every tool on `client` into `registry`, name-prefixed
    with `client.name`. Idempotent: tools already in the registry are
    skipped. Returns the full list of `<prefix>__*` names now in the
    registry — including ones registered on a prior call.
    """
    upstream = await client.list_tools()
    existing = set(registry.names())
    prefix = f"{client.name}__"
    newly: list[str] = []

    for spec in upstream:
        original_name = spec["name"]
        prefixed_name = f"{prefix}{original_name}"
        if prefixed_name in existing:
            continue

        async def invoker(args: dict[str, Any], _tn=original_name, _c=client) -> Any:
            return await _c.call_tool(_tn, args)

        tool = make_dynamic_tool(
            name=prefixed_name,
            description=spec.get("description") or f"{client.name} {original_name}",
            input_schema=spec.get("inputSchema") or {"type": "object"},
            invoker=invoker,
        )
        registry.register(tool)
        newly.append(prefixed_name)

    _log.info(
        "registered %d new %s tools (%d total)",
        len(newly),
        client.name,
        sum(1 for n in registry.names() if n.startswith(prefix)),
    )
    return [n for n in registry.names() if n.startswith(prefix)]
