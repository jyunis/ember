"""Tool registry.

Single instance per process (TLC-11). Both the MCP bridge and any
in-process tool loop reference the same registry. Validates args
against `input_schema` before dispatch (TLC-6).

v11 (TLC-3 / FC-3 / TLC-9): tools may declare `requires_confirmation=True`.
For those, `call_tool` refuses to invoke `tool.call()` until the caller
passes `allow_unconfirmed=True`. Otherwise it returns a marker
ToolResult `{"error": "confirmation_required", "action": <summary>}`
with `is_error=True`. The agent's `_StreamObserver` recognizes this
marker and persists `pending_tool_confirmation` on the conversation;
the next-turn pre-handler executes the call (or discards) directly.

A confirmation-gated tool may further define `needs_confirmation(args)`
to make the gate *conditional* on the call's arguments (see
`_tool_needs_confirmation`). `gmail_send` uses this to auto-send to
allowlisted recipients while prompting for anyone else.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from typing import Any

import jsonschema

from ember.errors import ToolValidationError
from ember.tools.base import Tool, ToolResult

CONFIRMATION_REQUIRED_ERROR = "confirmation_required"

# Above this many chars the generic 🔒 render switches from a one-line
# `tool(args)` to a multi-line `tool:` + pretty-printed JSON block. This
# is a layout threshold, NOT a truncation cap — the full args are always
# shown, because the user is approving exactly this call.
_ACTION_INLINE_LIMIT = 200

_log = logging.getLogger("ember.tools.registry")


class Registry:
    """In-memory tool registry."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        # Unlock windows (doc #60 / TLC-31): tools in `_locked_tools` skip
        # the confirmation gate while `_is_unlocked()` says the current
        # conversation holds a live window. Unset → every gated call
        # prompts (the pre-doc-#60 behaviour).
        self._locked_tools: frozenset[str] = frozenset()
        self._is_unlocked: Callable[[], bool] | None = None
        self._on_register: list[Callable[[Tool], None]] = []
        self._on_unregister: list[Callable[[str], None]] = []

    def add_listener(
        self,
        *,
        on_register: Callable[[Tool], None] | None = None,
        on_unregister: Callable[[str], None] | None = None,
    ) -> None:
        """Subscribe to membership changes. Listeners fire after the
        change is applied; a listener that raises is logged and skipped
        (a broken mirror must not corrupt the registry itself). The MCP
        bridge uses this to keep its served tool list in lockstep with
        runtime mounts/unmounts (mcp_connector)."""
        if on_register is not None:
            self._on_register.append(on_register)
        if on_unregister is not None:
            self._on_unregister.append(on_unregister)

    def configure_unlock(
        self, locked_tools: Iterable[str], is_unlocked: Callable[[], bool]
    ) -> None:
        """Wire the unlock gate. `is_unlocked` is consulted at dispatch time
        for the conversation currently driving the turn (the bridge's
        active handle); it must return False outside any turn."""
        self._locked_tools = frozenset(locked_tools)
        self._is_unlocked = is_unlocked

    def is_locked_tool(self, name: str) -> bool:
        return name in self._locked_tools

    def register(self, tool: Tool) -> None:
        """Register a tool. Raises ValueError on duplicate name (FC-3
        / FC-4 schema parity — one canonical registration)."""
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool
        for listener in self._on_register:
            try:
                listener(tool)
            except Exception:
                _log.exception("registry on_register listener failed for %s", tool.name)

    def unregister(self, name: str) -> bool:
        """Remove a tool by name. Returns True if removed, False if absent.
        Used by mcp_connector to drop tools when an outbound MCP is
        disconnected mid-run."""
        removed = self._tools.pop(name, None) is not None
        if removed:
            for listener in self._on_unregister:
                try:
                    listener(name)
                except Exception:
                    _log.exception("registry on_unregister listener failed for %s", name)
        return removed

    def get(self, name: str) -> Tool:
        """Return a registered tool by name. Raises KeyError if absent."""
        return self._tools[name]

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        """All registered tool names, sorted."""
        return sorted(self._tools.keys())

    def names_with_prefix(self, prefix: str) -> list[str]:
        """All registered tool names starting with `prefix`, sorted."""
        return sorted(n for n in self._tools if n.startswith(prefix))

    def list_tools(self) -> list[Tool]:
        """All registered tools (order-stable for byte-identical
        downstream serialization, TLC-12)."""
        return [self._tools[name] for name in self.names()]

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        *,
        allow_unconfirmed: bool = False,
    ) -> ToolResult:
        """Validate `args` against the tool's `input_schema` and dispatch.

        If the tool declares `requires_confirmation=True` and the caller
        did not pass `allow_unconfirmed=True`, returns a marker
        ToolResult instead of invoking the tool. The agent loop
        recognizes the marker, persists pending state, and re-invokes
        with `allow_unconfirmed=True` after the user confirms.
        """
        tool = self._tools[name]  # KeyError if unknown

        # Validate args first so legacy schema-validation contracts hold
        # for all tools, including confirmation-gated ones (TLC-6 stays
        # the registry's primary input-validity guarantee). The TLC-3
        # tests are permissive about the ordering (gate-first vs
        # validate-first); pre-v11 callers expect validate-first.
        try:
            jsonschema.validate(instance=args, schema=tool.input_schema)
        except jsonschema.ValidationError as e:
            raise ToolValidationError(
                f"args for tool {name!r} failed schema validation: {e.message}"
            ) from e

        if (
            getattr(tool, "requires_confirmation", False)
            and not allow_unconfirmed
            and self._tool_needs_confirmation(tool, args)
        ):
            locked = name in self._locked_tools
            if locked and self._is_unlocked is not None and self._is_unlocked():
                # Live unlock window for this conversation: the user
                # already presented the second factor for this class of
                # call. Run it without a prompt.
                return await tool.call(args)
            # Optional `preflight(args)` hook: a cheap, side-effect-free
            # check that the gated call *can* succeed (vault_get does the
            # redacted read). If it raises, the error goes back to the
            # model now — while it can still correct the call — instead
            # of after the user has typed a second-factor code for an
            # action that was always going to fail.
            preflight = getattr(tool, "preflight", None)
            if callable(preflight):
                await preflight(args)
            action = self._summarize_action(tool, args)
            return ToolResult(
                output={"error": CONFIRMATION_REQUIRED_ERROR, "action": action, "unlock": locked},
                is_error=True,
            )

        return await tool.call(args)

    @staticmethod
    def _tool_needs_confirmation(tool: Tool, args: dict[str, Any]) -> bool:
        """Decide whether *this specific call* must be confirmed.

        A `requires_confirmation=True` tool may additionally define a
        `needs_confirmation(args) -> bool` hook to make the gate
        conditional on the arguments (e.g. `gmail_send` auto-sends to
        allowlisted recipients but prompts for anyone else). Tools
        without the hook always gate — the historical all-or-nothing
        behavior. Fail closed: if the hook raises, we gate."""
        hook = getattr(tool, "needs_confirmation", None)
        if callable(hook):
            try:
                return bool(hook(args))
            except Exception:
                return True
        return True

    @staticmethod
    def _summarize_action(tool: Tool, args: dict[str, Any]) -> str:
        """Build the user-facing 🔒 confirm string. Tools may define a
        `summarize_action(args) -> str` for human-readable summaries
        (e.g. profile_update returning a unified diff instead of dumping
        the full new file). Falls back to a JSON-of-args render — the
        complete args, never truncated: short ones inline, longer ones as
        a pretty-printed block under a `tool:` header."""
        custom = getattr(tool, "summarize_action", None)
        if callable(custom):
            try:
                summary = custom(args)
                if isinstance(summary, str) and summary.strip():
                    return summary
            except Exception:
                # A tool's summarizer should never crash the gate path.
                # Fall through to the generic render.
                pass
        try:
            args_str = json.dumps(args, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return f"{tool.name}({args!s})"
        if len(args_str) <= _ACTION_INLINE_LIMIT:
            return f"{tool.name}({args_str})"
        pretty = json.dumps(args, ensure_ascii=False, sort_keys=True, indent=2)
        return f"{tool.name}:\n{pretty}"
