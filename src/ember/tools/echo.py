"""Echo tool — the trivial tool used to validate the v0.5 MCP bridge.

Takes `{"text": "<some string>"}` and returns it unchanged.
"""

from __future__ import annotations

from typing import Any

from ember.tools.base import Tool, ToolResult


class EchoTool(Tool):
    name: str = "echo"
    description: str = "Echo back the provided text unchanged."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Text to echo back.",
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    }
    requires_confirmation: bool = False

    async def call(self, args: dict[str, Any]) -> ToolResult:
        return ToolResult(output={"text": args["text"]})
