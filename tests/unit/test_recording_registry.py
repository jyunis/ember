"""RecordingRegistry — the FC-21 "no real side effects" tool recorder.

Tester barrier (UoW-21 / FC-21): these tests are written against
`evals/base.py` (the `ToolCallRecorder` Protocol) + `tools/base.py` +
`tools/registry.py` ONLY. We have NOT read `evals/recording.py` — the
unit under test. ImportError on first run is expected.

Contract (from `evals/base.py` ToolCallRecorder docstring, doc #36 § Layer 2):
- Exposes the SAME tool specs as the wrapped real registry (identical
  catalog → faithful routing).
- `call_tool` NEVER invokes the wrapped real tool's `call()`. No real
  side effect can occur. It records the call and returns either the
  case's scripted output for that tool or a generic inert stub.
- Every call (expected or not, scripted or not) is appended to `calls`
  in invocation order.
- Confirmation gating is bypassed (evals never prompt a human).

Constructor shape (committed): ``RecordingRegistry(real_registry, scripted=None)``.
"""

from __future__ import annotations

from typing import Any

from ember.evals.base import RecordedToolCall, ToolCallRecorder
from ember.evals.recording import RecordingRegistry
from ember.tools.base import ToolResult
from ember.tools.registry import Registry

# Permissive schema so Registry's TLC-6 arg-validation never interferes
# with what these tests are actually exercising.
_ANY_ARGS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": True,
}


# --- Fake tools -----------------------------------------------------------


class _ExplodingTool:
    """A tool whose `call()` blows up. If the recorder ever invokes the
    real tool, the AssertionError surfaces and the test fails loudly —
    that's exactly the FC-21 "no real side effect" tripwire."""

    def __init__(self, name: str, *, requires_confirmation: bool = False) -> None:
        self.name = name
        self.description = f"exploding tool {name}"
        self.input_schema = _ANY_ARGS
        self.requires_confirmation = requires_confirmation
        self.call_count = 0

    async def call(self, args: dict[str, Any]) -> ToolResult:
        self.call_count += 1
        raise AssertionError("real tool executed!")


def _real_registry(*tools: Any) -> Registry:
    reg = Registry()
    for t in tools:
        reg.register(t)
    return reg


# --- Protocol conformance -------------------------------------------------


class TestProtocolConformance:
    def test_is_tool_call_recorder(self) -> None:
        reg = _real_registry(_ExplodingTool("a"))
        rec = RecordingRegistry(reg)
        assert isinstance(rec, ToolCallRecorder)

    def test_calls_starts_empty(self) -> None:
        rec = RecordingRegistry(_real_registry(_ExplodingTool("a")))
        assert rec.calls == []


# --- Identical catalog ----------------------------------------------------


class TestFaithfulCatalog:
    def test_list_tools_matches_wrapped_specs(self) -> None:
        # The model must see an identical catalog so routing is faithful.
        t1 = _ExplodingTool("alpha")
        t2 = _ExplodingTool("beta")
        reg = _real_registry(t1, t2)
        rec = RecordingRegistry(reg)

        real_specs = {
            (t.name, t.description, _schema_key(t.input_schema)) for t in reg.list_tools()
        }
        rec_specs = {(t.name, t.description, _schema_key(t.input_schema)) for t in rec.list_tools()}
        assert rec_specs == real_specs

    def test_list_tools_same_count_and_names(self) -> None:
        reg = _real_registry(_ExplodingTool("alpha"), _ExplodingTool("beta"))
        rec = RecordingRegistry(reg)
        real_names = sorted(t.name for t in reg.list_tools())
        rec_names = sorted(t.name for t in rec.list_tools())
        assert rec_names == real_names == ["alpha", "beta"]


def _schema_key(schema: dict[str, Any]) -> str:
    import json

    return json.dumps(schema, sort_keys=True)


# --- FC-21: no real side effects ------------------------------------------


class TestNoRealSideEffects:
    async def test_call_tool_never_invokes_real_tool(self) -> None:
        # The core FC-21 guarantee: the wrapped tool's `call()` raises,
        # yet `call_tool` returns a ToolResult WITHOUT raising.
        boom = _ExplodingTool("boom")
        rec = RecordingRegistry(_real_registry(boom))
        result = await rec.call_tool("boom", {"x": 1})
        assert isinstance(result, ToolResult)
        assert boom.call_count == 0, "recorder must NOT invoke the real tool"

    async def test_two_different_unscripted_tools_dont_execute(self) -> None:
        a = _ExplodingTool("a")
        b = _ExplodingTool("b")
        rec = RecordingRegistry(_real_registry(a, b))
        ra = await rec.call_tool("a", {})
        rb = await rec.call_tool("b", {})
        assert isinstance(ra, ToolResult)
        assert isinstance(rb, ToolResult)
        assert ra.output is not None
        assert rb.output is not None
        assert a.call_count == 0
        assert b.call_count == 0


# --- Audit log: .calls ----------------------------------------------------


class TestCallRecording:
    async def test_records_call_name_and_args(self) -> None:
        rec = RecordingRegistry(_real_registry(_ExplodingTool("a")))
        await rec.call_tool("a", {"k": "v"})
        assert len(rec.calls) == 1
        call = rec.calls[0]
        assert isinstance(call, RecordedToolCall)
        assert call.name == "a"
        assert call.args == {"k": "v"}

    async def test_records_in_invocation_order(self) -> None:
        rec = RecordingRegistry(_real_registry(_ExplodingTool("a"), _ExplodingTool("b")))
        await rec.call_tool("a", {"n": 1})
        await rec.call_tool("b", {"n": 2})
        await rec.call_tool("a", {"n": 3})
        assert [(c.name, c.args) for c in rec.calls] == [
            ("a", {"n": 1}),
            ("b", {"n": 2}),
            ("a", {"n": 3}),
        ]

    async def test_records_unscripted_and_unexpected_calls(self) -> None:
        # Calls to tools that are NOT scripted and calls the case never
        # anticipated are STILL recorded — the runner needs the full log
        # to assert (or flag) the actual sequence.
        rec = RecordingRegistry(
            _real_registry(_ExplodingTool("scripted"), _ExplodingTool("surprise")),
            scripted={"scripted": {"ok": True}},
        )
        await rec.call_tool("surprise", {"unexpected": True})
        assert [c.name for c in rec.calls] == ["surprise"]


# --- Scripted vs stub outputs ---------------------------------------------


class TestScriptedOutputs:
    async def test_scripted_output_returned(self) -> None:
        rec = RecordingRegistry(
            _real_registry(_ExplodingTool("weather")),
            scripted={"weather": {"temp": 72, "sky": "clear"}},
        )
        result = await rec.call_tool("weather", {"city": "SF"})
        assert isinstance(result, ToolResult)
        assert result.output == {"temp": 72, "sky": "clear"}

    async def test_unscripted_returns_inert_stub(self) -> None:
        # Not over-pinning the stub shape — just that it's a ToolResult
        # with a non-None output and the real tool never ran.
        boom = _ExplodingTool("unscripted")
        rec = RecordingRegistry(_real_registry(boom), scripted={"other": 1})
        result = await rec.call_tool("unscripted", {})
        assert isinstance(result, ToolResult)
        assert result.output is not None
        assert boom.call_count == 0

    async def test_scripted_none_treated_as_no_scripts(self) -> None:
        # scripted=None default: every call falls to the inert stub.
        boom = _ExplodingTool("x")
        rec = RecordingRegistry(_real_registry(boom))
        result = await rec.call_tool("x", {})
        assert isinstance(result, ToolResult)
        assert result.output is not None
        assert boom.call_count == 0


# --- Confirmation gate bypassed -------------------------------------------


class TestConfirmationBypass:
    async def test_confirmation_required_tool_not_gated_not_executed(self) -> None:
        # A tool declaring requires_confirmation=True must NOT produce a
        # `confirmation_required` marker (evals never prompt a human) and
        # must NOT execute (its call() raises). It gets a normal stub.
        gated = _ExplodingTool("gated", requires_confirmation=True)
        rec = RecordingRegistry(_real_registry(gated))
        result = await rec.call_tool("gated", {"target": "/etc"})
        assert isinstance(result, ToolResult)
        # Not a confirmation marker.
        if isinstance(result.output, dict):
            assert result.output.get("error") != "confirmation_required"
        assert result.is_error is False
        assert gated.call_count == 0
        # Still recorded.
        assert [c.name for c in rec.calls] == ["gated"]

    async def test_scripted_confirmation_tool_returns_script(self) -> None:
        gated = _ExplodingTool("gated", requires_confirmation=True)
        rec = RecordingRegistry(_real_registry(gated), scripted={"gated": {"done": True}})
        result = await rec.call_tool("gated", {"target": "/etc"})
        assert result.output == {"done": True}
        assert gated.call_count == 0
