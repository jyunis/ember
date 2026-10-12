"""WebFetchTool tests (FC-3, TLC-7) — v10.

Covers `ember.tools.web.WebFetchTool`:
- FC-3 static surface (name, description, input_schema, requires_confirmation, Tool protocol).
- HTML / text / JSON / binary content-type handling.
- Title extraction.
- `max_chars` truncation.
- Redirect-follow result URL.
- `may_need_browser` heuristic (positive + negative).
- TLC-7 error mapping:
    HTTP 4xx/5xx → ToolResult with status_code; NO exception (distinct from search).
    Network error / timeout → TransportError.
    Binary content → ToolError ("unsupported content-type").
- User-Agent header is sent.

Tester-flagged ambiguities (also in the report):

1. **Truncation marker** — spec leaves marker text up to the implementer.
   We assert `len(content) <= max_chars + 50` (gives 50 chars of slack for
   any "...[truncated]"-style suffix). FLAG.

2. **`may_need_browser` heuristic threshold** — spec phrases it as
   `<200 chars` AND (`>5 scripts` OR known signatures). We exercise a
   clearly-positive case (15+ scripts + `__NEXT_DATA__` + tiny body) and a
   clearly-negative case (rich article HTML, no signatures). The
   ambiguous middle is intentionally not tested.

3. **`format=html` on JSON content** — spec says "format flag is ignored
   for non-HTML". We assert raw JSON text is returned regardless of flag.

4. **Trafilatura availability** — the implementer is expected to add
   `trafilatura` as a runtime dep. If it's not installed at test time,
   the import of `ember.tools.web` will fail and these tests will
   ImportError. That's the right signal.

5. **Title extraction for non-HTML** — spec says "None for non-HTML or
   missing". We assert `result["title"] is None` for text/plain and JSON.

6. **HTML happy path 'content' for format=text** — we assert the
   extracted text is *not* the raw HTML and contains the article body
   text. We do not pin specific trafilatura output (it can vary across
   versions).

7. **Final URL after redirects** — we assert `result["url"]` reflects the
   final destination, not the originally-requested URL.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

from ember.errors import ToolError, TransportError
from ember.tools.base import Tool, ToolResult
from ember.tools.web import WebFetchTool


# --- httpx.MockTransport plumbing -------------------------------------------


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
    real_client = httpx.Client

    def async_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(wrapping_handler)
        return real_async_client(*args, **kwargs)

    def sync_factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(wrapping_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_factory)
    monkeypatch.setattr(httpx, "Client", sync_factory)
    return rec


def _make() -> WebFetchTool:
    return WebFetchTool()


# --- Sample HTML fixtures ----------------------------------------------------


_ARTICLE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>An Article About Birds</title>
</head>
<body>
  <article>
    <h1>An Article About Birds</h1>
    <p>Birds are warm-blooded vertebrates constituting the class Aves,
    characterised by feathers, toothless beaked jaws, the laying of
    hard-shelled eggs, a high metabolic rate, a four-chambered heart,
    and a strong yet lightweight skeleton.</p>
    <p>About ten thousand living species exist; more than half are
    passerines, sometimes known as perching birds. Birds have wings whose
    development varies according to species; the only known groups
    without wings are the extinct moa and elephant birds.</p>
    <p>Wings, which evolved from forelimbs, gave birds the ability to fly,
    although further evolution has led to the loss of flight in some
    birds, including ratites, penguins, and diverse endemic island
    species.</p>
  </article>
</body>
</html>"""


_NO_TITLE_HTML = """<!doctype html>
<html><body><p>Just a paragraph with no head/title at all.</p></body></html>"""


_SPA_NEXT_DATA_HTML = """<!doctype html>
<html>
<head>
<title>App</title>
<script src="/_next/static/a.js"></script>
<script src="/_next/static/b.js"></script>
<script src="/_next/static/c.js"></script>
<script src="/_next/static/d.js"></script>
<script src="/_next/static/e.js"></script>
<script src="/_next/static/f.js"></script>
<script src="/_next/static/g.js"></script>
<script src="/_next/static/h.js"></script>
<script src="/_next/static/i.js"></script>
<script src="/_next/static/j.js"></script>
</head>
<body>
<div id="__next"></div>
<script id="__NEXT_DATA__" type="application/json">{"props":{}}</script>
</body>
</html>"""


# --- FC-3 static surface ------------------------------------------------------


class TestStaticSurface:
    def test_name(self) -> None:
        assert WebFetchTool.name == "web_fetch"

    def test_description_nonempty(self) -> None:
        assert isinstance(WebFetchTool.description, str)
        assert WebFetchTool.description.strip() != ""

    def test_not_destructive(self) -> None:
        assert WebFetchTool.requires_confirmation is False

    def test_schema_shape(self) -> None:
        s = WebFetchTool.input_schema
        assert s["type"] == "object"
        assert "url" in s["properties"]
        assert s["properties"]["url"]["type"] == "string"
        assert "format" in s["properties"]
        assert s["properties"]["format"]["enum"] == ["text", "html"]
        assert s["properties"]["format"].get("default") == "text"
        assert "max_chars" in s["properties"]
        assert s["properties"]["max_chars"]["type"] == "integer"
        assert s["properties"]["max_chars"].get("default") == 50000
        assert s["properties"]["max_chars"].get("minimum") == 100
        assert s["properties"]["max_chars"].get("maximum") == 200000
        assert s["required"] == ["url"]

    def test_constructs(self) -> None:
        _make()  # default UA

    def test_constructs_with_custom_user_agent(self) -> None:
        WebFetchTool(user_agent="TesterAgent/2.0")

    def test_satisfies_tool_protocol(self) -> None:
        assert isinstance(_make(), Tool)


# --- HTML happy paths ---------------------------------------------------------


class TestHtmlContent:
    async def test_html_text_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(
                200, content=_ARTICLE_HTML.encode(), headers={"content-type": "text/html"}
            ),
        )
        result = await _make().call({"url": "https://example.com/article", "format": "text"})

        assert isinstance(result, ToolResult)
        out = result.output
        assert out["status_code"] == 200
        assert "text/html" in out["content_type"]
        assert out["title"] == "An Article About Birds"
        # Text format: extraction should not include raw HTML tags.
        assert "<article>" not in out["content"]
        assert "<p>" not in out["content"]
        # Some article text should survive extraction.
        assert "birds" in out["content"].lower()
        # Real article → not flagged as needing browser.
        assert out["may_need_browser"] is False

    async def test_html_html_format_returns_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(
                200, content=_ARTICLE_HTML.encode(), headers={"content-type": "text/html"}
            ),
        )
        result = await _make().call({"url": "https://example.com/article", "format": "html"})
        out = result.output
        assert "<article>" in out["content"]
        assert "<title>" in out["content"]
        assert "text/html" in out["content_type"]
        assert out["may_need_browser"] is False

    async def test_title_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(
                200,
                content=b"<html><head><title>Hello World</title></head><body>x</body></html>",
                headers={"content-type": "text/html"},
            ),
        )
        result = await _make().call({"url": "https://example.com/x"})
        assert result.output["title"] == "Hello World"

    async def test_title_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(
                200,
                content=_NO_TITLE_HTML.encode(),
                headers={"content-type": "text/html"},
            ),
        )
        result = await _make().call({"url": "https://example.com/x"})
        assert result.output["title"] is None


# --- Non-HTML content types ---------------------------------------------------


class TestNonHtml:
    async def test_text_plain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = "Hello plain world.\nLine two.\n"
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(
                200, content=body.encode(), headers={"content-type": "text/plain"}
            ),
        )
        result = await _make().call({"url": "https://example.com/file.txt"})
        out = result.output
        assert out["status_code"] == 200
        assert "text/plain" in out["content_type"]
        assert out["content"] == body
        assert out["title"] is None
        assert out["may_need_browser"] is False

    async def test_application_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = '{"hello": "world", "count": 3}'
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(
                200, content=body.encode(), headers={"content-type": "application/json"}
            ),
        )
        result = await _make().call({"url": "https://api.example.com/x"})
        out = result.output
        assert "application/json" in out["content_type"]
        assert out["content"] == body
        assert out["title"] is None

    async def test_format_html_ignored_for_non_html(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = '{"k": "v"}'
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(
                200, content=body.encode(), headers={"content-type": "application/json"}
            ),
        )
        result = await _make().call({"url": "https://api.example.com/x", "format": "html"})
        # Format flag is irrelevant for non-HTML; same content.
        assert result.output["content"] == body

    async def test_binary_content_raises_tool_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 1x1 PNG header is enough — implementer should reject by content-type, not magic bytes.
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(200, content=png_bytes, headers={"content-type": "image/png"}),
        )
        with pytest.raises(ToolError) as exc_info:
            await _make().call({"url": "https://example.com/img.png"})
        msg = str(exc_info.value).lower()
        assert "unsupported" in msg
        assert "content-type" in msg or "content_type" in msg or "type" in msg


# --- HTTP status handling -----------------------------------------------------


class TestHttpStatus:
    async def test_404_returns_result_no_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(
                404,
                content=b"<html><body>Not Found</body></html>",
                headers={"content-type": "text/html"},
            ),
        )
        # Spec: HTTP errors do NOT raise — they're observable via status_code.
        result = await _make().call({"url": "https://example.com/missing"})
        assert isinstance(result, ToolResult)
        assert result.output["status_code"] == 404
        # Server-supplied content surfaces somehow (raw or extracted).
        assert "Not Found" in result.output["content"] or result.output["content"] != ""
        assert result.output["may_need_browser"] is False

    async def test_500_returns_result_no_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(
                500,
                content=b"server exploded",
                headers={"content-type": "text/plain"},
            ),
        )
        # NOTE: differs from search where 5xx → ToolRetryable. Fetch surfaces
        # the status code instead so the LLM can reason about it.
        result = await _make().call({"url": "https://example.com/oops"})
        assert isinstance(result, ToolResult)
        assert result.output["status_code"] == 500
        assert "exploded" in result.output["content"]


# --- Network failures ---------------------------------------------------------


class TestNetworkFailures:
    async def test_timeout_raises_transport_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raising(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out", request=request)

        _install_mock_transport(monkeypatch, raising)
        with pytest.raises(TransportError):
            await _make().call({"url": "https://slow.example.com/"})

    async def test_connect_refused_raises_transport_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raising(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _install_mock_transport(monkeypatch, raising)
        with pytest.raises(TransportError):
            await _make().call({"url": "https://nope.example.com/"})


# --- Redirects ----------------------------------------------------------------


class TestRedirects:
    async def test_redirect_followed_final_url_recorded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two-hop chain: /old → /new → final 200.
        def handler(request: httpx.Request) -> httpx.Response:
            path = urlparse(str(request.url)).path
            if path == "/old":
                return httpx.Response(301, headers={"Location": "https://example.com/new"})
            if path == "/new":
                return httpx.Response(
                    200,
                    content=b"<html><head><title>Final</title></head><body>final</body></html>",
                    headers={"content-type": "text/html"},
                )
            return httpx.Response(404)

        _install_mock_transport(monkeypatch, handler)
        result = await _make().call({"url": "https://example.com/old"})

        assert result.output["status_code"] == 200
        # Final URL after redirects should be recorded.
        assert "/new" in result.output["url"]
        assert "/old" not in result.output["url"]


# --- Truncation ---------------------------------------------------------------


class TestTruncation:
    async def test_text_truncated_to_max_chars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = "x" * 5000
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(
                200, content=body.encode(), headers={"content-type": "text/plain"}
            ),
        )
        result = await _make().call({"url": "https://example.com/big.txt", "max_chars": 1000})
        # Allow a 50-char slack for any "...[truncated]"-style suffix.
        assert len(result.output["content"]) <= 1050
        # And it actually trimmed.
        assert len(result.output["content"]) < len(body)

    async def test_under_max_chars_not_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = "small body"
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(
                200, content=body.encode(), headers={"content-type": "text/plain"}
            ),
        )
        result = await _make().call({"url": "https://example.com/x", "max_chars": 1000})
        assert result.output["content"] == body


# --- may_need_browser heuristic ----------------------------------------------


class TestMayNeedBrowser:
    async def test_positive_spa_with_next_data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # >5 scripts + __NEXT_DATA__ + minimal body → trafilatura yields
        # very little, heuristic flips True.
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(
                200,
                content=_SPA_NEXT_DATA_HTML.encode(),
                headers={"content-type": "text/html"},
            ),
        )
        result = await _make().call({"url": "https://spa.example.com/", "format": "text"})
        assert result.output["may_need_browser"] is True

    async def test_negative_real_article(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(
                200,
                content=_ARTICLE_HTML.encode(),
                headers={"content-type": "text/html"},
            ),
        )
        result = await _make().call({"url": "https://example.com/article", "format": "text"})
        assert result.output["may_need_browser"] is False

    async def test_html_format_never_flags_browser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even on a SPA-shaped page, format=html bypasses the heuristic.
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(
                200,
                content=_SPA_NEXT_DATA_HTML.encode(),
                headers={"content-type": "text/html"},
            ),
        )
        result = await _make().call({"url": "https://spa.example.com/", "format": "html"})
        # Spec: heuristic requires format="text". format="html" → False.
        assert result.output["may_need_browser"] is False


# --- Headers ------------------------------------------------------------------


class TestHeaders:
    async def test_default_user_agent_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(
                200,
                content=b"<html><body>x</body></html>",
                headers={"content-type": "text/html"},
            ),
        )
        await _make().call({"url": "https://example.com/"})
        ua = rec.last.headers.get("user-agent", "")
        assert "Ember" in ua

    async def test_custom_user_agent_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(
                200,
                content=b"<html><body>x</body></html>",
                headers={"content-type": "text/html"},
            ),
        )
        tool = WebFetchTool(user_agent="CustomBot/9.9 (+test)")
        await tool.call({"url": "https://example.com/"})
        ua = rec.last.headers.get("user-agent", "")
        assert "CustomBot/9.9" in ua
