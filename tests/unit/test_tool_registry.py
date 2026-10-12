"""Tool registry contracts (FC-3, FC-4 schema parity, TLC-6, TLC-11).

The registry is the single source of truth for tools per TLC-11 (one
registry instance shared by the in-process tool loop and the MCP
bridge). It rejects duplicate registrations, validates `args` against
each tool's `input_schema` before dispatch (TLC-6), and surfaces
unknown-tool lookups as `KeyError`.

Tester barrier: we only read interface files (`tools/base.py`,
`tools/registry.py`, `tools/echo.py` static surface). The bodies are
`...` until the implementer pass — every test here will fail with
AttributeError or "...-returns-None" until that lands. That is
expected.
"""

from __future__ import annotations

from typing import Any

import pytest

from ember.errors import ToolValidationError
from ember.tools.base import Tool, ToolResult
from ember.tools.echo import EchoTool
from ember.tools.registry import Registry


# --- Helpers --------------------------------------------------------------


class _RecordingTool:
    """A minimal Tool implementation used to verify dispatch.

    Strict schema: requires `text: string`, no additional properties.
    Records args on each call so the test can verify (or refute)
    invocation.
    """

    name: str = "rec"
    description: str = "Recording tool for tests."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
        },
        "required": ["text"],
        "additionalProperties": False,
    }
    requires_confirmation: bool = False

    def __init__(self, name: str = "rec") -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []

    async def call(self, args: dict[str, Any]) -> ToolResult:
        self.calls.append(args)
        return ToolResult(output={"echo": args.get("text")}, is_error=False)


def _make_registry_with_echo() -> tuple[Registry, EchoTool]:
    reg = Registry()
    et = EchoTool()
    reg.register(et)
    return reg, et


# --- TLC-11: single registration per name --------------------------------


class TestRegistration:
    def test_register_then_get_round_trip(self) -> None:
        reg, et = _make_registry_with_echo()
        assert reg.get("echo") is et

    def test_register_duplicate_name_raises_value_error(self) -> None:
        reg = Registry()
        reg.register(EchoTool())
        # FC-3 / FC-4 schema parity — one canonical registration per
        # name. Duplicate must raise ValueError per the registry's
        # docstring.
        with pytest.raises(ValueError):
            reg.register(EchoTool())

    def test_register_duplicate_name_with_different_instance_raises(self) -> None:
        # Different instance, same name → still rejected. The contract
        # is on the name, not the object identity.
        reg = Registry()
        reg.register(_RecordingTool(name="dupe"))
        with pytest.raises(ValueError):
            reg.register(_RecordingTool(name="dupe"))

    def test_get_unknown_raises_key_error(self) -> None:
        reg = Registry()
        with pytest.raises(KeyError):
            reg.get("does-not-exist")


# --- Order stability (TLC-12 by way of FC-4 schema parity) ---------------


class TestOrderStability:
    def test_names_returned_sorted(self) -> None:
        reg = Registry()
        reg.register(_RecordingTool(name="zeta"))
        reg.register(_RecordingTool(name="alpha"))
        reg.register(_RecordingTool(name="mu"))
        assert reg.names() == ["alpha", "mu", "zeta"]

    def test_names_empty_registry(self) -> None:
        reg = Registry()
        assert reg.names() == []

    def test_list_tools_is_order_stable(self) -> None:
        reg = Registry()
        names = ["c", "a", "b"]
        for n in names:
            reg.register(_RecordingTool(name=n))
        # Two consecutive list_tools() calls must yield the same
        # ordering — FC-4 schema parity says the bridge advertisement
        # is byte-equal to the in-process registry, which only holds
        # if iteration order is stable.
        first = [t.name for t in reg.list_tools()]
        second = [t.name for t in reg.list_tools()]
        assert first == second


# --- TLC-6: schema validation at the registry boundary -------------------


class TestSchemaValidation:
    async def test_valid_args_invokes_tool(self) -> None:
        reg = Registry()
        rec = _RecordingTool()
        reg.register(rec)
        result = await reg.call_tool("rec", {"text": "hello"})
        assert isinstance(result, ToolResult)
        assert rec.calls == [{"text": "hello"}]

    async def test_missing_required_field_raises_validation_error(self) -> None:
        reg = Registry()
        rec = _RecordingTool()
        reg.register(rec)
        with pytest.raises(ToolValidationError):
            await reg.call_tool("rec", {})  # missing required `text`
        # Tool was NOT invoked.
        assert rec.calls == []

    async def test_extra_unknown_field_raises_validation_error(self) -> None:
        reg = Registry()
        rec = _RecordingTool()
        reg.register(rec)
        # additionalProperties: false on the recording tool's schema.
        with pytest.raises(ToolValidationError):
            await reg.call_tool("rec", {"text": "ok", "stowaway": 42})
        assert rec.calls == []

    async def test_wrong_type_raises_validation_error(self) -> None:
        reg = Registry()
        rec = _RecordingTool()
        reg.register(rec)
        with pytest.raises(ToolValidationError):
            await reg.call_tool("rec", {"text": 42})  # int, not string
        assert rec.calls == []

    async def test_unknown_tool_name_raises_key_error(self) -> None:
        reg = Registry()
        with pytest.raises(KeyError):
            await reg.call_tool("nope", {"text": "hi"})

    async def test_validation_error_is_tool_error_subclass(self) -> None:
        # Defensive: ToolValidationError should be catchable as
        # ToolError (per errors.py taxonomy). If a future refactor
        # severs that hierarchy, tests that catch ToolError silently
        # stop catching validation failures.
        from ember.errors import ToolError

        reg = Registry()
        reg.register(_RecordingTool())
        with pytest.raises(ToolError):
            await reg.call_tool("rec", {})


# --- Protocol conformance check on EchoTool -------------------------------


class TestEchoToolIsATool:
    def test_echo_tool_satisfies_protocol(self) -> None:
        # runtime_checkable Protocol; EchoTool must satisfy it.
        assert isinstance(EchoTool(), Tool)


# --- Listener hooks (bridge live-sync) ------------------------------------


class _ListenerTool(EchoTool):
    name: str = "listener_target"


class TestListeners:
    """`add_listener` lets the MCP bridge mirror runtime registry
    changes into its served tool set (mcp_connect after boot)."""

    def test_on_register_fires_with_tool(self) -> None:
        reg = Registry()
        seen: list[str] = []
        reg.add_listener(on_register=lambda t: seen.append(t.name))
        reg.register(_ListenerTool())
        assert seen == ["listener_target"]

    def test_on_unregister_fires_only_on_actual_removal(self) -> None:
        reg = Registry()
        removed: list[str] = []
        reg.add_listener(on_unregister=removed.append)
        reg.register(_ListenerTool())
        assert reg.unregister("absent") is False
        assert removed == []
        assert reg.unregister("listener_target") is True
        assert removed == ["listener_target"]

    def test_raising_listener_does_not_break_registry(self) -> None:
        reg = Registry()

        def boom(_arg: Any) -> None:
            raise RuntimeError("mirror broke")

        reg.add_listener(on_register=boom, on_unregister=boom)
        reg.register(_ListenerTool())
        assert reg.has("listener_target")
        assert reg.unregister("listener_target") is True

    def test_duplicate_register_does_not_fire_listener(self) -> None:
        reg = Registry()
        seen: list[str] = []
        reg.register(_ListenerTool())
        reg.add_listener(on_register=lambda t: seen.append(t.name))
        with pytest.raises(ValueError):
            reg.register(_ListenerTool())
        assert seen == []
