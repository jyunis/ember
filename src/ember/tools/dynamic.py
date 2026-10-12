"""Dynamic tool wrapping.

Helpers that turn discovered MCP tools (with arbitrary JSON schemas)
into:

1. An in-process `Tool` instance (so the local `Registry` can hold
   them alongside hand-coded tools); and
2. A FastMCP-friendly Python callable with a synthesized signature, so
   the bridge can re-expose the tool with the *same* `inputSchema`
   byte-for-byte (FC-4 schema parity).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from inspect import Parameter, Signature
from typing import Any, Union

from ember.tools.base import Tool, ToolResult


def make_dynamic_tool(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    invoker: Callable[[dict[str, Any]], Awaitable[Any]],
    requires_confirmation: bool = False,
) -> Tool:
    """Build an in-process `Tool` whose `call(args)` dispatches to
    `invoker(args)`. Schema preserved as-is (TLC-6 / FC-4)."""

    class _DynamicTool:
        pass

    obj = _DynamicTool()
    obj.name = name  # type: ignore[attr-defined]
    obj.description = description  # type: ignore[attr-defined]
    obj.input_schema = input_schema  # type: ignore[attr-defined]
    obj.requires_confirmation = requires_confirmation  # type: ignore[attr-defined]

    async def _call(args: dict[str, Any]) -> ToolResult:
        result = await invoker(args)
        if isinstance(result, ToolResult):
            return result
        return ToolResult(output=result)

    obj.call = _call  # type: ignore[attr-defined]
    return obj  # type: ignore[return-value]


_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _py_type(prop_schema: dict[str, Any]) -> Any:
    schema_type = prop_schema.get("type")
    if isinstance(schema_type, list):
        # union type — pick the first non-null
        non_null = [t for t in schema_type if t != "null"]
        if non_null:
            return _TYPE_MAP.get(non_null[0], Any)
        return Any
    if isinstance(schema_type, str):
        return _TYPE_MAP.get(schema_type, Any)
    return Any


def make_bridge_wrapper(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    invoker: Callable[[dict[str, Any]], Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Return an async callable whose `__signature__` reflects the
    properties of `input_schema`. FastMCP introspects the signature to
    derive its public schema; we match the original schema's
    required/optional/typed shape.
    """
    properties: dict[str, dict[str, Any]] = input_schema.get("properties") or {}
    required: set[str] = set(input_schema.get("required") or [])

    params: list[Parameter] = []
    annotations: dict[str, Any] = {}
    # Optionals whose None default is synthesized (no schema default).
    # When the caller omits them, FastMCP binds None — which is NOT a
    # valid value under e.g. `{"type": "string"}` and fails validation
    # both in our registry and upstream ("None is not of type 'string'").
    # The wrapper drops these keys when their value is None: omitted
    # means absent, not null.
    synthesized_null: set[str] = set()
    for prop_name, prop_schema in properties.items():
        py_type = _py_type(prop_schema)
        is_required = prop_name in required
        has_default = "default" in prop_schema

        if is_required:
            annotation = py_type
            default: Any = Parameter.empty
        elif has_default:
            annotation = py_type
            default = prop_schema["default"]
        else:
            # Optional field with no explicit default — make the annotation
            # nullable so the synthesized signature is type/default-coherent
            # for FastMCP / pydantic validation. Without this, e.g. a
            # `{"type": "string"}` optional property produces
            # `param: str = None`, which pydantic rejects.
            annotation = py_type if py_type is Any else Union[py_type, None]  # noqa: UP007
            default = None
            synthesized_null.add(prop_name)

        annotations[prop_name] = annotation
        params.append(
            Parameter(
                name=prop_name,
                kind=Parameter.KEYWORD_ONLY,
                default=default,
                annotation=annotation,
            )
        )

    async def wrapper(**kwargs: Any) -> Any:
        args = {k: v for k, v in kwargs.items() if not (v is None and k in synthesized_null)}
        return await invoker(args)

    wrapper.__name__ = name
    wrapper.__qualname__ = name
    wrapper.__doc__ = description
    wrapper.__signature__ = Signature(  # type: ignore[attr-defined]
        parameters=params, return_annotation=Any
    )
    annotations["return"] = Any
    wrapper.__annotations__ = annotations
    return wrapper


def synthesized_signature_kinds(fn: Callable[..., Any]) -> set[Any]:
    """Helper for tests: the kinds of parameters in a synthesized fn."""
    sig = inspect.signature(fn)
    return {p.kind for p in sig.parameters.values()}
