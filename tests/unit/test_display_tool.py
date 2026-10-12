"""display_push — inkframe e-ink display tool (FC-3, TLC-6, TLC-7).

Covers:
- static surface (name, description, schema shape, no confirmation)
- registry schema validation (TLC-6)
- per-type payload construction against POST /push
- type-specific required-field validation (ToolError in the tool)
- error mapping: httpx.ConnectError → ToolRetryable, 4xx/5xx → ToolError
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ember.errors import ToolError, ToolRetryable, ToolValidationError
from ember.tools.base import Tool, ToolResult
from ember.tools.display import DisplayPushTool
from ember.tools.registry import Registry


BASE_URL = "http://display-manager.local:9928"


# --- Stub httpx ---------------------------------------------------------------


class _StubResponse:
    def __init__(self, *, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> Any:
        return self._payload


class _StubClient:
    """Drop-in for `httpx.AsyncClient(...)`. Records requests; returns the
    class-level queued response (default: `{"ok": true}`)."""

    response: _StubResponse = _StubResponse(payload={"ok": True, "type": "message"})
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_StubClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    @classmethod
    def reset(cls) -> None:
        cls.response = _StubResponse(payload={"ok": True, "type": "message"})
        cls.calls = []

    async def request(self, method: str, url: str, **kwargs: Any) -> _StubResponse:
        type(self).calls.append((method.upper(), url, kwargs))
        return type(self).response

    async def post(self, url: str, **kwargs: Any) -> _StubResponse:
        return await self.request("POST", url, **kwargs)


class _BrokenClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_BrokenClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        raise httpx.ConnectError("connection refused")

    async def post(self, url: str, **kwargs: Any) -> Any:
        raise httpx.ConnectError("connection refused")


@pytest.fixture
def stub_httpx(monkeypatch: pytest.MonkeyPatch) -> type[_StubClient]:
    _StubClient.reset()
    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
    return _StubClient


def _make() -> DisplayPushTool:
    return DisplayPushTool(base_url=BASE_URL)


def _sent_json() -> dict[str, Any]:
    assert len(_StubClient.calls) == 1
    method, url, kwargs = _StubClient.calls[0]
    assert method == "POST"
    assert url == f"{BASE_URL}/push"
    return kwargs["json"]


# --- FC-3 static surface ------------------------------------------------------


class TestStaticSurface:
    def test_name(self) -> None:
        assert DisplayPushTool.name == "display_push"

    def test_description_nonempty(self) -> None:
        assert isinstance(DisplayPushTool.description, str)
        assert DisplayPushTool.description.strip() != ""

    def test_schema_shape(self) -> None:
        s = DisplayPushTool.input_schema
        assert s["type"] == "object"
        assert s["additionalProperties"] is False
        props = s["properties"]
        assert props["type"]["enum"] == ["message", "alert", "news", "quote", "countdown"]
        assert props["type"]["default"] == "message"
        assert props["text"]["type"] == "string"
        assert props["sender"]["default"] == "ember"
        assert props["ttl_minutes"]["type"] == "integer"
        assert props["ttl_minutes"]["minimum"] == 0
        assert props["ttl_minutes"]["default"] == 0
        for optional in ("author", "label", "target_date"):
            assert props[optional]["type"] == "string"
        # No universally required field — type-specific requirements are
        # enforced in the tool.
        assert s.get("required", []) == []

    def test_not_destructive(self) -> None:
        assert DisplayPushTool.requires_confirmation is False

    def test_satisfies_tool_protocol(self) -> None:
        assert isinstance(_make(), Tool)


# --- TLC-6 (registry validates against schema) -------------------------------


class TestRegistryValidation:
    async def test_rejects_extra_property(self) -> None:
        reg = Registry()
        reg.register(_make())
        with pytest.raises(ToolValidationError):
            await reg.call_tool("display_push", {"text": "hi", "bogus": 1})

    async def test_rejects_bad_type_enum(self) -> None:
        reg = Registry()
        reg.register(_make())
        with pytest.raises(ToolValidationError):
            await reg.call_tool("display_push", {"type": "recipe", "text": "hi"})

    async def test_rejects_negative_ttl(self) -> None:
        reg = Registry()
        reg.register(_make())
        with pytest.raises(ToolValidationError):
            await reg.call_tool("display_push", {"text": "hi", "ttl_minutes": -5})


# --- Payload construction -----------------------------------------------------


class TestPayloads:
    async def test_message_defaults(self, stub_httpx: type[_StubClient]) -> None:
        result = await _make().call({"text": "hello frame"})
        assert _sent_json() == {
            "type": "message",
            "text": "hello frame",
            "sender": "ember",
            "ttl_minutes": 0,
        }
        assert isinstance(result, ToolResult)
        assert result.output == {"ok": True, "type": "message"}

    async def test_message_with_ttl_and_sender(self, stub_httpx: type[_StubClient]) -> None:
        await _make().call(
            {"type": "message", "text": "workout", "sender": "coach", "ttl_minutes": 120}
        )
        assert _sent_json() == {
            "type": "message",
            "text": "workout",
            "sender": "coach",
            "ttl_minutes": 120,
        }

    async def test_alert_omits_ttl(self, stub_httpx: type[_StubClient]) -> None:
        await _make().call({"type": "alert", "text": "pipe burst"})
        assert _sent_json() == {"type": "alert", "text": "pipe burst"}

    async def test_news_with_ttl(self, stub_httpx: type[_StubClient]) -> None:
        await _make().call({"type": "news", "text": "headline", "ttl_minutes": 30})
        assert _sent_json() == {"type": "news", "text": "headline", "ttl_minutes": 30}

    async def test_quote_with_author(self, stub_httpx: type[_StubClient]) -> None:
        await _make().call({"type": "quote", "text": "be here now", "author": "Ram Dass"})
        assert _sent_json() == {"type": "quote", "text": "be here now", "author": "Ram Dass"}

    async def test_quote_without_author(self, stub_httpx: type[_StubClient]) -> None:
        await _make().call({"type": "quote", "text": "anon wisdom"})
        assert _sent_json() == {"type": "quote", "text": "anon wisdom"}

    async def test_countdown(self, stub_httpx: type[_StubClient]) -> None:
        await _make().call({"type": "countdown", "label": "Vacation", "target_date": "2026-12-20"})
        assert _sent_json() == {
            "type": "countdown",
            "label": "Vacation",
            "target_date": "2026-12-20",
        }


# --- Type-specific required fields (validated in the tool) -------------------


class TestTypeRequirements:
    @pytest.mark.parametrize("kind", ["message", "alert", "news", "quote"])
    async def test_text_required(self, stub_httpx: type[_StubClient], kind: str) -> None:
        with pytest.raises(ToolError) as exc_info:
            await _make().call({"type": kind})
        assert "text" in str(exc_info.value)
        assert _StubClient.calls == []  # no request sent

    async def test_countdown_requires_label_and_date(self, stub_httpx: type[_StubClient]) -> None:
        with pytest.raises(ToolError) as exc_info:
            await _make().call({"type": "countdown", "label": "Vacation"})
        assert "target_date" in str(exc_info.value)
        with pytest.raises(ToolError):
            await _make().call({"type": "countdown", "target_date": "2026-12-20"})
        assert _StubClient.calls == []

    async def test_countdown_rejects_bad_date_format(self, stub_httpx: type[_StubClient]) -> None:
        with pytest.raises(ToolError) as exc_info:
            await _make().call({"type": "countdown", "label": "X", "target_date": "12/20/2026"})
        assert "YYYY-MM-DD" in str(exc_info.value)


# --- Error mapping (FC-3 / TLC-7) --------------------------------------------


class TestErrorMapping:
    async def test_connect_error_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(httpx, "AsyncClient", _BrokenClient)
        with pytest.raises(ToolRetryable):
            await _make().call({"text": "hi"})

    @pytest.mark.parametrize("status", [400, 422])
    async def test_4xx_raises_tool_error(self, stub_httpx: type[_StubClient], status: int) -> None:
        stub_httpx.response = _StubResponse(status_code=status, payload={"error": "bad"})
        with pytest.raises(ToolError) as exc_info:
            await _make().call({"text": "hi"})
        assert str(status) in str(exc_info.value)

    @pytest.mark.parametrize("status", [500, 503])
    async def test_5xx_raises_tool_error(self, stub_httpx: type[_StubClient], status: int) -> None:
        stub_httpx.response = _StubResponse(status_code=status, payload={"error": "boom"})
        with pytest.raises(ToolError):
            await _make().call({"text": "hi"})
