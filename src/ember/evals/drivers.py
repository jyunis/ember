"""Capture drivers — run one turn and capture the tools the model reached for.

Two drivers behind one interface, both funnelling tool execution through a
`RecordingRegistry` so no real side effect can occur (FC-21):

- `ClaudeCliBridgeDriver` — the production-fidelity path. Stands up an
  eval-only MCP `Bridge` over the RecordingRegistry on a dedicated port
  (`eval_bridge_port`), separate from the live bridge so eval runs never
  disturb the running bot. Each turn spawns a fresh `claude -p` session
  (no `--resume`), so the model routes exactly as it does in production — but
  every tool call hits the recording registry, returning a stub/scripted
  result and never executing. This is why v11 routing measures the real model
  (doc #36 Decisions: recording-bridge fidelity capture).

- `InProcessDriver` — used for providers that return `tool_calls` to the caller
  (anthropic_api) and for tests (FakeProvider). Runs a bounded tool loop,
  dispatching every returned tool call through the same RecordingRegistry.

Both expose `.recording` (the shared registry) so the runner reads
`.recording.calls` after each turn as the audit log it asserts against.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ember.bridge.server import Bridge
from ember.evals.recording import RecordingRegistry
from ember.providers.base import Message, ToolSpec, Usage
from ember.providers.claude_cli import ClaudeCliProvider

if TYPE_CHECKING:
    from ember.config import Settings
    from ember.providers.base import Provider

_log = logging.getLogger("ember.evals.drivers")

_MAX_TOOL_ITERS = 8  # bound the in-process loop (parallels agent.py)


def sum_usage(a: Usage, b: Usage) -> Usage:
    """Accumulate provider usage across a multi-call turn (TLC-5)."""
    cost: float | None
    if a.cost_usd is None and b.cost_usd is None:
        cost = None
    else:
        cost = (a.cost_usd or 0.0) + (b.cost_usd or 0.0)
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cached_tokens=a.cached_tokens + b.cached_tokens,
        cost_usd=cost,
        subscription=a.subscription or b.subscription,
    )


@runtime_checkable
class CaptureDriver(Protocol):
    """Runs one turn against a shared `RecordingRegistry` and returns the
    final reply text + accumulated usage. `.recording.calls` holds the
    captured tool-call sequence."""

    recording: RecordingRegistry

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def run_turn(
        self, *, system: str, prompt: str, advertise_tools: bool
    ) -> tuple[str, Usage]: ...


class InProcessDriver:
    """Driver for tool-returning providers (anthropic_api) and tests."""

    def __init__(
        self,
        provider: "Provider",
        recording: RecordingRegistry,
        *,
        model: str | None = None,
    ) -> None:
        self._provider = provider
        self.recording = recording
        self._model = model

    async def start(self) -> None:  # no external resources
        return None

    async def stop(self) -> None:
        return None

    async def run_turn(
        self, *, system: str, prompt: str, advertise_tools: bool
    ) -> tuple[str, Usage]:
        tools: list[ToolSpec] | None = None
        if advertise_tools:
            tools = [
                ToolSpec(name=t.name, description=t.description, input_schema=t.input_schema)
                for t in self.recording.list_tools()
            ]
        history: list[Message] = [Message(role="user", content=prompt)]
        total = Usage(input_tokens=0, output_tokens=0)
        text = ""
        for _ in range(_MAX_TOOL_ITERS):
            result = await self._provider.complete(
                system=system,
                messages=history,
                tools=tools,
                model=self._model,
                session_id=None,
                stream_handler=None,
            )
            total = sum_usage(total, result.usage)
            if result.text:
                text = result.text
            if not result.tool_calls:
                break
            history.append(
                Message(
                    role="assistant",
                    content=result.text or "",
                    tool_calls=list(result.tool_calls),
                )
            )
            for call in result.tool_calls:
                tr = await self.recording.call_tool(call.name, call.args)
                history.append(
                    Message(
                        role="tool",
                        content=json.dumps(tr.output, default=str),
                        tool_call_id=call.id,
                    )
                )
        return text, total


class ClaudeCliBridgeDriver:
    """Production-fidelity driver: a fresh `claude -p` session per turn against
    an eval-only bridge serving the RecordingRegistry (zero real side effects).
    """

    def __init__(self, settings: "Settings", recording: RecordingRegistry) -> None:
        self._settings = settings
        self.recording = recording
        self._bridge: Bridge | None = None
        # Eval bridge config, distinct from the live bridge's config file.
        self._config_path = settings.data_dir / "agent-mcp-eval.json"

    async def start(self) -> None:
        """Bind the eval bridge over the RecordingRegistry on the dedicated
        eval port and write its mcp-config. Idempotent."""
        if self._bridge is not None:
            return
        bridge = Bridge(
            self.recording,  # served as the tool catalog; call_tool records + stubs
            host=self._settings.bridge_host,
            port=self._settings.eval_bridge_port,
        )
        await bridge.start()
        self._config_path.write_text(json.dumps(bridge.mcp_config_dict(), indent=2))
        self._bridge = bridge
        _log.info("eval bridge up at %s", bridge.url)

    async def stop(self) -> None:
        if self._bridge is not None:
            await self._bridge.stop()
            self._bridge = None
        try:
            self._config_path.unlink(missing_ok=True)
        except OSError:
            pass

    async def run_turn(
        self, *, system: str, prompt: str, advertise_tools: bool
    ) -> tuple[str, Usage]:
        # A fresh provider per turn → fresh session (session_id=None), no
        # --resume, so nothing touches the live conversation's session jsonl.
        # `session_jsonl_dir=None` disables the pruner (there's no session to
        # prune). Tools attach via the eval bridge only when advertised.
        provider = ClaudeCliProvider(
            binary=self._settings.claude_binary_path,
            default_model=self._settings.claude_default_model,
            mcp_config_paths=[str(self._config_path)] if advertise_tools else [],
            allowed_tools="mcp__agent__*" if advertise_tools else "",
            session_jsonl_dir=None,
        )
        result = await provider.complete(
            system=system,
            messages=[Message(role="user", content=prompt)],
            tools=None,
            session_id=None,
            stream_handler=None,
        )
        return result.text, result.usage


def build_driver(
    *,
    settings: "Settings",
    recording: RecordingRegistry,
    provider: "Provider",
    model: str | None,
) -> CaptureDriver:
    """Pick the driver for the eval route. claude_cli returns no tool_calls to
    the caller (it executes in-session via the bridge), so it needs the eval
    bridge driver; everything else uses the in-process tool loop."""
    if provider.name == "claude_cli":
        return ClaudeCliBridgeDriver(settings, recording)
    return InProcessDriver(provider, recording, model=model)
