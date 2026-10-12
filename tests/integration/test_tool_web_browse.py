"""WebBrowseTool tests (FC-3, TLC-4, TLC-7) — v10.5.

Covers `ember.tools.web.WebBrowseTool`, the browserless-backed companion to
`web_fetch` for JS-rendered pages.

- FC-3 static surface: name, description, input_schema, requires_confirmation,
  Tool protocol.
- Behavior: POST to `{base_url}/content?token=<token>`, JSON body carrying
  the URL, wait_ms, and (when set) wait_for selector. Title scraped from
  `<title>`. Trafilatura extraction for `format=text`; raw HTML for
  `format=html`. Truncation matches web_fetch.
- TLC-7 error mapping:
    HTTP 401/403 → AuthError
    HTTP 429    → RateLimitError
    HTTP 5xx    → ToolRetryable
    HTTP 400 (timeout/navigation body) → ToolError
    Other 4xx   → ToolError
    Network err / timeout → TransportError
- TLC-4: token never appears in surfaced errors / log output.

Tester-flagged ambiguities (also in the report):

1. **Browserless body field names** — the spec says `waitForTimeout` /
   `waitForSelector` are the v2 conventions but explicitly leaves the exact
   field names off the contract. Tests assert that `args.url`, `wait_ms`,
   and `wait_for` end up *somewhere* in the request body — not pinned to
   any particular key name. The body-introspection helper flattens the
   JSON to a string for substring matching against expected values.

2. **`status_code` semantics** — spec says `status_code` is the BROWSERLESS
   API status (always 200 on rendered success), not the upstream page's
   status. Tests assert `status_code == 200` on the happy path; we do NOT
   probe whether a 200-from-browserless-but-upstream-404 surfaces anywhere.

3. **Truncation marker** — spec says "matching web_fetch". Tests look for
   the literal `\n...[truncated]` marker (matches the constant in web.py).
   If the implementer chose a different marker we'd flag, but the spec is
   prescriptive enough that pinning is fair.

4. **400 message format** — spec says `ToolError("web_browse: page load
   failed: ...")` for 400-with-timeout. We confirm a `ToolError` is raised
   and that it's NOT misclassified as AuthError/RateLimitError. We do not
   pin the exact message text.

5. **`wait_for` absent semantics** — spec accepts "no field set, null, or
   absent". We assert the literal string "#main" doesn't appear in the
   body when wait_for is omitted (a strict subset of all three acceptable
   shapes, and avoids relying on knowing which key name the implementer
   chose).

6. **`Content-Type` header form** — `application/json` vs
   `application/json; charset=utf-8`. We accept both via substring match.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ember.errors import (
    AuthError,
    RateLimitError,
    ToolError,
    ToolRetryable,
    TransportError,
)
from ember.tools.base import Tool, ToolResult
from ember.tools.web import WebBrowseTool


BASE_URL = "https://browserless.example.com"
# Sentinel containing the literal "SECRET" so we can grep it back out of any
# error text or log line.
TOKEN = "br-SECRET-XXX-YYY"


# --- httpx.MockTransport plumbing -------------------------------------------


class _Recorder:
    """Captures every request that flows through the mock transport."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def record(self, request: httpx.Request) -> None:
        self.requests.append(request)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


_ORIGINAL_ASYNC_CLIENT = httpx.AsyncClient


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> _Recorder:
    """Patch `httpx.AsyncClient` so every constructed client routes through
    `httpx.MockTransport(handler)`. Returns a recorder that captures all
    outbound requests for assertions.

    Calling this twice in one test (to flip handler mid-test) is supported:
    we always delegate to the module-level real `httpx.AsyncClient`, so the
    second install replaces the first rather than wrapping it.
    """

    rec = _Recorder()

    def wrapping_handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        return handler(request)

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(wrapping_handler)
        return _ORIGINAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return rec


# --- Helpers ------------------------------------------------------------------


def _make() -> WebBrowseTool:
    return WebBrowseTool(base_url=BASE_URL, token=TOKEN)


def _body_text_of(req: httpx.Request) -> str:
    """Return the request body as a string for substring matching. Tests use
    this when they don't want to pin exact JSON field names."""
    body = req.content or b""
    return body.decode("utf-8", errors="replace")


def _body_json_of(req: httpx.Request) -> dict[str, Any] | None:
    import json as _json

    body = req.content or b""
    if not body:
        return None
    try:
        data = _json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _query_params_of(req: httpx.Request) -> dict[str, list[str]]:
    parsed = urlparse(str(req.url))
    return {k: list(v) for k, v in parse_qs(parsed.query).items()}


def _flatten_to_text(obj: Any) -> str:
    """Flatten any JSON-ish structure to a single string for substring
    matching — handles nested dicts/lists without needing to know the
    implementer's chosen keys."""
    if isinstance(obj, dict):
        return " ".join(_flatten_to_text(v) for v in obj.values())
    if isinstance(obj, list):
        return " ".join(_flatten_to_text(v) for v in obj)
    return str(obj)


def _html_response(
    body: str,
    *,
    status: int = 200,
    content_type: str = "text/html",
) -> httpx.Response:
    return httpx.Response(
        status,
        content=body.encode("utf-8"),
        headers={"content-type": content_type},
    )


# --- Sample fixtures ----------------------------------------------------------


_SIMPLE_HTML = (
    "<!doctype html><html><head><title>X</title></head><body><p>Hello world.</p></body></html>"
)

_NO_TITLE_HTML = "<!doctype html><html><body><p>Body without title.</p></body></html>"

_LONG_HTML = (
    "<!doctype html><html><head><title>Big</title></head><body>"
    + ("<p>" + ("x" * 200) + "</p>") * 50
    + "</body></html>"
)


# --- FC-3 static surface ------------------------------------------------------


class TestStaticSurface:
    def test_name(self) -> None:
        assert WebBrowseTool.name == "web_browse"

    def test_description_nonempty(self) -> None:
        assert isinstance(WebBrowseTool.description, str)
        assert WebBrowseTool.description.strip() != ""

    def test_not_destructive(self) -> None:
        assert WebBrowseTool.requires_confirmation is False

    def test_schema_shape(self) -> None:
        s = WebBrowseTool.input_schema
        assert s["type"] == "object"
        # url is required and a string.
        assert "url" in s["properties"]
        assert s["properties"]["url"]["type"] == "string"
        assert s["required"] == ["url"]
        # format optional, enum text|html, default text.
        assert "format" in s["properties"]
        assert s["properties"]["format"]["enum"] == ["text", "html"]
        assert s["properties"]["format"].get("default") == "text"
        # wait_for optional string. Schema must permit null because the
        # bridge fills unset optionals with None at validation time;
        # without the union type, FastMCP rejects with "None is not of
        # type 'string'" before the call reaches our tool.
        assert "wait_for" in s["properties"]
        wait_for_type = s["properties"]["wait_for"]["type"]
        assert wait_for_type == ["string", "null"] or wait_for_type == "string"
        # wait_ms optional integer with bounds.
        assert "wait_ms" in s["properties"]
        assert s["properties"]["wait_ms"]["type"] == "integer"
        assert s["properties"]["wait_ms"].get("default") == 2000
        assert s["properties"]["wait_ms"].get("minimum") == 0
        assert s["properties"]["wait_ms"].get("maximum") == 30000
        # max_chars optional integer with bounds.
        assert "max_chars" in s["properties"]
        assert s["properties"]["max_chars"]["type"] == "integer"
        assert s["properties"]["max_chars"].get("default") == 50000
        assert s["properties"]["max_chars"].get("minimum") == 100
        assert s["properties"]["max_chars"].get("maximum") == 200000

    def test_constructs(self) -> None:
        _make()

    def test_satisfies_tool_protocol(self) -> None:
        assert isinstance(_make(), Tool)


# --- Happy paths --------------------------------------------------------------


class TestHappyPath:
    async def test_text_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(monkeypatch, lambda r: _html_response(_SIMPLE_HTML))
        result = await _make().call({"url": "https://example.com/page", "format": "text"})
        assert isinstance(result, ToolResult)
        out = result.output
        assert isinstance(out, dict)
        assert out["url"] == "https://example.com/page"
        assert out["status_code"] == 200
        assert out["title"] == "X"
        assert "Hello world" in out["content"]
        assert out["content_type"] == "text/html"
        assert out["may_need_browser"] is False

    async def test_html_format_returns_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(monkeypatch, lambda r: _html_response(_SIMPLE_HTML))
        result = await _make().call({"url": "https://example.com/page", "format": "html"})
        out = result.output
        # Raw HTML tags survive in html mode.
        assert "<p>Hello world.</p>" in out["content"]
        assert out["title"] == "X"
        assert out["content_type"] == "text/html"
        assert out["may_need_browser"] is False
        assert out["status_code"] == 200

    async def test_title_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(monkeypatch, lambda r: _html_response(_NO_TITLE_HTML))
        result = await _make().call({"url": "https://example.com/notitle"})
        assert result.output["title"] is None

    async def test_truncation_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(monkeypatch, lambda r: _html_response(_LONG_HTML))
        # Force tight max_chars so we definitely truncate even after
        # trafilatura extraction shrinks the body.
        result = await _make().call(
            {"url": "https://example.com/big", "format": "html", "max_chars": 500}
        )
        content = result.output["content"]
        # Truncation marker matches web_fetch's `\n...[truncated]`.
        assert content.endswith("\n...[truncated]")
        # And the content is in fact bounded near max_chars (allow slack for
        # the marker length).
        assert len(content) <= 500 + 50


# --- Request shape ------------------------------------------------------------


class TestRequestShape:
    async def test_method_is_post(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _install_mock_transport(monkeypatch, lambda r: _html_response(_SIMPLE_HTML))
        await _make().call({"url": "https://example.com/x"})
        assert rec.last.method == "POST"

    async def test_endpoint_path_is_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _install_mock_transport(monkeypatch, lambda r: _html_response(_SIMPLE_HTML))
        await _make().call({"url": "https://example.com/x"})
        path = urlparse(str(rec.last.url)).path
        assert path == "/content"

    async def test_token_in_query_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _install_mock_transport(monkeypatch, lambda r: _html_response(_SIMPLE_HTML))
        await _make().call({"url": "https://example.com/x"})
        params = _query_params_of(rec.last)
        assert "token" in params
        assert TOKEN in params["token"]

    async def test_content_type_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _install_mock_transport(monkeypatch, lambda r: _html_response(_SIMPLE_HTML))
        await _make().call({"url": "https://example.com/x"})
        ct = rec.last.headers.get("content-type", "")
        assert "application/json" in ct.lower()

    async def test_body_contains_input_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _install_mock_transport(monkeypatch, lambda r: _html_response(_SIMPLE_HTML))
        await _make().call({"url": "https://target.example.com/page"})
        body = _body_json_of(rec.last)
        assert body is not None, "request body was not valid JSON"
        flat = _flatten_to_text(body)
        assert "https://target.example.com/page" in flat

    async def test_body_contains_wait_ms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _install_mock_transport(monkeypatch, lambda r: _html_response(_SIMPLE_HTML))
        await _make().call({"url": "https://example.com/x", "wait_ms": 5000})
        body = _body_json_of(rec.last)
        assert body is not None
        # Loose: 5000 must appear *somewhere* in the body — could be under
        # waitForTimeout, gotoOptions.timeout, etc. Field name not pinned.
        flat = _flatten_to_text(body)
        assert "5000" in flat

    async def test_body_contains_wait_for_selector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _install_mock_transport(monkeypatch, lambda r: _html_response(_SIMPLE_HTML))
        await _make().call({"url": "https://example.com/x", "wait_for": "#main"})
        body = _body_json_of(rec.last)
        assert body is not None
        flat = _flatten_to_text(body)
        assert "#main" in flat

    async def test_wait_for_omitted_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _install_mock_transport(monkeypatch, lambda r: _html_response(_SIMPLE_HTML))
        await _make().call({"url": "https://example.com/x"})
        body_text = _body_text_of(rec.last)
        # We don't pin which key the implementer used — just that no
        # selector value was sent. Use a sentinel a bare schema'd request
        # would never invent: "#main" never appears unless we ask for it.
        # Stronger check: scan the body JSON values directly.
        body = _body_json_of(rec.last)
        assert body is not None
        flat = _flatten_to_text(body)
        # Common falsy-but-noisy values should not appear in the flattened
        # values. Specifically, no actual selector text should be present.
        # We can't easily prove "no selector field at all", so we instead
        # confirm the body doesn't carry a value resembling a CSS selector
        # that we never passed.
        assert "#" not in flat or "#main" not in flat
        # And the body parses cleanly (sanity).
        assert isinstance(body_text, str)


# --- TLC-7 error mapping ------------------------------------------------------


class TestErrorMapping:
    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_error(self, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
        _install_mock_transport(
            monkeypatch, lambda r: httpx.Response(status, json={"error": "auth"})
        )
        with pytest.raises(AuthError):
            await _make().call({"url": "https://example.com/x"})

    async def test_rate_limit_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(monkeypatch, lambda r: httpx.Response(429, json={"error": "slow"}))
        with pytest.raises(RateLimitError):
            await _make().call({"url": "https://example.com/x"})

    @pytest.mark.parametrize("status", [500, 502, 503])
    async def test_5xx_retryable(self, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
        _install_mock_transport(
            monkeypatch, lambda r: httpx.Response(status, json={"error": "boom"})
        )
        with pytest.raises(ToolRetryable):
            await _make().call({"url": "https://example.com/x"})

    async def test_400_navigation_timeout_is_tool_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 400 with a body indicating the upstream page failed (timeout /
        # navigation) — surfaces as ToolError, NOT misclassified.
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(400, json={"error": "Navigation timeout"}),
        )
        with pytest.raises(ToolError) as exc_info:
            await _make().call({"url": "https://slow.example.com/"})
        # Don't pin the exact message; just confirm the typed-error
        # hierarchy doesn't collapse to AuthError / RateLimitError.
        assert not isinstance(exc_info.value, (AuthError, RateLimitError, ToolRetryable))

    async def test_404_is_tool_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(
            monkeypatch, lambda r: httpx.Response(404, json={"error": "not found"})
        )
        with pytest.raises(ToolError) as exc_info:
            await _make().call({"url": "https://example.com/x"})
        assert not isinstance(exc_info.value, (AuthError, RateLimitError, ToolRetryable))

    async def test_connect_error_is_transport_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raising(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        _install_mock_transport(monkeypatch, raising)
        with pytest.raises(TransportError):
            await _make().call({"url": "https://example.com/x"})

    async def test_timeout_is_transport_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raising(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out", request=request)

        _install_mock_transport(monkeypatch, raising)
        with pytest.raises(TransportError):
            await _make().call({"url": "https://example.com/x"})


# --- TLC-4 secret hygiene -----------------------------------------------------


class TestSecretHygiene:
    """The browserless token must never appear in error messages or log
    output. Sentinel TOKEN contains 'SECRET' for grep."""

    async def test_token_not_in_auth_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(monkeypatch, lambda r: httpx.Response(401, json={"error": "auth"}))
        with pytest.raises(AuthError) as exc_info:
            await _make().call({"url": "https://example.com/x"})
        assert "SECRET" not in str(exc_info.value)
        assert "SECRET" not in repr(exc_info.value)
        assert TOKEN not in str(exc_info.value)
        assert TOKEN not in repr(exc_info.value)

    async def test_token_not_in_400_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(400, json={"error": "Navigation timeout"}),
        )
        with pytest.raises(ToolError) as exc_info:
            await _make().call({"url": "https://example.com/x"})
        assert "SECRET" not in str(exc_info.value)
        assert TOKEN not in str(exc_info.value)

    async def test_token_not_in_transport_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raising(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        _install_mock_transport(monkeypatch, raising)
        with pytest.raises(TransportError) as exc_info:
            await _make().call({"url": "https://example.com/x"})
        assert "SECRET" not in str(exc_info.value)
        assert TOKEN not in str(exc_info.value)

    async def test_token_not_in_logs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="ember.tools.web")
        caplog.set_level(logging.DEBUG, logger="ember")

        # Happy path
        _install_mock_transport(monkeypatch, lambda r: _html_response(_SIMPLE_HTML))
        await _make().call({"url": "https://example.com/x"})

        # 401 path — flip the handler.
        _install_mock_transport(monkeypatch, lambda r: httpx.Response(401, json={"error": "auth"}))
        with pytest.raises(AuthError):
            await _make().call({"url": "https://example.com/x"})

        # 400 navigation-timeout path
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(400, json={"error": "Navigation timeout"}),
        )
        with pytest.raises(ToolError):
            await _make().call({"url": "https://example.com/x"})

        # Transport failure
        def raising(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        _install_mock_transport(monkeypatch, raising)
        with pytest.raises(TransportError):
            await _make().call({"url": "https://example.com/x"})

        for record in caplog.records:
            msg = record.getMessage()
            assert "SECRET" not in msg, f"token leaked in log: {msg!r}"
            assert TOKEN not in msg, f"token leaked in log: {msg!r}"
