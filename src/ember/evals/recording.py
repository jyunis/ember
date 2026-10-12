"""Recording tool registry (FC-21 core — no real tool side effects).

Wraps a real `Registry` and exposes the SAME tool specs (so the model sees an
identical catalog and routes faithfully), but intercepts `call_tool`: it
records every call and returns either the case's scripted output for that tool
or a generic inert stub. The wrapped tool's `call()` is NEVER invoked — no real
side effect can occur, whether the run drives a live `claude -p` subprocess via
an eval-only bridge or an in-process fake provider. Confirmation gating is
bypassed (evals never prompt a human).

Implements the `ToolCallRecorder` Protocol (`evals/base.py`). The eval bridge
and the in-process runner both dispatch tool calls through this, so `.calls` is
the single audit log the runner asserts against (routing = first call;
tool_flow = the full sequence).
"""

from __future__ import annotations

from typing import Any

from ember.evals.base import RecordedToolCall
from ember.tools.base import ToolResult
from ember.tools.registry import Registry


class RecordingRegistry:
    """Fake-executing mirror of a real `Registry`. See module docstring."""

    def __init__(self, real_registry: Registry, scripted: dict[str, Any] | None = None) -> None:
        self._real = real_registry
        self._scripted = dict(scripted or {})
        self.calls: list[RecordedToolCall] = []

    # --- Per-case reuse (one instance served by one long-lived bridge) ----

    def reconfigure(self, scripted: dict[str, Any] | None) -> None:
        """Swap the scripted-response table for the next case. Lets a single
        RecordingRegistry (and the one eval bridge serving it) be reused
        across cases with different fake tools."""
        self._scripted = dict(scripted or {})

    def reset(self) -> None:
        """Clear the recorded-call log before a fresh trial."""
        self.calls.clear()

    # --- Catalog surface (delegates to the real registry) -----------------
    # The bridge reads `list_tools()` to advertise the catalog and builds an
    # invoker that calls `call_tool`; other callers may probe membership.

    def list_tools(self) -> list[Any]:
        return self._real.list_tools()

    def get(self, name: str) -> Any:
        return self._real.get(name)

    def has(self, name: str) -> bool:
        return self._real.has(name)

    def names(self) -> list[str]:
        return self._real.names()

    def names_with_prefix(self, prefix: str) -> list[str]:
        return self._real.names_with_prefix(prefix)

    # --- Intercepted dispatch (the FC-21 guarantee) -----------------------

    async def call_tool(
        self, name: str, args: dict[str, Any], *, allow_unconfirmed: bool = False
    ) -> ToolResult:
        """Record the call and return a scripted-or-stub result WITHOUT ever
        invoking the wrapped real tool. `allow_unconfirmed` is accepted for
        surface-compatibility with `Registry.call_tool` and ignored — evals
        neither gate nor execute."""
        self.calls.append(RecordedToolCall(name=name, args=dict(args)))
        if name in self._scripted:
            return ToolResult(output=self._scripted[name])
        # Inert stub — enough for the model to keep going, no side effect.
        return ToolResult(output={"_eval_stub": True, "tool": name})
