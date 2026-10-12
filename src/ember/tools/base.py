"""Tool interface (FC-3).

Every tool in the registry implements `Tool`. The registry validates
args against `input_schema` before calling `Tool.call` (TLC-6); tools
may assume validity.

Errors:
- `ToolError`         — fail without retry; agent loop returns the
                        message to the model as a tool result.
- `ToolRetryable`     — transient; agent loop may retry per its policy.
- `ToolValidationError` (raised by the registry, not the tool) — args
                        failed schema validation; tool not invoked.

Tools are stateless w.r.t. conversation context. Anything they need
arrives via `args`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Result returned by a tool call.

    `output` is the structured result the model sees. The bridge
    converts this into an MCP `CallToolResult` at its boundary.
    """

    output: Any
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Tool(Protocol):
    """The single internal interface every tool implements."""

    name: str
    description: str
    input_schema: dict[str, Any]
    requires_confirmation: bool

    async def call(self, args: dict[str, Any]) -> ToolResult:
        """Run the tool. `args` has already been schema-validated by
        the registry (TLC-6).

        Raises:
            ToolError: terminal failure; agent loop surfaces to model.
            ToolRetryable: transient; agent loop decides retry policy.
        """
        ...
