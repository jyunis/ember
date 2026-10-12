"""EchoTool — the trivial tool wired up for v0.5 (FC-3).

Verifies the static surface (name, description, schema, confirmation
flag) and the round-trip behavior `call({"text": x}) → {"text": x}`.
The EchoTool body is `...` until the implementer pass; the static
attributes are materialized so most of these tests are answerable from
the interface alone, while the `call` behavior tests will fail until
the body lands.
"""

from __future__ import annotations

from ember.tools.base import ToolResult
from ember.tools.echo import EchoTool


class TestStaticSurface:
    def test_name_is_echo(self) -> None:
        assert EchoTool().name == "echo"

    def test_does_not_require_confirmation(self) -> None:
        # Echo is a read/no-op tool. Per TLC-3 it's NOT destructive.
        assert EchoTool().requires_confirmation is False

    def test_schema_requires_text_string(self) -> None:
        schema = EchoTool().input_schema
        assert schema["type"] == "object"
        assert "text" in schema["properties"]
        assert schema["properties"]["text"]["type"] == "string"
        assert schema["required"] == ["text"]
        assert schema["additionalProperties"] is False

    def test_description_is_nonempty_string(self) -> None:
        assert isinstance(EchoTool().description, str)
        assert EchoTool().description.strip() != ""


class TestCallBehavior:
    """`call` is `...` until the implementer pass — these will fail
    with a `None` return until then."""

    async def test_call_returns_text_unchanged(self) -> None:
        result = await EchoTool().call({"text": "hello"})
        assert isinstance(result, ToolResult)
        assert result.is_error is False
        assert result.output == {"text": "hello"}

    async def test_call_handles_unicode(self) -> None:
        result = await EchoTool().call({"text": "héllo 🔥"})
        assert isinstance(result, ToolResult)
        assert result.is_error is False
        assert result.output == {"text": "héllo 🔥"}

    async def test_call_empty_string_round_trips(self) -> None:
        # Empty string is valid per the JSON schema (type: string with
        # no minLength). The tool should round-trip it unchanged.
        # NOTE [ambiguity]: spec doesn't pin behavior on empty string
        # explicitly; if implementer rejects, flag for spec.
        result = await EchoTool().call({"text": ""})
        assert isinstance(result, ToolResult)
        assert result.output == {"text": ""}
