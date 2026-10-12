"""Registry confirmation enforcement (TLC-3, FC-3, TLC-9).

Until v11, `requires_confirmation=True` was declarative metadata only —
the registry would still invoke `tool.call()` regardless. v11 wires real
enforcement: when the LLM calls a confirmation-required tool, the
registry refuses to execute and returns a marker `ToolResult` with
`output={"error": "confirmation_required", "action": <summary>}` and
`is_error=True`. The agent loop persists the pending state, then the
user replies y/n on the next turn and the agent directly executes (or
discards) the pending call via `call_tool(..., allow_unconfirmed=True)`.

These tests exercise the registry contract only — the agent-side
persistence + pre-turn flow lives in `test_agent_tlc3.py`.

Tester barrier: we have not read the new `registry.py` (with the
`allow_unconfirmed` kwarg) — we only read the v=4 interface. ImportError
or TypeError on first run is expected for the new-kwarg paths.
"""

from __future__ import annotations

from typing import Any

from ember.tools.base import ToolResult
from ember.tools.registry import Registry


# --- Fakes ----------------------------------------------------------------


class _SafeTool:
    """A confirmation-not-required tool. Records every call."""

    name: str = "safe_tool"
    description: str = "Does nothing scary."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": ["x"],
        "additionalProperties": False,
    }
    requires_confirmation: bool = False

    def __init__(self, name: str = "safe_tool") -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []

    async def call(self, args: dict[str, Any]) -> ToolResult:
        self.calls.append(args)
        return ToolResult(output={"echo": args.get("x")}, is_error=False)


class _DangerTool:
    """A confirmation-required tool. Records every call so the test can
    verify that `call()` was NOT invoked when the gate fires.
    """

    name: str = "danger_tool"
    description: str = "Reformats your hard drive."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "target": {"type": "string"},
        },
        "required": ["target"],
        "additionalProperties": False,
    }
    requires_confirmation: bool = True

    def __init__(self, name: str = "danger_tool") -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []

    async def call(self, args: dict[str, Any]) -> ToolResult:
        self.calls.append(args)
        return ToolResult(output={"deleted": args.get("target")}, is_error=False)


# --- Existing-tool behavior unchanged -------------------------------------


class TestUnchangedForSafeTool:
    """Tools with `requires_confirmation=False` (every existing tool)
    must continue to execute normally — with or without
    `allow_unconfirmed`. The new gate only fires when the tool itself
    declares confirmation is required."""

    async def test_safe_tool_executes_default(self) -> None:
        reg = Registry()
        t = _SafeTool()
        reg.register(t)
        result = await reg.call_tool("safe_tool", {"x": "hi"})
        assert isinstance(result, ToolResult)
        assert result.is_error is False
        assert result.output == {"echo": "hi"}
        assert t.calls == [{"x": "hi"}]

    async def test_safe_tool_executes_with_allow_unconfirmed_true(self) -> None:
        # `allow_unconfirmed=True` is a no-op for safe tools — they
        # have no gate to bypass. The result must be identical.
        reg = Registry()
        t = _SafeTool()
        reg.register(t)
        result = await reg.call_tool("safe_tool", {"x": "hi"}, allow_unconfirmed=True)
        assert result.is_error is False
        assert result.output == {"echo": "hi"}
        assert t.calls == [{"x": "hi"}]

    async def test_safe_tool_executes_with_allow_unconfirmed_false(self) -> None:
        reg = Registry()
        t = _SafeTool()
        reg.register(t)
        result = await reg.call_tool("safe_tool", {"x": "hi"}, allow_unconfirmed=False)
        assert result.is_error is False
        assert result.output == {"echo": "hi"}
        assert t.calls == [{"x": "hi"}]


# --- New gate behavior on confirmation-required tools ---------------------


class TestConfirmationRequiredGate:
    async def test_default_blocks_execution_returns_marker(self) -> None:
        # Default `allow_unconfirmed=False` (kwarg omitted): the gate
        # fires, the tool's `call()` is NOT invoked, and the registry
        # returns a marker ToolResult.
        reg = Registry()
        t = _DangerTool()
        reg.register(t)
        result = await reg.call_tool("danger_tool", {"target": "/etc"})
        assert isinstance(result, ToolResult)
        assert result.is_error is True
        assert isinstance(result.output, dict)
        assert result.output.get("error") == "confirmation_required"
        # The tool body was NOT run.
        assert t.calls == [], (
            "registry must not invoke tool.call() when the confirmation gate fires"
        )

    async def test_explicit_allow_false_blocks_execution(self) -> None:
        # Explicitly passing `allow_unconfirmed=False` is equivalent to
        # the default — gate fires, no execution.
        reg = Registry()
        t = _DangerTool()
        reg.register(t)
        result = await reg.call_tool("danger_tool", {"target": "/etc"}, allow_unconfirmed=False)
        assert result.is_error is True
        assert result.output.get("error") == "confirmation_required"
        assert t.calls == []

    async def test_allow_unconfirmed_true_executes_normally(self) -> None:
        # The pre-turn yes-handler path: `allow_unconfirmed=True`
        # bypasses the gate and runs the tool normally.
        reg = Registry()
        t = _DangerTool()
        reg.register(t)
        result = await reg.call_tool("danger_tool", {"target": "/etc"}, allow_unconfirmed=True)
        assert result.is_error is False
        assert result.output == {"deleted": "/etc"}
        assert t.calls == [{"target": "/etc"}]


# --- Marker shape ---------------------------------------------------------


class TestMarkerShape:
    """The marker payload's exact shape matters because the agent's
    `_StreamObserver` parses tool_result content as JSON and matches on
    `output["error"] == "confirmation_required"`. If the implementer
    drifts here, the observer can't recognize the marker."""

    async def test_marker_has_required_keys(self) -> None:
        reg = Registry()
        reg.register(_DangerTool())
        result = await reg.call_tool("danger_tool", {"target": "/etc"})
        assert isinstance(result.output, dict), (
            f"marker output must be a dict; got {type(result.output)}"
        )
        assert "error" in result.output
        assert "action" in result.output

    async def test_marker_error_is_confirmation_required(self) -> None:
        reg = Registry()
        reg.register(_DangerTool())
        result = await reg.call_tool("danger_tool", {"target": "/etc"})
        assert result.output["error"] == "confirmation_required"

    async def test_marker_is_error_true(self) -> None:
        reg = Registry()
        reg.register(_DangerTool())
        result = await reg.call_tool("danger_tool", {"target": "/etc"})
        assert result.is_error is True

    async def test_marker_action_is_non_empty_string(self) -> None:
        reg = Registry()
        reg.register(_DangerTool())
        result = await reg.call_tool("danger_tool", {"target": "/etc"})
        action = result.output["action"]
        assert isinstance(action, str), f"action must be a string; got {type(action)}"
        assert action.strip() != "", "action must be non-empty"

    async def test_marker_action_includes_tool_name(self) -> None:
        # Spec v0: action is `f"{name}({args_summary})"`. We assert the
        # tool name appears as a substring — keeps the test resilient to
        # the implementer's choice of args formatting.
        reg = Registry()
        reg.register(_DangerTool())
        result = await reg.call_tool("danger_tool", {"target": "/etc"})
        action = result.output["action"]
        assert "danger_tool" in action, (
            f"action string should mention the tool name; got {action!r}"
        )

    async def test_marker_action_never_truncates_long_args(self) -> None:
        # The generic render used to cut args at 200 chars with "...".
        # The user is approving exactly this call, so every arg value
        # must be present in full — long ones as a pretty-printed block.
        reg = Registry()
        reg.register(_DangerTool())
        target = "/very/long/" + "/".join(f"segment-{i}" for i in range(60))
        result = await reg.call_tool("danger_tool", {"target": target})
        action = result.output["action"]
        assert action.startswith("danger_tool:\n")
        assert target in action
        assert "..." not in action

    async def test_marker_action_short_args_stay_inline(self) -> None:
        reg = Registry()
        reg.register(_DangerTool())
        result = await reg.call_tool("danger_tool", {"target": "/etc"})
        assert result.output["action"] == 'danger_tool({"target": "/etc"})'


# --- Conditional gate via needs_confirmation(args) ------------------------


class _ConditionalTool:
    """A confirmation-required tool that gates only for some args — the
    `gmail_send` pattern. Gates unless `args["safe"]` is truthy."""

    name: str = "conditional_tool"
    description: str = "Gated only for unsafe args."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"safe": {"type": "boolean"}},
        "additionalProperties": True,
    }
    requires_confirmation: bool = True

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def needs_confirmation(self, args: dict[str, Any]) -> bool:
        return not bool(args.get("safe"))

    async def call(self, args: dict[str, Any]) -> ToolResult:
        self.calls.append(args)
        return ToolResult(output={"ran": True}, is_error=False)


class _RaisingHookTool(_ConditionalTool):
    """`needs_confirmation` raises — the registry must fail closed (gate)."""

    name: str = "raising_hook_tool"

    def needs_confirmation(self, args: dict[str, Any]) -> bool:
        raise RuntimeError("hook blew up")


class TestConditionalConfirmationGate:
    async def test_hook_false_bypasses_gate_and_runs(self) -> None:
        # needs_confirmation -> False: the auto-run fast path. The tool
        # executes on the first call with no marker.
        reg = Registry()
        t = _ConditionalTool()
        reg.register(t)
        result = await reg.call_tool("conditional_tool", {"safe": True})
        assert result.is_error is False
        assert result.output == {"ran": True}
        assert t.calls == [{"safe": True}]

    async def test_hook_true_gates_and_returns_marker(self) -> None:
        reg = Registry()
        t = _ConditionalTool()
        reg.register(t)
        result = await reg.call_tool("conditional_tool", {"safe": False})
        assert result.is_error is True
        assert result.output.get("error") == "confirmation_required"
        assert t.calls == []

    async def test_hook_true_then_confirmed_runs(self) -> None:
        # After the user confirms, the re-invoke passes
        # allow_unconfirmed=True and the hook is not consulted.
        reg = Registry()
        t = _ConditionalTool()
        reg.register(t)
        gated = await reg.call_tool("conditional_tool", {"safe": False})
        assert gated.output.get("error") == "confirmation_required"
        confirmed = await reg.call_tool("conditional_tool", {"safe": False}, allow_unconfirmed=True)
        assert confirmed.is_error is False
        assert t.calls == [{"safe": False}]

    async def test_raising_hook_fails_closed(self) -> None:
        # If needs_confirmation raises, the registry must gate (never
        # silently auto-run a confirmation-required tool).
        reg = Registry()
        t = _RaisingHookTool()
        reg.register(t)
        result = await reg.call_tool("raising_hook_tool", {"safe": True})
        assert result.is_error is True
        assert result.output.get("error") == "confirmation_required"
        assert t.calls == []


# --- Independence from schema validation ----------------------------------


class TestGateAndSchemaValidation:
    """The confirmation gate is orthogonal to TLC-6 schema validation.
    Bad args still fail at the schema layer (caller never gets a marker
    for invalid args). We don't pin which one runs first — just that
    invalid args still fail and valid args yield a marker."""

    async def test_invalid_args_still_fail(self) -> None:
        # Missing required `target` — must NOT silently return a marker
        # (the args were never valid). TLC-6 still applies.
        from ember.errors import ToolValidationError

        reg = Registry()
        t = _DangerTool()
        reg.register(t)
        # Either: schema validation raises ToolValidationError, OR the
        # marker is returned (some implementers might gate first then
        # validate on the y-confirm path). We assert tool was not
        # invoked either way.
        try:
            result = await reg.call_tool("danger_tool", {})
        except ToolValidationError:
            pass
        else:
            # Implementer may choose to gate first and validate later.
            # In that case the marker is OK — but the tool body must
            # not have run.
            assert result.is_error is True
        assert t.calls == []


# --- Preflight hook (2026-09-27 vault UX) --------------------------------


class _PreflightTool(_DangerTool):
    """Gated tool whose `preflight` can be told to fail."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        super().__init__("preflight_tool")
        self.fail_with = fail_with
        self.preflights: list[dict[str, Any]] = []

    async def preflight(self, args: dict[str, Any]) -> None:
        self.preflights.append(args)
        if self.fail_with is not None:
            raise self.fail_with


class TestPreflight:
    async def test_failing_preflight_raises_instead_of_marker(self) -> None:
        from ember.errors import ToolError

        tool = _PreflightTool(fail_with=ToolError("vault: not found: X"))
        reg = Registry()
        reg.register(tool)
        import pytest

        with pytest.raises(ToolError, match="not found"):
            await reg.call_tool("preflight_tool", {"target": "x"})
        assert tool.calls == [], "tool.call must not run when preflight fails"
        assert tool.preflights == [{"target": "x"}]

    async def test_passing_preflight_still_returns_marker(self) -> None:
        tool = _PreflightTool()
        reg = Registry()
        reg.register(tool)
        result = await reg.call_tool("preflight_tool", {"target": "x"})
        assert result.is_error and result.output["error"] == "confirmation_required"
        assert tool.calls == []

    async def test_preflight_skipped_when_allow_unconfirmed(self) -> None:
        tool = _PreflightTool(fail_with=RuntimeError("must not run"))
        reg = Registry()
        reg.register(tool)
        await reg.call_tool("preflight_tool", {"target": "x"}, allow_unconfirmed=True)
        assert tool.preflights == []
        assert tool.calls == [{"target": "x"}]


# --- Unlock windows (doc #60 TLC-31) ------------------------------------


class TestUnlockGate:
    async def test_locked_tool_runs_inside_window_without_marker(self) -> None:
        tool = _PreflightTool(fail_with=RuntimeError("preflight must not run"))
        reg = Registry()
        reg.register(tool)
        reg.configure_unlock({"preflight_tool"}, lambda: True)
        result = await reg.call_tool("preflight_tool", {"target": "x"})
        assert not result.is_error
        assert tool.calls == [{"target": "x"}]
        assert tool.preflights == []

    async def test_locked_tool_marker_flags_unlock_when_locked(self) -> None:
        tool = _PreflightTool()
        reg = Registry()
        reg.register(tool)
        reg.configure_unlock({"preflight_tool"}, lambda: False)
        result = await reg.call_tool("preflight_tool", {"target": "x"})
        assert result.is_error and result.output["error"] == "confirmation_required"
        assert result.output["unlock"] is True
        assert tool.calls == []
        assert tool.preflights == [{"target": "x"}], "preflight still guards the prompt"

    async def test_non_locked_tool_ignores_window(self) -> None:
        tool = _DangerTool()
        reg = Registry()
        reg.register(tool)
        reg.configure_unlock({"something_else"}, lambda: True)
        result = await reg.call_tool("danger_tool", {"target": "x"})
        assert result.is_error and result.output["unlock"] is False
        assert tool.calls == []
