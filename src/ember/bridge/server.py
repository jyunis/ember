"""MCP bridge.

Long-lived FastMCP-backed streamable-HTTP server that exposes the
agent's tool registry to the `claude` CLI. One bridge per agent
process, started on boot, stopped on shutdown (FC-4).

The MCP server's `name` is "agent" — claude sees our tools as
`mcp__agent__<tool-name>`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from ember.tools.base import Tool
from ember.tools.dynamic import make_bridge_wrapper
from ember.tools.payload_store import PayloadStore
from ember.tools.registry import CONFIRMATION_REQUIRED_ERROR, Registry

_log = logging.getLogger("ember.bridge")


# Per-result hard cap (FC-4 / UoW-4 step 4). Pathological tool returns
# (multi-MB HTML, oversized rss payload) are truncated at the boundary
# before they hit the session jsonl so the retroactive pruner never has
# to deal with a 5MB blob. 60KB is the default (TLC-18). v15: this is
# now the BACKSTOP behind the payload store (TLC-26) — with a store
# wired, anything over its (smaller) threshold becomes an envelope well
# before this cap could fire.
_PER_RESULT_HARD_CAP_BYTES = 60 * 1024


def _serialize_output(output: Any) -> str:
    """The canonical serialized form of a tool output — shared by the
    payload-store threshold check (TLC-26) and the hard cap so both
    measure the same bytes."""
    try:
        return json.dumps(output, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(output)


def _cap_serialized_output(output: Any) -> Any:
    """If the JSON-serialized form of `output` exceeds the hard cap,
    return a truncated string with a marker line appended. Otherwise
    return `output` unchanged.

    The marker line is byte-stable (no clock interpolation) so it
    survives prompt-cache equality checks.
    """
    serialized = _serialize_output(output)
    if len(serialized.encode("utf-8")) <= _PER_RESULT_HARD_CAP_BYTES:
        return output
    # Truncate at byte boundary, then append the marker. Round the
    # original size to the nearest KB (integer) — deterministic.
    original_kb = len(serialized.encode("utf-8")) // 1024
    encoded = serialized.encode("utf-8")[:_PER_RESULT_HARD_CAP_BYTES]
    truncated = encoded.decode("utf-8", errors="ignore")
    return f"{truncated}\n\n[tool output capped: original was {original_kb}KB]"


class Bridge:
    """MCP-over-streamable-HTTP bridge. Stateless w.r.t. conversations."""

    def __init__(
        self,
        registry: Registry,
        *,
        host: str = "127.0.0.1",
        port: int = 9931,
        payload_store: PayloadStore | None = None,
    ) -> None:
        self._registry = registry
        self._host = host
        self._port = port
        # v15 (FC-30 / TLC-26): oversize normal-path tool results become
        # payload-store envelopes. None disables substitution (tests /
        # minimal wiring) — the 60KB hard cap still applies.
        self._payload_store = payload_store
        self._mcp = FastMCP(
            name="agent",
            host=host,
            port=port,
            # Stateful streamable-HTTP sessions. With stateless_http=True the
            # server issues no Mcp-Session-Id, and the claude CLI's MCP client
            # (>=2.1.18x) intermittently fails to register our tools even after
            # a served tools/list — the model then hits "No such tool available:
            # mcp__agent__*" and the whole turn loses its agent tools. A
            # maintained session makes attachment reliable. (This is HTTP
            # session state, independent of conversation state, which Ember
            # tracks separately.)
            stateless_http=False,
            log_level="WARNING",
        )
        self._task: asyncio.Task[None] | None = None
        self._register_tools()
        # Live-sync: runtime registry changes (mcp_connect / mcp_disconnect)
        # must reach the served tool list, or tools mounted after boot are
        # invisible to claude ("No such tool available") until the next
        # container restart. Each turn spawns a fresh `claude --print`
        # process that re-fetches tools/list, so mirroring here makes new
        # mounts callable from the next turn onward.
        registry.add_listener(
            on_register=self._on_registry_register,
            on_unregister=self._on_registry_unregister,
        )

    # --- Public surface ---------------------------------------------------

    async def start(self) -> None:
        """Bind and begin serving. Returns once the server is up (or
        raises if bind fails)."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._mcp.run_streamable_http_async(), name="ember-bridge")
        # Give uvicorn a moment to bind; surface bind errors immediately.
        await asyncio.sleep(0.1)
        if self._task.done():
            exc = self._task.exception()
            if exc is not None:
                raise exc
        _log.info("MCP bridge listening at %s", self.url)

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

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}/mcp"

    def mcp_config_dict(self) -> dict[str, Any]:
        return {
            "mcpServers": {
                "agent": {
                    "type": "http",
                    "url": self.url,
                }
            }
        }

    # --- Internal: registry live-sync -------------------------------------

    def _on_registry_register(self, tool: Tool) -> None:
        self._add_one(tool)
        self._notify_tools_changed()

    def _on_registry_unregister(self, name: str) -> None:
        try:
            self._mcp._tool_manager.remove_tool(name)
        except Exception:
            # Absent from the served set (never mirrored) — nothing to drop.
            _log.debug("bridge: remove_tool(%s) skipped", name, exc_info=True)
            return
        self._notify_tools_changed()

    def _notify_tools_changed(self) -> None:
        """Best-effort `notifications/tools/list_changed` to the session
        whose request triggered the change. Registry mutations arrive
        synchronously inside a bridge tool call (mcp_connect runs through
        our invoker), so the low-level request context is set. If the CLI
        honors the notification the new tools are callable in the same
        turn; if not, the next turn's fresh process picks them up anyway."""
        try:
            session = self._mcp._mcp_server.request_context.session
        except LookupError:
            return  # mutation from outside a bridge request (e.g. boot)
        try:
            asyncio.get_running_loop().create_task(session.send_tool_list_changed())
        except Exception:
            _log.debug("bridge: tools/list_changed notify failed", exc_info=True)

    # --- Internal: tool registration -------------------------------------

    def _register_tools(self) -> None:
        for tool in self._registry.list_tools():
            self._add_one(tool)

    def _add_one(self, tool: Tool) -> None:
        """Wrap a registry Tool as a FastMCP tool.

        Synthesizes a Python wrapper whose signature matches the tool's
        `input_schema`, then registers it. FastMCP derives its public
        schema from the synthesized signature — schema parity with our
        registry (FC-4).
        """
        registry = self._registry
        tool_name = tool.name
        payload_store = self._payload_store

        async def invoker(args: dict[str, Any]) -> Any:
            result = await registry.call_tool(tool_name, args)
            if result.is_error:
                # The confirmation_required marker is a structured signal,
                # not a real error. Raising would force FastMCP to serialize
                # it as `repr(dict)` text, which the agent observer can't
                # parse as JSON — so the 🔒 confirm prompt never emits and
                # pending state is never persisted. Pass it through, AND
                # reshape so the model doesn't read the literal "error"
                # field and narrate failure ("Same result..."). The agent
                # observer matches both `error == "confirmation_required"`
                # (legacy / in-process tests) and the new
                # `status == "awaiting_user_confirmation"` shape.
                if (
                    isinstance(result.output, dict)
                    and result.output.get("error") == CONFIRMATION_REQUIRED_ERROR
                ):
                    unlock = bool(result.output.get("unlock"))
                    reshaped = {
                        "status": "awaiting_user_confirmation",
                        "action": result.output.get("action", ""),
                        "unlock": unlock,
                        "message": (
                            "The host is asking the user (out-of-band) for a "
                            "second-factor code to unlock this tool in this "
                            "conversation. Do NOT retry this tool now. Do NOT "
                            "narrate a failure or apologize. Say in one line "
                            "that you need them to unlock it, then stop; you "
                            "will be prompted to continue once they have."
                            if unlock
                            else "The user is being shown a confirmation prompt "
                            "out-of-band by the host. Do NOT retry this tool. "
                            "Do NOT narrate a failure or apologize. Briefly "
                            "acknowledge that the change is queued pending "
                            "the user's approval, then stop."
                        ),
                    }
                    return _cap_serialized_output(reshaped)
                raise RuntimeError(str(result.output))
            attachments = list(result.metadata.get("attachments") or [])
            if attachments:
                # Best-effort post into the active conversation; failures
                # are logged inside forward_attachments and never block
                # the tool result reaching claude.
                from ember.bridge.active import forward_attachments

                await forward_attachments(attachments)
            # UoW-29: oversize normal-path results become payload-store
            # envelopes — the full payload is retrievable via
            # `read_payload`, and only the small deterministic envelope
            # enters the session jsonl. The confirmation-reshape path
            # above is exempt (always small). The hard cap stays as the
            # backstop in both branches (TLC-26).
            if payload_store is not None:
                serialized = _serialize_output(result.output)
                if payload_store.should_store(serialized):
                    return _cap_serialized_output(payload_store.put(tool_name, serialized))
            return _cap_serialized_output(result.output)

        wrapper = make_bridge_wrapper(
            name=tool_name,
            description=tool.description,
            input_schema=tool.input_schema,
            invoker=invoker,
        )
        # FastMCP.add_tool returns None; reach into the tool_manager so
        # we can override the served schema for FC-4 parity.
        fastmcp_tool = self._mcp._tool_manager.add_tool(
            wrapper,
            name=tool_name,
            description=tool.description,
        )
        # FC-4 schema parity: FastMCP rebuilds the schema from the
        # wrapper's signature (loses descriptions, adds title/etc).
        # Replace the served schema with our canonical one.
        fastmcp_tool.parameters = tool.input_schema
