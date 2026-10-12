"""WebSearchTool unit tests (FC-3, TLC-4, TLC-7) — v10.

Covers `ember.tools.web.WebSearchTool`:
- FC-3 static surface (name, description, input_schema, requires_confirmation, Tool protocol).
- Behavior: POST/GET to Brave search, header `X-Subscription-Token`, parsed result shape.
- TLC-7 error mapping:
    HTTP 401/403 → AuthError
    HTTP 429    → RateLimitError
    HTTP 5xx    → ToolRetryable
    Other 4xx   → ToolError
    Network err → TransportError
- TLC-4: api key never appears in surfaced errors / log output.

Tester-flagged ambiguities (also in the report):

1. **HTTP method (GET vs POST)** — spec wording says "POST or GET". Brave's
   public API uses GET. Tests assert that the request is *some* HTTP call to
   `/res/v1/web/search` carrying the query; method is not pinned.

2. **Where `count` is forwarded** — params, query string, or JSON body. Tests
   accept any of those.

3. **`count` out-of-range tolerance** — the registry validates input_schema
   before `call`; the tool may assume valid args. We still smoke-test that
   `count: 0` and `count: 21` (out of declared range) don't blow up if the
   tool happens to receive them — implementer may clamp, reject, or just
   forward. We only assert `call` either returns a `ToolResult` OR raises a
   typed error — never crashes with a raw exception.

4. **`web` key missing in response** — spec says return empty list. We
   accept empty `output["results"]`.

5. **Empty results vs missing `web` key** — both treated as zero results.

6. **`AuthError`/`RateLimitError`/`TransportError` are `ProviderError`
   subclasses, not `ToolError` subclasses.** The implementer raises them
   directly from `call()` per the v10 spec (this differs from the mealie
   pattern, which collapsed everything to `ToolError`).
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
    TransportError,
)
from ember.tools.base import Tool, ToolResult
from ember.tools.web import WebSearchTool


BASE_URL = "https://api.search.brave.com"
# Sentinel that includes the literal "SECRET" so we can grep it back out of
# any captured error text or log output.
API_KEY = "sk-brave-XXX-SECRET-XXX-YYY"


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


def _brave_response(items: list[dict[str, Any]] | None, *, status: int = 200) -> httpx.Response:
    if status >= 400:
        # The implementer may or may not parse the error body; we still
        # return JSON so json() works if they call it.
        return httpx.Response(status, json={"error": "x"})
    body: dict[str, Any] = {}
    if items is not None:
        body["web"] = {"results": items}
    return httpx.Response(200, json=body)


# --- Helpers ------------------------------------------------------------------


def _make() -> WebSearchTool:
    return WebSearchTool(api_key=API_KEY, base_url=BASE_URL)


def _query_params_of(req: httpx.Request) -> dict[str, list[str]]:
    """Return the request's effective query params — combines URL query
    string with form/JSON body when applicable, so tests don't need to know
    where the implementer chose to put `q` and `count`."""
    parsed = urlparse(str(req.url))
    out: dict[str, list[str]] = {k: list(v) for k, v in parse_qs(parsed.query).items()}
    # Also peek at body in case the implementer used POST.
    body_bytes = req.content or b""
    if body_bytes:
        body_text = body_bytes.decode("utf-8", errors="replace")
        # Best-effort: form-urlencoded
        try:
            for k, v in parse_qs(body_text).items():
                out.setdefault(k, []).extend(v)
        except Exception:
            pass
        # Best-effort: JSON
        try:
            import json as _json

            data = _json.loads(body_text)
            if isinstance(data, dict):
                for k, v in data.items():
                    out.setdefault(k, []).append(str(v))
        except Exception:
            pass
    return out


# --- FC-3 static surface ------------------------------------------------------


class TestStaticSurface:
    def test_name(self) -> None:
        assert WebSearchTool.name == "web_search"

    def test_description_nonempty(self) -> None:
        assert isinstance(WebSearchTool.description, str)
        assert WebSearchTool.description.strip() != ""

    def test_not_destructive(self) -> None:
        assert WebSearchTool.requires_confirmation is False

    def test_schema_shape(self) -> None:
        s = WebSearchTool.input_schema
        assert s["type"] == "object"
        assert "query" in s["properties"]
        assert s["properties"]["query"]["type"] == "string"
        assert "count" in s["properties"]
        assert s["properties"]["count"]["type"] == "integer"
        # min/max bounds per spec.
        assert s["properties"]["count"].get("minimum") == 1
        assert s["properties"]["count"].get("maximum") == 20
        assert s["properties"]["count"].get("default") == 10
        assert s["required"] == ["query"]

    def test_constructs(self) -> None:
        _make()  # must not raise

    def test_satisfies_tool_protocol(self) -> None:
        assert isinstance(_make(), Tool)


# --- Happy paths --------------------------------------------------------------


class TestHappyPath:
    async def test_returns_parsed_results(self, monkeypatch: pytest.MonkeyPatch) -> None:
        items = [
            {
                "title": "Example One",
                "url": "https://example.com/one",
                "description": "First hit.",
            },
            {
                "title": "Example Two",
                "url": "https://example.com/two",
                "description": "Second hit.",
                "page_age": "2026-04-20T00:00:00",
            },
        ]
        _install_mock_transport(monkeypatch, lambda r: _brave_response(items))

        result = await _make().call({"query": "example"})

        assert isinstance(result, ToolResult)
        assert isinstance(result.output, dict)
        assert "results" in result.output
        out = result.output["results"]
        assert isinstance(out, list)
        assert len(out) == 2
        # title / url / description must round-trip on every result.
        for got, want in zip(out, items, strict=False):
            assert got["title"] == want["title"]
            assert got["url"] == want["url"]
            assert got["description"] == want["description"]

    async def test_published_date_passthrough_when_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If Brave returns a date-ish field, the tool should pass it through
        # under `published_date`. If the implementer filters it, that's
        # spec-compatible too — assertion is loose: date appears somewhere
        # in the surfaced result blob.
        items = [
            {
                "title": "Dated",
                "url": "https://example.com/dated",
                "description": "...",
                "published_date": "2026-01-15",
            }
        ]
        _install_mock_transport(monkeypatch, lambda r: _brave_response(items))
        result = await _make().call({"query": "anything"})
        # If the tool extracts published_date, it should match. If it doesn't,
        # the field is at minimum NOT crashing the call.
        first = result.output["results"][0]
        assert first["title"] == "Dated"
        # FLAG: tester does not pin the field name — could be `published_date`,
        # `page_age`, or omitted entirely. Loose assertion only.

    async def test_published_date_omitted_when_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        items = [
            {
                "title": "No Date",
                "url": "https://example.com/nodate",
                "description": "...",
            }
        ]
        _install_mock_transport(monkeypatch, lambda r: _brave_response(items))
        result = await _make().call({"query": "x"})
        first = result.output["results"][0]
        # If the tool always sets `published_date`, it should be None or absent.
        if "published_date" in first:
            assert first["published_date"] in (
                None,
                "",
            )

    async def test_empty_results(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(monkeypatch, lambda r: _brave_response([]))
        result = await _make().call({"query": "noresults"})
        assert result.output["results"] == []

    async def test_missing_web_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Brave response with no `web` block — tool returns empty list.
        _install_mock_transport(monkeypatch, lambda r: _brave_response(None))
        result = await _make().call({"query": "x"})
        assert result.output["results"] == []


# --- Request shape ------------------------------------------------------------


class TestRequestShape:
    async def test_authorization_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _install_mock_transport(monkeypatch, lambda r: _brave_response([]))
        await _make().call({"query": "x"})
        assert rec.requests, "no request was made"
        req = rec.last
        token = req.headers.get("X-Subscription-Token")
        assert token == API_KEY

    async def test_accept_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _install_mock_transport(monkeypatch, lambda r: _brave_response([]))
        await _make().call({"query": "x"})
        accept = rec.last.headers.get("Accept", "")
        assert "application/json" in accept

    async def test_endpoint_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _install_mock_transport(monkeypatch, lambda r: _brave_response([]))
        await _make().call({"query": "x"})
        assert "/res/v1/web/search" in str(rec.last.url)

    async def test_query_param_q(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _install_mock_transport(monkeypatch, lambda r: _brave_response([]))
        await _make().call({"query": "kittens"})
        params = _query_params_of(rec.last)
        # Brave uses `q`; some implementers might (incorrectly) use `query`.
        # Pin the spec: `q`.
        assert "q" in params
        assert "kittens" in params["q"]

    async def test_count_param_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _install_mock_transport(monkeypatch, lambda r: _brave_response([]))
        await _make().call({"query": "x", "count": 5})
        params = _query_params_of(rec.last)
        # `count` could be in URL or body; either way it should be visible.
        assert "count" in params
        assert "5" in params["count"]

    async def test_special_chars_url_encoded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _install_mock_transport(monkeypatch, lambda r: _brave_response([]))
        await _make().call({"query": "a b&c=d"})
        # The raw URL should NOT contain unencoded `&` or `=` from the query
        # value. Easiest invariant: parsing the URL recovers the original
        # query string intact.
        params = _query_params_of(rec.last)
        assert params.get("q") == ["a b&c=d"]


# --- TLC-7 error mapping ------------------------------------------------------


class TestErrorMapping:
    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_error(self, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
        _install_mock_transport(
            monkeypatch, lambda r: httpx.Response(status, json={"error": "auth"})
        )
        with pytest.raises(AuthError):
            await _make().call({"query": "x"})

    async def test_rate_limit_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(
            monkeypatch, lambda r: httpx.Response(429, json={"error": "slow down"})
        )
        with pytest.raises(RateLimitError):
            await _make().call({"query": "x"})

    @pytest.mark.parametrize("status", [500, 502, 503])
    async def test_5xx_retryable(self, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
        _install_mock_transport(
            monkeypatch, lambda r: httpx.Response(status, json={"error": "boom"})
        )
        # Spec: HTTP 5xx → ToolRetryable.
        from ember.errors import ToolRetryable

        with pytest.raises(ToolRetryable):
            await _make().call({"query": "x"})

    async def test_400_tool_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(400, json={"error": "bad query"}),
        )
        # Other 4xx → ToolError. We accept any subclass except the typed
        # auth/rate-limit ones (those are 401/403/429 only).
        with pytest.raises(ToolError) as exc_info:
            await _make().call({"query": "x"})
        # Make sure it's not a misclassified AuthError/RateLimitError.
        assert not isinstance(exc_info.value, (AuthError, RateLimitError))

    async def test_network_timeout_is_transport_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raising(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timeout", request=request)

        _install_mock_transport(monkeypatch, raising)
        with pytest.raises(TransportError):
            await _make().call({"query": "x"})

    async def test_connect_error_is_transport_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raising(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        _install_mock_transport(monkeypatch, raising)
        with pytest.raises(TransportError):
            await _make().call({"query": "x"})


# --- TLC-4 secret hygiene -----------------------------------------------------


class TestSecretHygiene:
    """The api key must never appear in error messages, exception args, or
    logged output. We use a sentinel API_KEY containing 'SECRET' for grep."""

    async def test_key_not_in_auth_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(monkeypatch, lambda r: httpx.Response(401, json={"error": "auth"}))
        with pytest.raises(AuthError) as exc_info:
            await _make().call({"query": "x"})
        # str(exc) and repr(exc) both must be clean.
        assert "SECRET" not in str(exc_info.value)
        assert "SECRET" not in repr(exc_info.value)
        assert API_KEY not in str(exc_info.value)
        assert API_KEY not in repr(exc_info.value)

    async def test_key_not_in_400_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(monkeypatch, lambda r: httpx.Response(400, json={"error": "bad"}))
        with pytest.raises(ToolError) as exc_info:
            await _make().call({"query": "x"})
        assert "SECRET" not in str(exc_info.value)
        assert API_KEY not in str(exc_info.value)

    async def test_key_not_in_transport_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raising(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        _install_mock_transport(monkeypatch, raising)
        with pytest.raises(TransportError) as exc_info:
            await _make().call({"query": "x"})
        assert "SECRET" not in str(exc_info.value)
        assert API_KEY not in str(exc_info.value)

    async def test_key_not_in_logs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Capture every log line the tool emits across success + failure.
        caplog.set_level(logging.DEBUG, logger="ember.tools.web")
        caplog.set_level(logging.DEBUG, logger="ember")

        # Happy path
        _install_mock_transport(
            monkeypatch, lambda r: _brave_response([{"title": "t", "url": "u", "description": "d"}])
        )
        await _make().call({"query": "x"})

        # 401 path — re-install to flip behavior
        _install_mock_transport(monkeypatch, lambda r: httpx.Response(401, json={"error": "auth"}))
        with pytest.raises(AuthError):
            await _make().call({"query": "x"})

        for record in caplog.records:
            assert "SECRET" not in record.getMessage(), (
                f"api key leaked in log: {record.getMessage()!r}"
            )
            assert API_KEY not in record.getMessage()


# --- Out-of-range count tolerance --------------------------------------------


class TestCountOutOfRange:
    """The registry validates `input_schema` before invoking `call`. So
    these args would normally be rejected upstream. But if `call` is
    invoked directly with out-of-range values (as it is in unit tests),
    the spec says the tool may "defensively clamp or reject". We accept
    either outcome: a `ToolResult` is fine, a typed error is fine, but a
    raw ValueError / KeyError / TypeError is not.
    """

    async def test_count_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(monkeypatch, lambda r: _brave_response([]))
        try:
            result = await _make().call({"query": "x", "count": 0})
        except (ToolError, AuthError, RateLimitError, TransportError):
            return  # acceptable: tool defensively rejected
        assert isinstance(result, ToolResult)

    async def test_count_too_large(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(monkeypatch, lambda r: _brave_response([]))
        try:
            result = await _make().call({"query": "x", "count": 21})
        except (ToolError, AuthError, RateLimitError, TransportError):
            return
        assert isinstance(result, ToolResult)
