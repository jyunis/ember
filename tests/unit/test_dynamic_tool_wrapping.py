"""Dynamic tool wrapping (FC-4 schema parity, TLC-6).

Two helpers under test in `tools/dynamic.py`:

1. `make_dynamic_tool` — produces an in-process `Tool` from a JSON
   schema + invoker. The local registry then validates args against
   `input_schema` per TLC-6 and dispatches to the invoker.

2. `make_bridge_wrapper` — produces a Python callable whose
   `__signature__` reflects the schema's properties. FastMCP
   introspects the signature to derive its public schema; the
   synthesized signature must match the schema's required/optional
   shape so the schema bridged to claude is byte-identical to what we
   gave (FC-4 schema parity).

Tester-flagged ambiguities:
- The exact `inspect.Parameter.kind` of synthesized parameters (KEYWORD_ONLY
  vs POSITIONAL_OR_KEYWORD) is not pinned by the spec. We test for either,
  and flag it. FastMCP accepts both.
- Whether `make_dynamic_tool`'s `requires_confirmation` is statically False
  is not pinned either; we assert False because dynamically-discovered MCP
  tools shouldn't trigger TLC-3 confirmation gates by default (those are
  for hand-coded destructive tools).
"""

from __future__ import annotations

import copy
import inspect
from typing import Any


from ember.tools.base import Tool, ToolResult
from ember.tools.dynamic import make_bridge_wrapper, make_dynamic_tool

# A representative schema that covers required + optional + typed +
# default values. Mirrors what an upstream MCP server might advertise.
SAMPLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "what to search"},
        "limit": {"type": "integer", "default": 10, "description": "max results"},
    },
    "required": ["query"],
    "additionalProperties": False,
}


# --- make_dynamic_tool ---------------------------------------------------


class TestMakeDynamicTool:
    def test_returns_tool_protocol_instance(self) -> None:
        async def invoker(args: dict[str, Any]) -> Any:
            return {"ok": True}

        tool = make_dynamic_tool(
            name="memory__search",
            description="search the memory store",
            input_schema=SAMPLE_SCHEMA,
            invoker=invoker,
        )
        assert isinstance(tool, Tool)

    def test_attributes_match_inputs(self) -> None:
        async def invoker(args: dict[str, Any]) -> Any:
            return {}

        tool = make_dynamic_tool(
            name="memory__search",
            description="search the memory store",
            input_schema=SAMPLE_SCHEMA,
            invoker=invoker,
        )
        assert tool.name == "memory__search"
        assert tool.description == "search the memory store"

    def test_input_schema_is_preserved_byte_for_byte(self) -> None:
        # FC-4 schema parity: the schema we hand in is the schema the
        # registry advertises, the bridge re-exposes, and external
        # callers see. No silent normalization.
        original = copy.deepcopy(SAMPLE_SCHEMA)

        async def invoker(args: dict[str, Any]) -> Any:
            return {}

        tool = make_dynamic_tool(
            name="memory__search",
            description="x",
            input_schema=SAMPLE_SCHEMA,
            invoker=invoker,
        )
        assert tool.input_schema == original

    def test_requires_confirmation_defaults_false(self) -> None:
        # Dynamically-discovered MCP tools are not destructive by
        # default; TLC-3 confirmation flag stays False.
        async def invoker(args: dict[str, Any]) -> Any:
            return {}

        tool = make_dynamic_tool(
            name="memory__search",
            description="x",
            input_schema=SAMPLE_SCHEMA,
            invoker=invoker,
        )
        assert tool.requires_confirmation is False

    async def test_call_dispatches_to_invoker_with_args(self) -> None:
        captured: dict[str, Any] = {}

        async def invoker(args: dict[str, Any]) -> Any:
            captured.update(args)
            return {"echo": args}

        tool = make_dynamic_tool(
            name="memory__search",
            description="x",
            input_schema=SAMPLE_SCHEMA,
            invoker=invoker,
        )
        result = await tool.call({"query": "hello", "limit": 5})
        assert isinstance(result, ToolResult)
        assert captured == {"query": "hello", "limit": 5}

    async def test_call_returns_invoker_output_in_tool_result(self) -> None:
        async def invoker(args: dict[str, Any]) -> Any:
            return {"hits": [1, 2, 3]}

        tool = make_dynamic_tool(
            name="memory__search",
            description="x",
            input_schema=SAMPLE_SCHEMA,
            invoker=invoker,
        )
        result = await tool.call({"query": "anything"})
        # The invoker's return value is what's surfaced. The wrapper
        # may pass it through verbatim or under an "output" key — but
        # something searchable must be present.
        assert result.is_error is False
        assert "hits" in repr(result.output)


# --- make_bridge_wrapper -------------------------------------------------


class TestMakeBridgeWrapperSignature:
    def test_returns_callable(self) -> None:
        async def invoker(args: dict[str, Any]) -> Any:
            return {}

        wrapper = make_bridge_wrapper(
            name="search",
            description="x",
            input_schema=SAMPLE_SCHEMA,
            invoker=invoker,
        )
        assert callable(wrapper)

    def test_signature_parameter_names_match_schema_properties(self) -> None:
        async def invoker(args: dict[str, Any]) -> Any:
            return {}

        wrapper = make_bridge_wrapper(
            name="search",
            description="x",
            input_schema=SAMPLE_SCHEMA,
            invoker=invoker,
        )
        sig = inspect.signature(wrapper)
        names = list(sig.parameters.keys())
        # `query` is required, `limit` is optional. Both must be exposed
        # as parameters; their order is the schema's `properties` order.
        assert "query" in names
        assert "limit" in names

    def test_required_property_has_no_default(self) -> None:
        async def invoker(args: dict[str, Any]) -> Any:
            return {}

        wrapper = make_bridge_wrapper(
            name="search",
            description="x",
            input_schema=SAMPLE_SCHEMA,
            invoker=invoker,
        )
        sig = inspect.signature(wrapper)
        assert sig.parameters["query"].default is inspect.Parameter.empty

    def test_optional_property_has_default(self) -> None:
        # `limit` is not in `required`, has a `default: 10` in the
        # schema. The synthesized signature must reflect that — either
        # via the schema default or `None`.
        async def invoker(args: dict[str, Any]) -> Any:
            return {}

        wrapper = make_bridge_wrapper(
            name="search",
            description="x",
            input_schema=SAMPLE_SCHEMA,
            invoker=invoker,
        )
        sig = inspect.signature(wrapper)
        limit = sig.parameters["limit"]
        # Spec doesn't strictly pin: either the schema's default value
        # or None is acceptable. Both indicate "optional" to FastMCP.
        assert limit.default is not inspect.Parameter.empty

    def test_signature_kind_is_acceptable_for_fastmcp(self) -> None:
        # FastMCP introspects via `inspect.signature` and is fine with
        # both KEYWORD_ONLY and POSITIONAL_OR_KEYWORD. We assert one of
        # those two (not VAR_POSITIONAL or VAR_KEYWORD or POSITIONAL_ONLY).
        # FLAG: the spec doesn't pin which; either is acceptable.
        async def invoker(args: dict[str, Any]) -> Any:
            return {}

        wrapper = make_bridge_wrapper(
            name="search",
            description="x",
            input_schema=SAMPLE_SCHEMA,
            invoker=invoker,
        )
        sig = inspect.signature(wrapper)
        for p in sig.parameters.values():
            assert p.kind in (
                inspect.Parameter.KEYWORD_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ), f"unexpected kind for {p.name}: {p.kind}"

    def test_dunder_name_matches_requested_name(self) -> None:
        # FastMCP uses the Python function's `__name__` as a fallback
        # tool identifier. The synthesized callable should match the
        # requested name so logs and traces are readable.
        async def invoker(args: dict[str, Any]) -> Any:
            return {}

        wrapper = make_bridge_wrapper(
            name="memory__search",
            description="x",
            input_schema=SAMPLE_SCHEMA,
            invoker=invoker,
        )
        assert wrapper.__name__ == "memory__search"


class TestMakeBridgeWrapperInvocation:
    async def test_kwargs_are_dispatched_as_dict_to_invoker(self) -> None:
        captured: dict[str, Any] = {}

        async def invoker(args: dict[str, Any]) -> Any:
            captured.update(args)
            return {"ok": True}

        wrapper = make_bridge_wrapper(
            name="search",
            description="x",
            input_schema=SAMPLE_SCHEMA,
            invoker=invoker,
        )
        # FastMCP calls the wrapper with kwargs derived from the
        # MCP request payload. Per the docstring, the wrapper packs
        # those into a single dict and forwards to invoker.
        await wrapper(query="hi", limit=3)
        assert captured == {"query": "hi", "limit": 3}

    async def test_invoker_return_value_is_propagated(self) -> None:
        async def invoker(args: dict[str, Any]) -> Any:
            return {"echoed": args["query"]}

        wrapper = make_bridge_wrapper(
            name="search",
            description="x",
            input_schema=SAMPLE_SCHEMA,
            invoker=invoker,
        )
        out = await wrapper(query="ping")
        # Wrapper passes through whatever the invoker returns to its
        # MCP caller.
        assert "ping" in repr(out)

    async def test_only_required_kwarg_works(self) -> None:
        # When only `query` is supplied, invoker is dispatched without
        # `limit` — or with `limit` set to its default. Both are fine
        # for FC-4 parity; the contract is "valid kwargs reach the
        # invoker as a dict".
        captured: dict[str, Any] = {}

        async def invoker(args: dict[str, Any]) -> Any:
            captured.update(args)
            return {}

        wrapper = make_bridge_wrapper(
            name="search",
            description="x",
            input_schema=SAMPLE_SCHEMA,
            invoker=invoker,
        )
        await wrapper(query="hi")
        # The required arg always reaches the invoker.
        assert captured.get("query") == "hi"


# --- Omitted optionals must not become nulls -------------------------------


# Mirrors wire-wallet's `preview` shape: optionals typed `string` /
# `boolean` with NO default and NO null in their type. Injecting None
# for these fails jsonschema validation ("None is not of type
# 'string'") locally and upstream.
STRICT_OPTIONALS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "description": {"type": "string"},
        "simulate": {"type": "boolean"},
        "headers": {"type": ["object", "null"]},
        "limit": {"type": "integer", "default": 10},
    },
    "required": ["action"],
    "additionalProperties": False,
}


class TestOmittedOptionalsAreDropped:
    @staticmethod
    def _capture_wrapper() -> tuple[Any, dict[str, Any]]:
        captured: dict[str, Any] = {}

        async def invoker(args: dict[str, Any]) -> Any:
            captured.clear()
            captured.update(args)
            return {}

        wrapper = make_bridge_wrapper(
            name="preview",
            description="x",
            input_schema=STRICT_OPTIONALS_SCHEMA,
            invoker=invoker,
        )
        return wrapper, captured

    async def test_omitted_optionals_do_not_reach_invoker_as_none(self) -> None:
        wrapper, captured = self._capture_wrapper()
        # FastMCP binds every signature param, so omitted optionals
        # arrive at the wrapper as None — exactly the failing case.
        await wrapper(action="a", description=None, simulate=None, headers=None, limit=10)
        assert captured == {"action": "a", "limit": 10}

    async def test_supplied_falsy_values_are_forwarded(self) -> None:
        wrapper, captured = self._capture_wrapper()
        await wrapper(action="a", description="", simulate=False, headers=None, limit=0)
        assert captured == {"action": "a", "description": "", "simulate": False, "limit": 0}

    async def test_forwarded_args_pass_schema_validation(self) -> None:
        # End-to-end guarantee: what the wrapper forwards for a
        # required-only call validates against the upstream schema.
        import jsonschema

        wrapper, captured = self._capture_wrapper()
        await wrapper(action="a", description=None, simulate=None, headers=None, limit=10)
        jsonschema.validate(captured, STRICT_OPTIONALS_SCHEMA)

    async def test_required_param_none_is_not_dropped(self) -> None:
        # A required param is never in the synthesized-null set; if the
        # caller really sends null it must surface to validation, not
        # silently vanish into a missing-required error downstream.
        wrapper, captured = self._capture_wrapper()
        await wrapper(action=None)
        assert "action" in captured and captured["action"] is None
