"""`read_payload` tool (v15, FC-30 / UoW-29) — retrieve stored oversize
tool outputs.

The bridge replaces any tool result bigger than the payload-store
threshold with an envelope carrying a `payload_ref` (TLC-26); this tool
is the retrieval half of that round-trip. Kept out of the router-mode
tools catalog (`DEFAULT_CATALOG_EXCLUDE`) — it is plumbing the envelope
note points at, not a user-facing action.
"""

from __future__ import annotations

from typing import Any

from ember.tools.base import ToolResult
from ember.tools.payload_store import PayloadStore


class ReadPayloadTool:
    name = "read_payload"
    requires_confirmation = False
    description = (
        "Read more of a stored tool output. When a tool result was too "
        "large it was replaced by an envelope containing a `payload_ref` "
        "and an excerpt; pass that ref here to read the full payload in "
        "slices. Returns {content, offset, total_bytes}; page by calling "
        "again with offset advanced past the bytes already read."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "The payload_ref from a stored-output envelope.",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Byte offset into the payload to start reading from (default 0).",
            },
            "max_bytes": {
                "type": "integer",
                "minimum": 1,
                "default": 16384,
                "description": "Maximum bytes to return in this slice (default 16384).",
            },
        },
        "required": ["ref"],
        "additionalProperties": False,
    }

    def __init__(self, *, payload_store: PayloadStore) -> None:
        self._payload_store = payload_store

    async def call(self, args: dict[str, Any]) -> ToolResult:
        # `get` never raises — an unknown ref comes back as an error dict
        # the model can read and recover from (TLC-26).
        result = self._payload_store.get(
            args["ref"],
            offset=int(args.get("offset", 0)),
            max_bytes=int(args.get("max_bytes", 16384)),
        )
        return ToolResult(output=result)
