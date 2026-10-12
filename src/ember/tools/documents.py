"""Documents MCP outbound tools.

Mirrors `tools/memory.py` against the documents MCP server.
"""

from __future__ import annotations

from ember.tools.memory import _register_prefixed
from ember.tools.mcp_client import McpOutboundClient
from ember.tools.registry import Registry


async def register_documents_tools(
    registry: Registry,
    client: McpOutboundClient,
) -> list[str]:
    return await _register_prefixed(registry, client)
