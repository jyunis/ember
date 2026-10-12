"""`use_skill` host-level tool + sub-agent loop (FC-17, TLC-16, TLC-17, UoW-18).

The `use_skill` tool dispatches a named skill in an isolated sub-agent
loop. The sub-agent's tool registry is filtered to the skill's allowlist
(with `use_skill` itself excluded — TLC-16 §4 no nesting), the system
prompt is the skill's `system_prompt` only (TLC-16 §2), and the message
history is just the user query (TLC-16 §3).

Bounds (TLC-16 §5):
  - `max_turns`     — counts provider calls.
  - `max_tool_calls`— cumulative tool dispatches across all turns.
  - `timeout_s`     — wall-clock budget from start of the sub-agent loop.

On exhaustion the loop returns a status `exhausted_<which>` with the
last assistant text (or empty string) under `partial_text`. Provider
errors are caught and returned as `provider_error` with a typed
`error_type`. ToolErrors mid-loop are caught and fed back to the model
as a tool result so the loop can continue.

Cost attribution (TLC-16 §6): every `Store.append_message` performed
inside the sub-agent carries `subagent_skill=<name>` and
`parent_message_id=<parent's id>` so the parent conversation owns the
usage rows.

Status forwarding: tool dispatches inside the sub-agent emit a status
message on the parent's channel with a `[<skill>]` prefix.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ember.channels.base import Channel
from ember.errors import (
    AuthError,
    ProviderError,
    RateLimitError,
    ToolError,
    TransportError,
)
from ember.providers.base import (
    CompletionResult,
    Message,
    Provider,
    ToolCall,
    ToolSpec,
)
from ember.providers.routing import ProviderRouter
from ember.skills.base import Skill, SkillRegistry
from ember.tools.base import ToolResult
from ember.tools.registry import Registry

__all__ = ["UseSkillTool", "run_subagent"]

_log = logging.getLogger("ember.tools.use_skill")

_TOOL_STATUS_ARGS_LIMIT = 200


class UseSkillTool:
    """Tool that dispatches a named skill in an isolated sub-agent loop.

    Conversation-scoped: each instance carries the parent's
    `conversation_id` and `parent_message_id` so cost attribution can
    target the right rows. The agent loop instantiates a per-turn
    `UseSkillTool` and adds it to the parent's tool list when in router
    mode (TLC-17).
    """

    name: str = "use_skill"
    description: str = (
        "Dispatch a named skill to handle this request. The skill runs in an "
        "isolated sub-agent with only its own tools available. Use when the "
        "request matches a registered skill's domain."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "Name of the skill to dispatch.",
            },
            "query": {
                "type": "string",
                "description": "The query to pass to the sub-agent.",
            },
        },
        "required": ["skill", "query"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        *,
        skill_registry: SkillRegistry,
        tool_registry: Registry,
        provider_router: ProviderRouter,
        channel: Channel,
        store: Any,  # Store or FakeStore — duck-typed
        conversation_id: int,
        parent_message_id: int,
        parent_conversation_key: str | None = None,
        max_turns: int = 8,
        max_tool_calls: int = 16,
        timeout_s: float = 120.0,
    ) -> None:
        self._skill_registry = skill_registry
        self._tool_registry = tool_registry
        self._provider_router = provider_router
        self._channel = channel
        self._store = store
        self._conversation_id = conversation_id
        self._parent_message_id = parent_message_id
        self._parent_conversation_key = parent_conversation_key
        self._max_turns = max_turns
        self._max_tool_calls = max_tool_calls
        self._timeout_s = timeout_s

    async def call(self, args: dict[str, Any]) -> ToolResult:
        skill_name = args.get("skill")
        query = args.get("query")

        # Schema validation already happened at registry boundary, but
        # defensively re-check in case this tool is invoked directly.
        if not isinstance(skill_name, str) or not isinstance(query, str):
            return ToolResult(
                output={
                    "status": "unknown_skill",
                    "error_type": "unknown_skill",
                    "message": "skill and query must be strings",
                },
                is_error=True,
            )

        # Resolve the skill.
        try:
            skill = self._skill_registry.get(skill_name)
        except KeyError:
            return ToolResult(
                output={
                    "status": "unknown_skill",
                    "error_type": "unknown_skill",
                    "message": f"skill {skill_name!r} not in registry",
                },
                is_error=True,
            )

        # Verify all skill tools resolve in the global registry.
        wanted = tuple(getattr(skill, "tools", ()) or ())
        missing: list[str] = []
        for tool_name in wanted:
            try:
                self._tool_registry.get(tool_name)
            except KeyError:
                missing.append(tool_name)
        if missing:
            return ToolResult(
                output={
                    "status": "missing_tool",
                    "error_type": "missing_tool",
                    "message": (
                        f"skill {skill_name!r} references missing tools: {', '.join(missing)}"
                    ),
                },
                is_error=True,
            )

        # Resolve a provider for the sub-agent. The skill's
        # `preferred_model` (if any) is a route string; otherwise fall
        # back to `claude_cli`. If neither is registered, take any
        # available provider.
        provider = self._resolve_provider(skill)

        result = await run_subagent(
            skill=skill,
            query=query,
            provider=provider,
            tool_registry=self._tool_registry,
            channel=self._channel,
            store=self._store,
            conversation_id=self._conversation_id,
            parent_message_id=self._parent_message_id,
            parent_conversation_key=self._parent_conversation_key,
            max_turns=self._max_turns,
            max_tool_calls=self._max_tool_calls,
            timeout_s=self._timeout_s,
        )

        return ToolResult(output=result, is_error=result.get("status") != "ok")

    def _resolve_provider(self, skill: Skill) -> Provider:
        """Pick a provider for the sub-agent.

        Preference order:
          1. The skill's `preferred_model` if it parses as a known route.
          2. The `claude_cli` route if registered.
          3. Any registered provider (deterministic via sorted names).
        """
        preferred = getattr(skill, "preferred_model", None)
        if preferred:
            try:
                return self._provider_router.resolve(preferred).provider
            except Exception:
                _log.warning(
                    "skill %r preferred_model %r unresolvable; falling back",
                    skill.name,
                    preferred,
                )
        if self._provider_router.has("claude_cli"):
            return self._provider_router.resolve("claude_cli").provider
        names = self._provider_router.names()
        if not names:
            raise RuntimeError("no providers registered for sub-agent dispatch")
        return self._provider_router.resolve(names[0]).provider


def _build_subagent_registry(
    global_registry: Registry,
    allowlist: tuple[str, ...],
) -> Registry:
    """Build a fresh `Registry` for the sub-agent.

    TLC-16 §4: `use_skill` is never present in the sub-agent registry
    (no nesting). TLC-16 §1: only the skill's allowlisted tools are
    advertised.
    """
    out = Registry()
    wanted = set(allowlist)
    for tool in global_registry.list_tools():
        if tool.name == "use_skill":
            continue
        if tool.name not in wanted:
            continue
        out.register(tool)
    return out


def _tool_specs(registry: Registry) -> list[ToolSpec]:
    return [
        ToolSpec(
            name=tool.name,
            description=tool.description,
            input_schema=tool.input_schema,
        )
        for tool in registry.list_tools()
    ]


async def run_subagent(
    *,
    skill: Skill,
    query: str,
    provider: Provider,
    tool_registry: Registry,
    channel: Channel,
    store: Any,
    conversation_id: int,
    parent_message_id: int,
    parent_conversation_key: str | None = None,
    max_turns: int,
    max_tool_calls: int,
    timeout_s: float,
) -> dict[str, Any]:
    """Run a bounded sub-agent loop.

    `tool_registry` is the global registry; this function filters it to
    the skill's allowlist (with `use_skill` excluded — TLC-16 §4) before
    running the loop.

    Returns one of:
      - `{"status": "ok", "text": ..., "usage": ...}`
      - `{"status": "exhausted_<turns|tool_calls|timeout>", "partial_text": ..., "usage": ...}`
      - `{"status": "provider_error", "error_type": ..., "message": ...}`
    """
    deadline = time.monotonic() + timeout_s
    system = skill.system_prompt or ""
    history: list[Message] = [Message(role="user", content=query)]
    sub_registry = _build_subagent_registry(tool_registry, tuple(getattr(skill, "tools", ()) or ()))
    advertised_specs = _tool_specs(sub_registry)

    provider_calls = 0
    tool_calls_done = 0
    last_assistant_text = ""
    usage_summary: dict[str, Any] = {
        "provider_calls": 0,
        "tool_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }

    skill_prefix = f"[{skill.name}]"

    async def _stream_handler(event: dict[str, Any]) -> None:
        """Forward sub-agent tool_use events as parent-channel statuses
        with the skill prefix."""
        if event.get("type") != "assistant":
            return
        content = event.get("message", {}).get("content") or []
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            name = block.get("name") or "?"
            args = block.get("input") or {}
            try:
                args_str = json.dumps(args, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                args_str = str(args)
            if len(args_str) > _TOOL_STATUS_ARGS_LIMIT:
                args_str = args_str[: _TOOL_STATUS_ARGS_LIMIT - 3] + "..."
            await _safe_status(f"{skill_prefix} 🔧 {name}({args_str})")

    async def _persist_assistant(result: CompletionResult) -> None:
        try:
            await store.append_message(
                conversation_id,
                "assistant",
                result.text,
                provider_route=getattr(provider, "name", None),
                usage=result.usage,
                # Bridge-executed calls (UoW-4 step 5) are audit-only.
                tool_calls=[*result.executed_tool_calls, *result.tool_calls] or None,
                subagent_skill=skill.name,
                parent_message_id=parent_message_id,
            )
        except Exception:
            _log.exception("persist sub-agent assistant message failed")

    async def _persist_tool_result(call: ToolCall, result_text: str) -> None:
        try:
            await store.append_message(
                conversation_id,
                "tool",
                result_text,
                tool_calls=[call],
                subagent_skill=skill.name,
                parent_message_id=parent_message_id,
            )
        except Exception:
            _log.exception("persist sub-agent tool result failed")

    # Status messages must target the parent conversation's key so the
    # user actually sees them on the originating thread (FC-18 wiring B).
    # When the caller didn't pass one (older test fixtures), fall back to
    # a synthetic key — the channel is then expected to be a stub.
    status_target_key = parent_conversation_key or "__subagent__"

    async def _safe_status(content: str) -> None:
        try:
            await channel.send(status_target_key, content, kind="status")
        except Exception:
            _log.debug("sub-agent status emit failed", exc_info=True)

    while True:
        # Time bound check before each provider call.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "status": "exhausted_timeout",
                "partial_text": last_assistant_text,
                "usage": dict(usage_summary),
            }

        if provider_calls >= max_turns:
            return {
                "status": "exhausted_turns",
                "partial_text": last_assistant_text,
                "usage": dict(usage_summary),
            }

        try:
            result = await asyncio.wait_for(
                provider.complete(
                    system=system,
                    messages=list(history),
                    tools=advertised_specs,
                    stream_handler=_stream_handler,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            return {
                "status": "exhausted_timeout",
                "partial_text": last_assistant_text,
                "usage": dict(usage_summary),
            }
        except RateLimitError as e:
            return {
                "status": "provider_error",
                "error_type": "rate_limit",
                "message": str(e),
            }
        except AuthError as e:
            return {
                "status": "provider_error",
                "error_type": "auth",
                "message": str(e),
            }
        except TransportError as e:
            return {
                "status": "provider_error",
                "error_type": "transport",
                "message": str(e),
            }
        except ProviderError as e:
            return {
                "status": "provider_error",
                "error_type": "provider",
                "message": str(e),
            }

        provider_calls += 1
        usage_summary["provider_calls"] = provider_calls
        if result.usage is not None:
            usage_summary["input_tokens"] += int(result.usage.input_tokens or 0)
            usage_summary["output_tokens"] += int(result.usage.output_tokens or 0)

        if result.text:
            last_assistant_text = result.text

        await _persist_assistant(result)

        # Append the assistant message into the sub-agent's history so
        # subsequent turns see it.
        history.append(
            Message(
                role="assistant",
                content=result.text,
                tool_calls=result.tool_calls or None,
            )
        )

        if not result.tool_calls:
            return {
                "status": "ok",
                "text": result.text,
                "usage": dict(usage_summary),
            }

        # Dispatch each tool call.
        for call in result.tool_calls:
            if tool_calls_done >= max_tool_calls:
                return {
                    "status": "exhausted_tool_calls",
                    "partial_text": last_assistant_text,
                    "usage": dict(usage_summary),
                }

            # Status forwarding with skill prefix.
            await _safe_status(_format_tool_status(skill_prefix, call))

            try:
                tool_result = await sub_registry.call_tool(call.name, call.args)
                tool_text = _serialize_tool_output(tool_result.output)
                is_error = tool_result.is_error
            except ToolError as e:
                tool_text = json.dumps({"error": str(e)})
                is_error = True
            except KeyError as e:
                tool_text = json.dumps({"error": f"unknown tool: {e}", "tool": call.name})
                is_error = True
            except Exception as e:
                _log.exception("sub-agent tool dispatch raised")
                tool_text = json.dumps({"error": f"{type(e).__name__}: {e}", "tool": call.name})
                is_error = True

            tool_calls_done += 1
            usage_summary["tool_calls"] = tool_calls_done

            await _persist_tool_result(call, tool_text)

            # Feed result back as a tool message in history.
            history.append(
                Message(
                    role="tool",
                    content=tool_text,
                    tool_call_id=call.id,
                )
            )

            # If we just hit the cap, stop dispatching the remaining
            # calls and bail with the right status.
            if tool_calls_done >= max_tool_calls and call is not result.tool_calls[-1]:
                return {
                    "status": "exhausted_tool_calls",
                    "partial_text": last_assistant_text,
                    "usage": dict(usage_summary),
                }
            del is_error  # consumed by the json blob; kept here for future shaping


def _format_tool_status(prefix: str, call: ToolCall) -> str:
    try:
        args_str = json.dumps(call.args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        args_str = str(call.args)
    if len(args_str) > _TOOL_STATUS_ARGS_LIMIT:
        args_str = args_str[: _TOOL_STATUS_ARGS_LIMIT - 3] + "..."
    return f"{prefix} 🔧 {call.name}({args_str})"


def _serialize_tool_output(output: Any) -> str:
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(output)
