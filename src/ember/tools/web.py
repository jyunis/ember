"""Web tools.

- `web_search` — Brave Search API.
- `web_fetch` — plain HTTP GET + trafilatura extraction (PDFs via pdftotext).
- `http_request` — arbitrary HTTP request (any method, body, headers).

Brave key from env via Settings.brave_api_key. Tools register only when
key is set (see main.py). Future browserless integration uses the
`may_need_browser` flag on web_fetch results to suggest escalation.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import re
import tempfile
import urllib.parse
from typing import Any

import feedparser
import httpx
import trafilatura

from ember.errors import (
    AuthError,
    RateLimitError,
    ToolError,
    ToolRetryable,
    TransportError,
)
from ember.tools.base import ToolResult

_log = logging.getLogger("ember.tools.web")

_BRAVE_DEFAULT_BASE = "https://api.search.brave.com"
_FETCH_TIMEOUT_S = 10.0
_SEARCH_TIMEOUT_S = 10.0
_RSS_FETCH_TIMEOUT_S = 10.0
_HTTP_REQUEST_TIMEOUT_S = 30.0
_FETCH_MAX_BYTES = 5 * 1024 * 1024
_TRUNCATION_MARKER = "\n...[truncated]"

# Realistic desktop Chrome User-Agent. Chromium's default headless UA
# contains the literal string "HeadlessChrome", which every bot
# detector keys on. Sending a normal-looking Chrome UA through
# browserless dodges the laziest detectors. Aggressive ones
# (Akamai/Cloudflare bot detection on big retail sites like REI) still
# fingerprint at the TLS / HTTP/2 layer and reject the connection
# regardless — that's a hard problem we don't solve here.
_REALISTIC_CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

_TEXT_CONTENT_TYPES = (
    "text/html",
    "text/plain",
    "application/json",
    "application/xml",
    "text/xml",
    "text/csv",
    "text/markdown",
)

# PDFs are the one binary type worth handling: manuals, spec sheets and
# recipe PDFs are a recurring fetch target and the image ships poppler-utils
# for the claude CLI already. Text comes from `pdftotext -layout`; larger
# than this and we refuse rather than truncate (a cut PDF has no trailer).
_PDF_CONTENT_TYPES = ("application/pdf", "application/x-pdf")
_PDF_MAX_BYTES = 20 * 1024 * 1024
_PDF_TIMEOUT_S = 30.0


async def _pdf_to_text(data: bytes) -> str:
    """Extract text from PDF bytes with `pdftotext -layout` (poppler-utils).

    Raises `ToolError` when the binary is missing, times out or fails.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(data)
        path = f.name
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "pdftotext",
            "-layout",
            path,
            "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_PDF_TIMEOUT_S)
    except FileNotFoundError:
        raise ToolError(
            "web_fetch: PDF support needs pdftotext (poppler-utils), which is not installed"
        ) from None
    except asyncio.TimeoutError:
        if proc is not None:
            proc.kill()
        raise ToolError("web_fetch: pdftotext timed out") from None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if proc.returncode != 0:
        detail = err.decode("utf-8", errors="replace").strip()[:200]
        raise ToolError(f"web_fetch: pdftotext failed: {detail or proc.returncode}")
    return out.decode("utf-8", errors="replace")


_SPA_SIGNATURES = (
    "__NEXT_DATA__",
    "data-reactroot",
    "ng-app=",
    'id="__nuxt"',
    '<div id="root"></div>',
    '<div id="app"></div>',
)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


class WebSearchTool:
    """Search the web via Brave Search API.

    Errors:
        AuthError       — HTTP 401/403 (api key invalid)
        RateLimitError  — HTTP 429
        ToolRetryable   — HTTP 5xx
        ToolError       — other 4xx, malformed JSON
        TransportError  — network failure / timeout
    """

    name: str = "web_search"
    description: str = "Search the web via Brave Search API. Returns up to 20 results."
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "count": {
                "type": "integer",
                "default": 10,
                "minimum": 1,
                "maximum": 20,
                "description": "Number of results (1-20).",
            },
        },
        "required": ["query"],
    }

    def __init__(self, *, api_key: str, base_url: str = _BRAVE_DEFAULT_BASE) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    async def call(self, args: dict[str, Any]) -> ToolResult:
        query = args["query"]
        count = args.get("count", 10)
        # Defensive clamp; the registry validates input_schema upstream
        # (TLC-6) but unit tests invoke `call` directly with out-of-range
        # values.
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 10
        if count < 1:
            count = 1
        elif count > 20:
            count = 20

        url = f"{self._base_url}/res/v1/web/search"
        headers = {
            "X-Subscription-Token": self._api_key,
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=_SEARCH_TIMEOUT_S) as client:
                resp = await client.get(
                    url,
                    params={"q": query, "count": count},
                    headers=headers,
                )
        except httpx.TimeoutException as e:
            # Never include the api key (or anything else from headers) in
            # surfaced messages.
            raise TransportError(f"web_search transport timeout: {type(e).__name__}") from None
        except httpx.NetworkError as e:
            raise TransportError(f"web_search transport error: {type(e).__name__}") from None

        if resp.status_code in (401, 403):
            raise AuthError(f"web_search: auth rejected (HTTP {resp.status_code})")
        if resp.status_code == 429:
            raise RateLimitError("web_search: rate limited")
        if 500 <= resp.status_code < 600:
            raise ToolRetryable(f"web_search: server error (HTTP {resp.status_code})")
        if 400 <= resp.status_code < 500:
            raise ToolError(f"web_search: client error (HTTP {resp.status_code})")

        try:
            payload = resp.json()
        except ValueError:
            raise ToolError("web_search: malformed JSON response") from None

        web = payload.get("web") if isinstance(payload, dict) else None
        raw_results = (web or {}).get("results") or []
        results: list[dict[str, Any]] = []
        for r in raw_results:
            if not isinstance(r, dict):
                continue
            item: dict[str, Any] = {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "description": r.get("description", ""),
            }
            # Brave returns `page_age`; surface as `published_date`. Some
            # mocks may pre-name it `published_date` already — accept either.
            if "page_age" in r:
                item["published_date"] = r["page_age"]
            elif "published_date" in r:
                item["published_date"] = r["published_date"]
            results.append(item)
        return ToolResult(output={"results": results})


class WebFetchTool:
    """Fetch a URL and return readable content.

    Differs from `web_search` in error mapping: HTTP 4xx/5xx surface as
    a `ToolResult` with `status_code`, not as exceptions. Network
    failures still raise `TransportError`. Binary content raises
    `ToolError` (no point handing the model a base64 blob).
    """

    name: str = "web_fetch"
    description: str = (
        "Fetch a URL and return readable content (HTML is extracted to text; PDFs are "
        "converted with pdftotext). Flags pages that may need a real browser."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch."},
            "format": {
                "type": "string",
                "enum": ["text", "html"],
                "default": "text",
                "description": "`text` runs trafilatura extraction; `html` returns raw HTML.",
            },
            "max_chars": {
                "type": "integer",
                "default": 50000,
                "minimum": 100,
                "maximum": 200000,
                "description": "Truncate content above this length.",
            },
        },
        "required": ["url"],
    }

    def __init__(self, *, user_agent: str = "Ember/1.0 (+self-hosted)") -> None:
        self._user_agent = user_agent

    async def call(self, args: dict[str, Any]) -> ToolResult:
        url = args["url"]
        format_ = args.get("format", "text")
        try:
            max_chars = int(args.get("max_chars", 50000))
        except (TypeError, ValueError):
            max_chars = 50000

        try:
            async with httpx.AsyncClient(
                timeout=_FETCH_TIMEOUT_S,
                follow_redirects=True,
                headers={"User-Agent": self._user_agent},
            ) as client:
                resp = await client.get(url)
        except httpx.TimeoutException as e:
            raise TransportError(f"web_fetch transport timeout: {type(e).__name__}") from None
        except httpx.NetworkError as e:
            raise TransportError(f"web_fetch transport error: {type(e).__name__}") from None

        final_url = str(resp.url)
        raw_ct = resp.headers.get("content-type", "")
        content_type = raw_ct.split(";")[0].strip().lower() or "application/octet-stream"

        # PDFs: by declared type, or an octet-stream whose URL path ends in
        # .pdf (common for misconfigured static hosts).
        is_pdf = content_type in _PDF_CONTENT_TYPES or (
            content_type == "application/octet-stream"
            and urllib.parse.urlsplit(final_url).path.lower().endswith(".pdf")
        )
        if is_pdf:
            data = resp.content
            if len(data) > _PDF_MAX_BYTES:
                raise ToolError(
                    f"web_fetch: PDF too large ({len(data)} bytes; limit {_PDF_MAX_BYTES})"
                )
            content = await _pdf_to_text(data)
            if len(content) > max_chars:
                content = content[:max_chars] + _TRUNCATION_MARKER
            return ToolResult(
                output={
                    "url": final_url,
                    "status_code": resp.status_code,
                    "title": None,
                    "content": content,
                    "content_type": "application/pdf",
                    "may_need_browser": False,
                }
            )

        # Reject other binary content by content-type. The model can't
        # usefully consume a base64 blob inline.
        if not any(content_type.startswith(t) for t in _TEXT_CONTENT_TYPES):
            raise ToolError(f"web_fetch: unsupported content-type: {content_type}")

        # httpx decodes using the response charset.
        raw_content = resp.text
        # Defensive cap on pathological responses (huge pages with no
        # content-length).
        if len(raw_content) > _FETCH_MAX_BYTES:
            raw_content = raw_content[:_FETCH_MAX_BYTES]

        title: str | None = None
        may_need_browser = False
        content: str

        if content_type == "text/html":
            title = _extract_title(raw_content)
            if format_ == "text":
                extracted = trafilatura.extract(raw_content) or ""
                content = extracted
                may_need_browser = _may_need_browser(raw_content, extracted)
            else:
                content = raw_content
        else:
            content = raw_content

        if len(content) > max_chars:
            content = content[:max_chars] + _TRUNCATION_MARKER

        return ToolResult(
            output={
                "url": final_url,
                "status_code": resp.status_code,
                "title": title,
                "content": content,
                "content_type": content_type,
                "may_need_browser": may_need_browser,
            }
        )


class HttpRequestTool:
    """Make an arbitrary HTTP request.

    Sibling of `web_fetch`, but unrestricted in method and body — for
    direct API calls, debugging upstream services, and probing endpoints
    that don't have dedicated tools (e.g. an MCP server speaking SSE).

    Non-2xx responses are NOT exceptions — `status_code` carries the HTTP
    status, and `body` carries the response text. The model decides what
    to do with it. Network failures and timeouts surface as
    `TransportError`.

    Response bodies are returned as text (httpx decodes using the response
    charset; undecodable bytes fall back to UTF-8 with replacement). Body
    is truncated to `max_chars` (default 50k); a `truncated` flag in the
    output signals the cap was hit.
    """

    name: str = "http_request"
    description: str = (
        "Make an HTTP request (any method, body, headers, params). "
        "Returns status_code, response headers, and body text "
        "(truncated to max_chars). Non-2xx responses are returned as "
        "data, not errors — inspect status_code. Use this for direct "
        "API calls, MCP debugging, and probing services without a "
        "dedicated tool."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Full URL including scheme.",
            },
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                "default": "GET",
            },
            "headers": {
                "type": ["object", "null"],
                "additionalProperties": {"type": "string"},
                "default": None,
                "description": "Optional request headers.",
            },
            "params": {
                "type": ["object", "null"],
                "additionalProperties": {"type": "string"},
                "default": None,
                "description": "Optional query-string parameters.",
            },
            "json_body": {
                "description": (
                    "Optional JSON-serializable body. Mutually exclusive "
                    "with `body`. Sets Content-Type: application/json "
                    "automatically (unless overridden in `headers`)."
                ),
                "default": None,
            },
            "body": {
                "type": ["string", "null"],
                "default": None,
                "description": (
                    "Optional raw request body (string). Mutually exclusive with `json_body`."
                ),
            },
            "timeout_s": {
                "type": "number",
                "default": _HTTP_REQUEST_TIMEOUT_S,
                "minimum": 0.1,
                "maximum": 120.0,
            },
            "follow_redirects": {
                "type": "boolean",
                "default": True,
            },
            "max_chars": {
                "type": "integer",
                "default": 50000,
                "minimum": 100,
                "maximum": 500000,
                "description": "Truncate response body above this length.",
            },
        },
        "required": ["url"],
    }

    def __init__(self, *, user_agent: str = "Ember/1.0 (+self-hosted)") -> None:
        self._user_agent = user_agent

    async def call(self, args: dict[str, Any]) -> ToolResult:
        url = args["url"]
        method = str(args.get("method") or "GET").upper()
        headers = dict(args.get("headers") or {})
        params = args.get("params") or None
        json_body = args.get("json_body")
        body = args.get("body")
        try:
            timeout_s = float(args.get("timeout_s", _HTTP_REQUEST_TIMEOUT_S))
        except (TypeError, ValueError):
            timeout_s = _HTTP_REQUEST_TIMEOUT_S
        follow_redirects = bool(args.get("follow_redirects", True))
        try:
            max_chars = int(args.get("max_chars", 50000))
        except (TypeError, ValueError):
            max_chars = 50000

        if json_body is not None and body is not None:
            raise ToolError("http_request: pass either `json_body` or `body`, not both")

        if not any(k.lower() == "user-agent" for k in headers):
            headers["User-Agent"] = self._user_agent

        request_kwargs: dict[str, Any] = {
            "method": method,
            "url": url,
            "headers": headers,
        }
        if params is not None:
            request_kwargs["params"] = params
        if json_body is not None:
            request_kwargs["json"] = json_body
        elif body is not None:
            request_kwargs["content"] = body

        try:
            async with httpx.AsyncClient(
                timeout=timeout_s,
                follow_redirects=follow_redirects,
            ) as client:
                resp = await client.request(**request_kwargs)
        except httpx.TimeoutException as e:
            raise TransportError(f"http_request transport timeout: {type(e).__name__}") from None
        except httpx.NetworkError as e:
            raise TransportError(f"http_request transport error: {type(e).__name__}") from None

        try:
            text = resp.text
        except UnicodeDecodeError:
            text = resp.content.decode("utf-8", errors="replace")

        if len(text) > _FETCH_MAX_BYTES:
            text = text[:_FETCH_MAX_BYTES]

        truncated = False
        if len(text) > max_chars:
            text = text[:max_chars] + _TRUNCATION_MARKER
            truncated = True

        return ToolResult(
            output={
                "url": str(resp.url),
                "method": method,
                "status_code": resp.status_code,
                "headers": dict(resp.headers),
                "body": text,
                "truncated": truncated,
            }
        )


class WebBrowseTool:
    """Fetch a URL via headless Chrome (browserless) for JS-rendered pages.

    Use when `web_fetch` flagged `may_need_browser` on a page. Posts to
    `{base_url}/content?token=<token>` and reads the returned, fully-
    rendered HTML.

    `status_code` in the result reflects the BROWSERLESS API status (200
    on a successful render), NOT the upstream page's HTTP status.

    Errors:
        AuthError       — HTTP 401/403 (browserless token rejected)
        RateLimitError  — HTTP 429
        ToolRetryable   — HTTP 5xx (transient browserless failure)
        ToolError       — HTTP 4xx (incl. 400 navigation/timeout)
        TransportError  — network failure / timeout
    """

    name: str = "web_browse"
    description: str = (
        "Fetch a URL via headless Chrome (browserless). Use when web_fetch sets "
        "may_need_browser. status_code reflects the browserless API status (200 on render), "
        "not the upstream page."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch."},
            "format": {
                "type": "string",
                "enum": ["text", "html"],
                "default": "text",
                "description": "`text` runs trafilatura extraction; `html` returns rendered HTML.",
            },
            "wait_for": {
                "type": ["string", "null"],
                "description": "Optional CSS selector to wait for before reading.",
            },
            "wait_ms": {
                "type": "integer",
                "default": 2000,
                "minimum": 0,
                "maximum": 30000,
                "description": (
                    "Post-load idle wait (ms). Applied AFTER navigation "
                    "completes — gives JS a chance to settle before the "
                    "HTML is read."
                ),
            },
            "nav_timeout_ms": {
                "type": "integer",
                "default": 30000,
                "minimum": 1000,
                "maximum": 90000,
                "description": (
                    "Navigation timeout (ms) — max time to reach the "
                    "page load event. Defaults to 30s; raise for slow "
                    "sites. Distinct from wait_ms, which is the "
                    "post-load idle delay."
                ),
            },
            "max_chars": {
                "type": "integer",
                "default": 50000,
                "minimum": 100,
                "maximum": 200000,
                "description": "Truncate content above this length.",
            },
        },
        "required": ["url"],
    }

    def __init__(self, *, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token

    async def call(self, args: dict[str, Any]) -> ToolResult:
        url = args["url"]
        format_ = args.get("format", "text")
        wait_for = args.get("wait_for")
        try:
            wait_ms = int(args.get("wait_ms", 2000))
        except (TypeError, ValueError):
            wait_ms = 2000
        try:
            nav_timeout_ms = int(args.get("nav_timeout_ms", 30000))
        except (TypeError, ValueError):
            nav_timeout_ms = 30000
        try:
            max_chars = int(args.get("max_chars", 50000))
        except (TypeError, ValueError):
            max_chars = 50000

        body: dict[str, Any] = {
            "url": url,
            # `gotoOptions.timeout` is the navigation budget (time to
            # reach the load event). `waitForTimeout` is the post-load
            # idle delay. They are distinct concepts and must NOT share
            # a value — early versions did, which made the default 2s
            # nav budget too tight for any real SPA and surfaced as 500s.
            "gotoOptions": {"timeout": nav_timeout_ms},
            "waitForTimeout": wait_ms,
            # Mask the headless-Chrome fingerprint at the UA level. See
            # `_REALISTIC_CHROME_UA` for what this does and doesn't fix.
            # Browserless 2.54.x tightened the schema: `userAgent` must be
            # an object — a bare string 400s with `"userAgent" must be
            # object`. The nested `{"userAgent": <ua>}` shape sets the UA
            # (verified: the string reaches the page's navigator.userAgent)
            # and also parses on older 2.x, so it survives the next image
            # bump. Do NOT flatten this back to a plain string. Dropping the
            # field entirely also clears the 400 but re-exposes the
            # HeadlessChrome fingerprint this constant exists to hide.
            "userAgent": {"userAgent": _REALISTIC_CHROME_UA},
            # Return whatever the page managed to render even if goto()
            # raises (e.g. a transient HTTP/2 hiccup, an aborted
            # subresource). Without this browserless 500s on any nav
            # error and the model gets nothing back to work with.
            "bestAttempt": True,
        }
        if wait_for:
            # Browserless v2 wants this as an object, not a bare string.
            # Selector wait shares the navigation budget — it's the same
            # "give up if the page isn't ready" semantic.
            body["waitForSelector"] = {"selector": wait_for, "timeout": nav_timeout_ms}

        # The httpx client timeout must comfortably exceed the worst
        # browserless can take (nav_timeout + wait_ms + render overhead),
        # otherwise we time out before browserless can return its 500.
        client_timeout_s = (nav_timeout_ms + wait_ms) / 1000 + 30.0

        endpoint = f"{self._base_url}/content"
        try:
            async with httpx.AsyncClient(timeout=client_timeout_s) as client:
                resp = await client.post(
                    endpoint,
                    params={"token": self._token},
                    headers={"Content-Type": "application/json"},
                    json=body,
                )
        except httpx.TimeoutException as e:
            raise TransportError(f"web_browse transport timeout: {type(e).__name__}") from None
        except httpx.NetworkError as e:
            raise TransportError(f"web_browse transport error: {type(e).__name__}") from None

        # Status mapping. Order matters: auth/rate-limit before generic 4xx.
        if resp.status_code in (401, 403):
            raise AuthError(f"web_browse: auth rejected (HTTP {resp.status_code})")
        if resp.status_code == 429:
            raise RateLimitError("web_browse: rate limited")
        if 500 <= resp.status_code < 600:
            raise ToolRetryable(f"web_browse: browserless server error (HTTP {resp.status_code})")
        if 400 <= resp.status_code < 500:
            # Surface browserless's response body. A 4xx here is often a
            # request-schema rejection (a field shape browserless tightened
            # across an image bump), not an actual page failure — without
            # the body the two are indistinguishable and schema drift costs
            # a curl archaeology session to diagnose. The token rides in the
            # query string, never the response body, so this can't leak it.
            detail = resp.text[:200].strip()
            raise ToolError(f"web_browse: page load failed (HTTP {resp.status_code}): {detail}")

        rendered_html = resp.text
        title = _extract_title(rendered_html)

        if format_ == "text":
            content = trafilatura.extract(rendered_html) or ""
        else:
            content = rendered_html

        if len(content) > max_chars:
            content = content[:max_chars] + _TRUNCATION_MARKER

        return ToolResult(
            output={
                "url": url,
                "status_code": 200,
                "title": title,
                "content": content,
                "content_type": "text/html",
                "may_need_browser": False,
            }
        )


class RssFetchTool:
    """Fetch and parse one or more RSS / Atom feeds in parallel.

    Per-feed errors are isolated — a failed feed produces an entry with
    `ok=False, error=<short reason>, items=[]`; the tool itself never
    raises on individual fetch failures. The returned `feeds` list
    matches the order of the input `urls`.
    """

    name: str = "rss_fetch"
    description: str = (
        "Fetch and parse one or more RSS/Atom feeds in parallel. "
        "Returns structured items per feed; per-feed errors are isolated."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 30,
                "description": "Feed URLs to fetch (1-30). Fetched in parallel.",
            },
            "limit_per_feed": {
                "type": "integer",
                "default": 10,
                "minimum": 1,
                "maximum": 50,
                "description": "Max items returned per feed.",
            },
            "description_max_chars": {
                "type": "integer",
                "default": 280,
                "minimum": 0,
                "maximum": 4000,
                "description": "Per-item description truncation cap. 0 omits descriptions entirely.",
            },
        },
        "required": ["urls"],
    }

    def __init__(self, *, user_agent: str = "Ember/1.0 (+self-hosted)") -> None:
        self._user_agent = user_agent

    async def call(self, args: dict[str, Any]) -> ToolResult:
        urls: list[str] = list(args["urls"])
        try:
            limit = int(args.get("limit_per_feed", 10))
        except (TypeError, ValueError):
            limit = 10
        if limit < 1:
            limit = 1
        elif limit > 50:
            limit = 50

        try:
            desc_max = int(args.get("description_max_chars", 280))
        except (TypeError, ValueError):
            desc_max = 280
        if desc_max < 0:
            desc_max = 0
        elif desc_max > 4000:
            desc_max = 4000

        async with httpx.AsyncClient(
            timeout=_RSS_FETCH_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": self._user_agent},
        ) as client:
            coros = [self._fetch_one(client, url, limit, desc_max) for url in urls]
            feeds = await asyncio.gather(*coros)

        return ToolResult(output={"feeds": list(feeds)})

    async def _fetch_one(
        self,
        client: httpx.AsyncClient,
        url: str,
        limit: int,
        desc_max: int,
    ) -> dict[str, Any]:
        try:
            resp = await client.get(url)
        except httpx.TimeoutException as e:
            return {
                "url": url,
                "source": None,
                "ok": False,
                "error": _network_error_label(e),
                "items": [],
            }
        except httpx.NetworkError as e:
            return {
                "url": url,
                "source": None,
                "ok": False,
                "error": _network_error_label(e),
                "items": [],
            }
        except Exception as e:  # pragma: no cover - defensive isolation
            return {
                "url": url,
                "source": None,
                "ok": False,
                "error": _network_error_label(e),
                "items": [],
            }

        if resp.status_code >= 400:
            return {
                "url": url,
                "source": None,
                "ok": False,
                "error": f"http_{resp.status_code}",
                "items": [],
            }

        parsed = feedparser.parse(resp.content)
        bozo = bool(getattr(parsed, "bozo", False))
        entries = list(getattr(parsed, "entries", []) or [])

        if bozo and not entries:
            return {
                "url": url,
                "source": None,
                "ok": False,
                "error": "parse_error",
                "items": [],
            }

        feed_meta = getattr(parsed, "feed", None)
        title = None
        if feed_meta is not None:
            try:
                title = feed_meta.get("title")
            except Exception:
                title = None
        source = (
            title.strip() if isinstance(title, str) and title.strip() else None
        ) or _hostname_from_url(url)

        items: list[dict[str, Any]] = []
        for entry in entries[:limit]:
            published = _struct_time_to_iso(
                getattr(entry, "published_parsed", None)
            ) or _struct_time_to_iso(getattr(entry, "updated_parsed", None))

            summary_raw = entry.get("summary") if hasattr(entry, "get") else None
            description: str | None
            if summary_raw and desc_max > 0:
                stripped = _strip_html(summary_raw)
                if stripped:
                    if len(stripped) > desc_max:
                        stripped = stripped[: desc_max - 1].rstrip() + "…"
                    description = stripped
                else:
                    description = None
            else:
                description = None

            items.append(
                {
                    "title": (entry.get("title", "") if hasattr(entry, "get") else "") or "",
                    "link": (entry.get("link", "") if hasattr(entry, "get") else "") or "",
                    "description": description,
                    "published": published,
                }
            )

        return {
            "url": url,
            "source": source,
            "ok": True,
            "items": items,
        }


def _strip_html(text: str) -> str:
    if not text:
        return ""
    no_tags = _HTML_TAG_RE.sub("", text)
    return _WHITESPACE_RE.sub(" ", no_tags).strip()


def _struct_time_to_iso(t: Any) -> str | None:
    if t is None:
        return None
    try:
        dt = datetime.datetime(*t[:6], tzinfo=datetime.timezone.utc)
        return dt.isoformat()
    except (TypeError, ValueError):
        return None


def _hostname_from_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.hostname or url
    except Exception:
        return url


def _network_error_label(exc: BaseException) -> str:
    """Short error label for per-feed network failures.

    httpx.ConnectTimeout → "connecttimeout" — contains "timeout" so the
    test's `'time' in err or 'timeout' in err` substring check passes.
    httpx.ConnectError → "connecterror".
    """
    name = type(exc).__name__.lower()
    return name or "network_error"


def _extract_title(html: str) -> str | None:
    m = _TITLE_RE.search(html)
    if not m:
        return None
    title = m.group(1).strip()
    return title or None


def _may_need_browser(html: str, extracted: str) -> bool:
    """SPA-detection heuristic. Per spec, requires ALL of:
    - format == "text" (caller already gated)
    - extracted length < 200
    - >5 <script> tags OR a known SPA signature.
    """
    if len(extracted) >= 200:
        return False
    script_count = html.lower().count("<script")
    if script_count > 5:
        return True
    return any(sig in html for sig in _SPA_SIGNATURES)
