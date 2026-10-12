"""HttpRequestTool — direct HTTP request tool.

Covers:
- FC-3 static surface (Tool protocol conformance).
- Basic GET round-trip → status_code + body + headers.
- POST with json_body sets Content-Type: application/json and serializes.
- Mutually-exclusive json_body + body raises ToolError.
- Non-2xx responses are NOT exceptions (returned as data, like web_fetch).
- Network/timeout failures map to TransportError (TLC-7).
- Custom headers from caller are passed through; default User-Agent is
  added when caller does not specify one (case-insensitive check).
- Body truncation honors max_chars and sets `truncated=True`.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ember.errors import ToolError, TransportError
from ember.tools.base import Tool, ToolResult
from ember.tools.web import HttpRequestTool


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def record(self, request: httpx.Request) -> None:
        self.requests.append(request)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def _install_mock_transport(monkeypatch: pytest.MonkeyPatch, handler) -> _Recorder:
    rec = _Recorder()

    def wrapping_handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        return handler(request)

    real_async_client = httpx.AsyncClient

    def async_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(wrapping_handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_factory)
    return rec


def _make() -> HttpRequestTool:
    return HttpRequestTool()


class TestStaticSurface:
    def test_conforms_to_tool_protocol(self) -> None:
        assert isinstance(_make(), Tool)

    def test_name_and_confirmation(self) -> None:
        t = _make()
        assert t.name == "http_request"
        assert t.requires_confirmation is False

    def test_input_schema_required_url_only(self) -> None:
        s = _make().input_schema
        assert s["required"] == ["url"]
        assert "method" in s["properties"]
        assert "json_body" in s["properties"]


class TestRoundTrip:
    async def test_get_returns_status_body_headers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json", "x-test": "yes"},
                text='{"ok":true}',
            )

        _install_mock_transport(monkeypatch, handler)
        result = await _make().call({"url": "https://example.com/x"})
        assert isinstance(result, ToolResult)
        assert result.output["status_code"] == 200
        assert result.output["body"] == '{"ok":true}'
        assert result.output["method"] == "GET"
        assert result.output["headers"]["x-test"] == "yes"
        assert result.output["truncated"] is False

    async def test_post_json_body_sets_content_type_and_serializes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(201, text="created")

        rec = _install_mock_transport(monkeypatch, handler)
        result = await _make().call(
            {
                "url": "https://api.example.com/things",
                "method": "POST",
                "json_body": {"name": "alpha", "count": 3},
            }
        )
        assert result.output["status_code"] == 201
        assert rec.last.method == "POST"
        assert "application/json" in rec.last.headers.get("content-type", "")
        # httpx serialized the dict; verify it round-trips.
        import json

        assert json.loads(rec.last.content.decode()) == {"name": "alpha", "count": 3}

    async def test_caller_headers_passed_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="ok")

        rec = _install_mock_transport(monkeypatch, handler)
        await _make().call(
            {
                "url": "https://api.example.com/x",
                "headers": {"Authorization": "Bearer xyz"},
            }
        )
        assert rec.last.headers["authorization"] == "Bearer xyz"
        # Default User-Agent added when caller did not specify one.
        assert "ember" in rec.last.headers.get("user-agent", "").lower()

    async def test_caller_user_agent_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="ok")

        rec = _install_mock_transport(monkeypatch, handler)
        await _make().call(
            {
                "url": "https://api.example.com/x",
                "headers": {"user-agent": "custom/9"},
            }
        )
        assert rec.last.headers["user-agent"] == "custom/9"

    async def test_query_params_appended(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="ok")

        rec = _install_mock_transport(monkeypatch, handler)
        await _make().call(
            {
                "url": "https://api.example.com/x",
                "params": {"q": "hello", "n": "5"},
            }
        )
        assert "q=hello" in str(rec.last.url)
        assert "n=5" in str(rec.last.url)


class TestNon2xxIsData:
    async def test_404_returns_status_code_not_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="not found")

        _install_mock_transport(monkeypatch, handler)
        result = await _make().call({"url": "https://example.com/missing"})
        assert result.output["status_code"] == 404
        assert result.output["body"] == "not found"

    async def test_500_returns_status_code_not_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        _install_mock_transport(monkeypatch, handler)
        result = await _make().call({"url": "https://example.com/oops"})
        assert result.output["status_code"] == 500


class TestErrors:
    async def test_json_body_and_body_together_raises_tool_error(self) -> None:
        with pytest.raises(ToolError):
            await _make().call(
                {
                    "url": "https://example.com",
                    "json_body": {"x": 1},
                    "body": "raw",
                }
            )

    async def test_timeout_raises_transport_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raising(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timeout")

        _install_mock_transport(monkeypatch, raising)
        with pytest.raises(TransportError):
            await _make().call({"url": "https://example.com"})

    async def test_connect_error_raises_transport_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raising(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        _install_mock_transport(monkeypatch, raising)
        with pytest.raises(TransportError):
            await _make().call({"url": "https://example.com"})


class TestTruncation:
    async def test_body_truncated_above_max_chars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        big = "A" * 5000

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=big)

        _install_mock_transport(monkeypatch, handler)
        result = await _make().call({"url": "https://example.com", "max_chars": 1000})
        assert result.output["truncated"] is True
        assert len(result.output["body"]) <= 1000 + 50  # truncation marker slack

    async def test_body_under_cap_not_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="short")

        _install_mock_transport(monkeypatch, handler)
        result = await _make().call({"url": "https://example.com"})
        assert result.output["truncated"] is False
        assert result.output["body"] == "short"
