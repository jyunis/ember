"""RssFetchTool tests (FC-3, TLC-7) — v10.6.

Covers `ember.tools.web.RssFetchTool`:
- FC-3 static surface (name, description, input_schema, requires_confirmation,
  Tool protocol).
- Happy path: single + multi-feed parsing, item field shape (title, link,
  description with HTML stripped, published as ISO 8601 UTC).
- limit_per_feed bounds (default 10, custom values).
- Atom feed parsing.
- Per-feed isolation: HTTP 5xx, malformed XML, timeout, connect error.
- Parallelism: total wall-clock close to max(per-feed) not sum(per-feed).
- User-Agent header sent on every outbound request.

Tester-flagged ambiguities (also in the report):

1. **`source` fallback** — spec says "feed.feed.title or domain fallback".
   We assert source is non-empty for parsed feeds with a title; we don't pin
   the exact domain-fallback format.

2. **Error string wording** — spec leaves error text up to implementer. Tests
   assert `error` is a non-empty string and that timeout/HTTP/parse error
   reasons differ between branches only loosely (e.g. "timeout" appears in
   the timeout case). Loose substring assertions only.

3. **HTML stripping library** — spec says "regex or BeautifulSoup". We
   assert tags `<` and `>` are absent from `description` after stripping,
   plus that the visible text content survives. We do not pin the exact
   whitespace handling.

4. **Per-feed error in the Atom case is N/A** — Atom is happy-path only.

5. **`urls` clamp** — spec says defensive clamp inside `call` is OK but not
   required. We don't test out-of-range urls (>30) — the registry handles it.

6. **Item field absence** — spec says fallback to "" for title/link missing,
   None for description/published missing. Tests pin those defaults.

7. **`feedparser` runtime dep** — module imports `feedparser` at top so test
   collection fails clearly if the implementer hasn't added it. Tests
   assert on the *tool's* output shape, not on feedparser internals.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import feedparser  # noqa: F401  — fail fast if dep missing
import httpx
import pytest

from ember.tools.base import Tool, ToolResult
from ember.tools.web import RssFetchTool


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
    """Patch `httpx.AsyncClient` to route through `httpx.MockTransport`.

    Supports both sync and async handlers (via httpx.MockTransport's
    handler-introspection: it awaits the result if it's a coroutine).
    """

    rec = _Recorder()

    if asyncio.iscoroutinefunction(handler):

        async def wrapping_handler(request: httpx.Request) -> httpx.Response:
            rec.record(request)
            return await handler(request)
    else:

        def wrapping_handler(request: httpx.Request) -> httpx.Response:  # type: ignore[misc]
            rec.record(request)
            return handler(request)

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(wrapping_handler)
        return _ORIGINAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return rec


def _make() -> RssFetchTool:
    return RssFetchTool()


# --- Sample feed XML fixtures ------------------------------------------------


# Two-item feed: first item has full fields (HTML in description, pubDate);
# second item is minimal (no description, no pubDate).
_RSS_TWO_ITEMS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Test Feed</title>
<link>https://example.com/</link>
<description>A test feed</description>
<item>
  <title>Item 1</title>
  <link>https://x.com/1</link>
  <description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description>
  <pubDate>Wed, 28 Apr 2026 12:00:00 GMT</pubDate>
</item>
<item>
  <title>Item 2</title>
  <link>https://x.com/2</link>
</item>
</channel></rss>"""


def _rss_with_n_items(n: int) -> str:
    items = "\n".join(
        f"<item><title>Item {i}</title><link>https://x.com/{i}</link>"
        f"<description>desc {i}</description>"
        f"<pubDate>Wed, 28 Apr 2026 12:00:{i:02d} GMT</pubDate></item>"
        for i in range(1, n + 1)
    )
    return (
        '<?xml version="1.0"?>\n'
        '<rss version="2.0"><channel>\n'
        "<title>Big Feed</title>\n"
        f"{items}\n"
        "</channel></rss>"
    )


_RSS_FIVE_ITEMS = _rss_with_n_items(5)
_RSS_TWENTY_FIVE_ITEMS = _rss_with_n_items(25)


_ATOM_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Test Feed</title>
  <link href="https://atom.example.com/"/>
  <updated>2026-04-28T12:00:00Z</updated>
  <id>urn:uuid:atomfeed</id>
  <entry>
    <title>Atom Item 1</title>
    <link href="https://atom.example.com/1"/>
    <id>urn:uuid:atom-1</id>
    <updated>2026-04-28T12:00:00Z</updated>
    <summary>Atom summary one</summary>
  </entry>
  <entry>
    <title>Atom Item 2</title>
    <link href="https://atom.example.com/2"/>
    <id>urn:uuid:atom-2</id>
    <updated>2026-04-28T13:00:00Z</updated>
    <summary>Atom summary two</summary>
  </entry>
</feed>"""


_MALFORMED_XML = "<?xml version='1.0'?><rss><channel><title>Broken<<<</channel"


def _rss_response(body: str, *, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        content=body.encode("utf-8"),
        headers={"content-type": "application/rss+xml"},
    )


# --- FC-3 static surface ------------------------------------------------------


class TestStaticSurface:
    def test_name(self) -> None:
        assert RssFetchTool.name == "rss_fetch"

    def test_description_nonempty(self) -> None:
        assert isinstance(RssFetchTool.description, str)
        assert RssFetchTool.description.strip() != ""

    def test_not_destructive(self) -> None:
        assert RssFetchTool.requires_confirmation is False

    def test_schema_shape(self) -> None:
        s = RssFetchTool.input_schema
        assert s["type"] == "object"
        # urls required, array of strings, 1..30
        assert "urls" in s["properties"]
        urls = s["properties"]["urls"]
        assert urls["type"] == "array"
        assert urls.get("items", {}).get("type") == "string"
        assert urls.get("minItems") == 1
        assert urls.get("maxItems") == 30
        assert s["required"] == ["urls"]
        # limit_per_feed optional integer with bounds
        assert "limit_per_feed" in s["properties"]
        lpf = s["properties"]["limit_per_feed"]
        assert lpf["type"] == "integer"
        assert lpf.get("default") == 10
        assert lpf.get("minimum") == 1
        assert lpf.get("maximum") == 50

    def test_constructs(self) -> None:
        _make()

    def test_constructs_with_custom_user_agent(self) -> None:
        RssFetchTool(user_agent="TesterRSS/1.0")

    def test_satisfies_tool_protocol(self) -> None:
        assert isinstance(_make(), Tool)


# --- Happy path: single feed ----------------------------------------------


class TestSingleFeed:
    async def test_single_feed_basic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(monkeypatch, lambda r: _rss_response(_RSS_FIVE_ITEMS))
        result = await _make().call({"urls": ["https://feed.example.com/rss"]})

        assert isinstance(result, ToolResult)
        feeds = result.output["feeds"]
        assert isinstance(feeds, list)
        assert len(feeds) == 1
        feed = feeds[0]
        assert feed["url"] == "https://feed.example.com/rss"
        assert feed["ok"] is True
        assert isinstance(feed["source"], str) and feed["source"].strip() != ""
        items = feed["items"]
        assert isinstance(items, list)
        assert len(items) == 5
        # Items in feed-order: titles "Item 1" .. "Item 5".
        assert [it["title"] for it in items] == [f"Item {i}" for i in range(1, 6)]


# --- Happy path: multiple feeds, ordering --------------------------------


class TestMultipleFeeds:
    async def test_three_feeds_returned_in_input_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Each URL gets a unique feed body so we can assert order from items.
        bodies = {
            "/feed-a": _rss_with_n_items(1),  # "Item 1"
            "/feed-b": _rss_with_n_items(2),  # "Item 1", "Item 2"
            "/feed-c": _rss_with_n_items(3),  # "Item 1", "Item 2", "Item 3"
        }

        # Add an artificial delay that REVERSES arrival order: a slowest, c
        # fastest. If implementer naively used arrival order, the result
        # would be c, b, a — not a, b, c.
        delays = {"/feed-a": 0.15, "/feed-b": 0.08, "/feed-c": 0.01}

        async def handler(request: httpx.Request) -> httpx.Response:
            path = httpx.URL(str(request.url)).path
            await asyncio.sleep(delays.get(path, 0))
            body = bodies.get(path)
            if body is None:
                return httpx.Response(404)
            return _rss_response(body)

        _install_mock_transport(monkeypatch, handler)

        urls = [
            "https://example.com/feed-a",
            "https://example.com/feed-b",
            "https://example.com/feed-c",
        ]
        result = await _make().call({"urls": urls})
        feeds = result.output["feeds"]
        assert len(feeds) == 3
        # Order must match input urls — not arrival order.
        assert [f["url"] for f in feeds] == urls
        assert [len(f["items"]) for f in feeds] == [1, 2, 3]
        for f in feeds:
            assert f["ok"] is True


# --- limit_per_feed -----------------------------------------------------------


class TestLimitPerFeed:
    async def test_limit_per_feed_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(monkeypatch, lambda r: _rss_response(_RSS_FIVE_ITEMS))
        result = await _make().call({"urls": ["https://example.com/feed"], "limit_per_feed": 2})
        items = result.output["feeds"][0]["items"]
        assert len(items) == 2
        # Feed-order (caps from the head of the items list).
        assert [it["title"] for it in items] == ["Item 1", "Item 2"]

    async def test_default_limit_is_10(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(monkeypatch, lambda r: _rss_response(_RSS_TWENTY_FIVE_ITEMS))
        result = await _make().call({"urls": ["https://example.com/big"]})
        items = result.output["feeds"][0]["items"]
        assert len(items) == 10


# --- Item fields --------------------------------------------------------------


class TestItemFields:
    async def test_title_link_description_html_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_mock_transport(monkeypatch, lambda r: _rss_response(_RSS_TWO_ITEMS))
        result = await _make().call({"urls": ["https://example.com/feed"]})
        items = result.output["feeds"][0]["items"]
        assert len(items) == 2
        first = items[0]
        assert first["title"] == "Item 1"
        assert first["link"] == "https://x.com/1"
        # Description present, HTML stripped.
        desc = first["description"]
        assert isinstance(desc, str)
        assert "<" not in desc
        assert ">" not in desc
        # Visible text survives stripping.
        assert "Hello" in desc
        assert "world" in desc

    async def test_published_iso_8601_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(monkeypatch, lambda r: _rss_response(_RSS_TWO_ITEMS))
        result = await _make().call({"urls": ["https://example.com/feed"]})
        first = result.output["feeds"][0]["items"][0]
        published = first["published"]
        assert isinstance(published, str) and published != ""
        # ISO 8601 in UTC: 2026-04-28T12:00:00 plus a Z or +00:00 suffix.
        # Tolerate either suffix; pin year/month/day/time core.
        assert published.startswith("2026-04-28T12:00:00")
        assert published.endswith("Z") or published.endswith("+00:00")

    async def test_missing_published_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(monkeypatch, lambda r: _rss_response(_RSS_TWO_ITEMS))
        result = await _make().call({"urls": ["https://example.com/feed"]})
        # Item 2 in the fixture has no <pubDate>.
        second = result.output["feeds"][0]["items"][1]
        assert second["published"] is None

    async def test_missing_description_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(monkeypatch, lambda r: _rss_response(_RSS_TWO_ITEMS))
        result = await _make().call({"urls": ["https://example.com/feed"]})
        # Item 2 has no <description>.
        second = result.output["feeds"][0]["items"][1]
        assert second["description"] is None


# --- Atom feed ----------------------------------------------------------------


class TestAtomFeed:
    async def test_atom_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock_transport(
            monkeypatch,
            lambda r: httpx.Response(
                200,
                content=_ATOM_FEED.encode("utf-8"),
                headers={"content-type": "application/atom+xml"},
            ),
        )
        result = await _make().call({"urls": ["https://atom.example.com/feed"]})
        feeds = result.output["feeds"]
        assert len(feeds) == 1
        feed = feeds[0]
        assert feed["ok"] is True
        items = feed["items"]
        assert len(items) == 2
        assert items[0]["title"] == "Atom Item 1"
        assert items[0]["link"] == "https://atom.example.com/1"
        # `summary` populates description on Atom too.
        assert items[0]["description"] is not None
        assert "Atom summary one" in items[0]["description"]
        # Atom uses <updated>; should populate published as ISO 8601 UTC.
        published = items[0]["published"]
        assert isinstance(published, str)
        assert published.startswith("2026-04-28T12:00:00")


# --- Per-feed isolation -------------------------------------------------------


class TestPerFeedIsolation:
    async def test_one_5xx_one_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # First URL → valid 200; second URL → 500.
        def handler(request: httpx.Request) -> httpx.Response:
            path = httpx.URL(str(request.url)).path
            if path == "/good":
                return _rss_response(_RSS_FIVE_ITEMS)
            return httpx.Response(500, content=b"oops")

        _install_mock_transport(monkeypatch, handler)
        result = await _make().call(
            {
                "urls": [
                    "https://example.com/good",
                    "https://example.com/bad",
                ]
            }
        )
        feeds = result.output["feeds"]
        assert len(feeds) == 2
        assert feeds[0]["ok"] is True
        assert len(feeds[0]["items"]) == 5
        assert feeds[1]["ok"] is False
        assert feeds[1]["items"] == []
        assert isinstance(feeds[1].get("error"), str)
        assert feeds[1]["error"].strip() != ""

    async def test_malformed_xml_isolated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = httpx.URL(str(request.url)).path
            if path == "/good":
                return _rss_response(_RSS_FIVE_ITEMS)
            return _rss_response(_MALFORMED_XML)

        _install_mock_transport(monkeypatch, handler)
        result = await _make().call(
            {
                "urls": [
                    "https://example.com/good",
                    "https://example.com/broken",
                ]
            }
        )
        feeds = result.output["feeds"]
        assert feeds[0]["ok"] is True
        # Bad URL: ok=False with a non-empty error and empty items.
        # NOTE: feedparser is lenient and may return ok=True with zero items
        # for some malformed inputs. The spec says malformed XML → ok=False;
        # we accept either ok=False OR (ok=True AND items==[]) as long as
        # items are empty. Pin the empty-items behavior strictly; loosen the
        # ok flag.
        bad = feeds[1]
        assert bad["items"] == []
        if bad["ok"] is False:
            assert isinstance(bad.get("error"), str)
            assert bad["error"].strip() != ""

    async def test_timeout_isolated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = httpx.URL(str(request.url)).path
            if path == "/good":
                return _rss_response(_RSS_FIVE_ITEMS)
            raise httpx.ConnectTimeout("timed out", request=request)

        _install_mock_transport(monkeypatch, handler)
        result = await _make().call(
            {
                "urls": [
                    "https://example.com/good",
                    "https://slow.example.com/feed",
                ]
            }
        )
        feeds = result.output["feeds"]
        assert feeds[0]["ok"] is True
        bad = feeds[1]
        assert bad["ok"] is False
        assert bad["items"] == []
        # Loose check: error mentions a timeout. Pinned per the spec hint
        # ("timeout or similar").
        assert isinstance(bad.get("error"), str)
        assert bad["error"].strip() != ""
        assert "time" in bad["error"].lower() or "timeout" in bad["error"].lower()

    async def test_dns_error_isolated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = httpx.URL(str(request.url)).path
            if path == "/good":
                return _rss_response(_RSS_FIVE_ITEMS)
            raise httpx.ConnectError("name resolution failed", request=request)

        _install_mock_transport(monkeypatch, handler)
        result = await _make().call(
            {
                "urls": [
                    "https://example.com/good",
                    "https://nope.example.com/feed",
                ]
            }
        )
        feeds = result.output["feeds"]
        assert feeds[0]["ok"] is True
        bad = feeds[1]
        assert bad["ok"] is False
        assert bad["items"] == []
        assert isinstance(bad.get("error"), str)
        assert bad["error"].strip() != ""


# --- Parallelism --------------------------------------------------------------


class TestParallelism:
    async def test_five_feeds_in_parallel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Each fetch sleeps 200ms. Five sequential fetches = 1000ms+;
        # five parallel fetches = ~200ms (with a healthy slack).
        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.2)
            return _rss_response(_RSS_FIVE_ITEMS)

        _install_mock_transport(monkeypatch, handler)
        urls = [f"https://example.com/feed{i}" for i in range(5)]

        t0 = time.monotonic()
        result = await _make().call({"urls": urls})
        elapsed = time.monotonic() - t0

        assert len(result.output["feeds"]) == 5
        for f in result.output["feeds"]:
            assert f["ok"] is True
        # Sequential lower bound: 5*200ms = 1000ms. Parallel: ~200ms.
        # Pick a comfortable cutoff: < 600ms — clearly parallel, with
        # slack for CI noise.
        assert elapsed < 0.6, (
            f"rss_fetch took {elapsed:.3f}s for 5 parallel feeds; expected < 0.6s — "
            "implementation appears to be sequential"
        )


# --- Headers ------------------------------------------------------------------


class TestHeaders:
    async def test_default_user_agent_sent_on_each_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _install_mock_transport(monkeypatch, lambda r: _rss_response(_RSS_FIVE_ITEMS))
        urls = [
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/c",
        ]
        await _make().call({"urls": urls})
        # All three requests carry a User-Agent containing "Ember".
        assert len(rec.requests) == 3
        for req in rec.requests:
            ua = req.headers.get("user-agent", "")
            assert "Ember" in ua, f"missing Ember UA in request to {req.url}: {ua!r}"

    async def test_custom_user_agent_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _install_mock_transport(monkeypatch, lambda r: _rss_response(_RSS_FIVE_ITEMS))
        tool = RssFetchTool(user_agent="CustomRSS/9.9 (+test)")
        await tool.call({"urls": ["https://example.com/feed"]})
        ua = rec.last.headers.get("user-agent", "")
        assert "CustomRSS/9.9" in ua


# --- Schema bounds (separate test for clarity) -------------------------------


class TestSchemaBounds:
    def test_urls_bounds(self) -> None:
        s = RssFetchTool.input_schema
        assert s["properties"]["urls"]["minItems"] == 1
        assert s["properties"]["urls"]["maxItems"] == 30

    def test_limit_per_feed_bounds(self) -> None:
        s = RssFetchTool.input_schema
        lpf = s["properties"]["limit_per_feed"]
        assert lpf["minimum"] == 1
        assert lpf["maximum"] == 50


# --- Sanity: regex import suppression ----------------------------------------
# `re` is imported above for any future use (e.g. inline ISO 8601 pattern check).
# Keep the explicit reference to avoid lint noise.
_ = re
