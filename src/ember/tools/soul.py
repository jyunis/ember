"""Soul read tool.

Reads soul.md verbatim. The contents are already injected into the
system block every turn (per architecture § Prompt assembly order), but
the LLM can't distinguish "system prompt" from "soul.md file contents"
without an explicit read path. This tool makes that path legible.

Per FC-8, soul.md is **read-only to the agent**. Manual edits only,
via sudo on the host. The earlier `soul_update` tool was tried and
reverted: claude consistently asks-permission-first instead of calling
the gated tool, which breaks the TLC-3 gate contract. The friction of
sudo-editing soul.md once a month is worth the explicit human-in-the-
loop boundary on agent self-modification.
"""

from __future__ import annotations

import logging
from typing import Any

from ember.identity.store import IdentityStore
from ember.tools.base import ToolResult

_log = logging.getLogger("ember.tools.soul")


class SoulReadTool:
    """Read soul.md verbatim. No confirmation needed; read-only."""

    name: str = "soul_read"
    description: str = "Return the full current contents of the agent's soul.md."
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, *, identity: IdentityStore) -> None:
        self._identity = identity

    async def call(self, args: dict[str, Any]) -> ToolResult:
        try:
            content = self._identity.read_soul()
        except FileNotFoundError:
            return ToolResult(output={"content": "", "error": "soul.md not found"}, is_error=True)
        return ToolResult(output={"content": content})
