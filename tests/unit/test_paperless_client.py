"""PaperlessClient (`records/paperless.py`, FC-34) — unit, httpx stubbed.

Properties under test:

- Every request carries `Authorization: Token <t>` and the pinned API
  version header `Accept: application/json; version=10`.
- `list_documents` passes `ordering=modified`, `modified__gt` (only when
  given), `fields` joined by comma, `page_size`, and follows `next`.
- `list_document_ids` raises `PaperlessEnumerationError` when a page
  fetch fails midway (HTTP error or connect error) — never a partial list.
- `get_task` parses `{"results": [{status, related_document, result}]}`.
- `post_document` sends multipart with a `document` file field plus the
  optional title/created/tags and returns the uuid string (paperless
  answers with a bare JSON string).
- `ensure_tags` GETs existing tags and POSTs only the missing names.
- `name_maps` builds the three id→name tables.
- Error ladder: `httpx.ConnectError` → `ToolRetryable`; 401 → `ToolError`
  naming `PAPERLESS_API_TOKEN`; 500 → `ToolError`.

Documented choices:
- The stub accepts query parameters either in the URL string or in the
  `params=` kwarg (merged); headers either per call or on the
  `AsyncClient` constructor (mealie precedent).
- `get_task` message: paperless 3.x puts the human text in `result`;
  a `message` key is accepted as an alias. Both are asserted.
- Multipart tags may be sent as a list under `data["tags"]` or as a
  scalar; the assertion normalizes to a list of ints.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from ember.errors import PaperlessEnumerationError, ToolError, ToolRetryable
from ember.records.paperless import NameMaps, PaperlessClient, TaskStatus

BASE_URL = "http://paperless:8000"
TOKEN = "t"


# --- Stub httpx ---------------------------------------------------------------


class _StubResponse:
    def __init__(self, *, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers: dict[str, str] = {"content-type": "application/json"}

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://stub/")
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )


Handler = Callable[[str, str, dict[str, Any]], "_StubResponse | None"]


class _Call:
    def __init__(self, method: str, url: str, kwargs: dict[str, Any], client: "_StubClient"):
        self.method = method
        self.url = url
        self.kwargs = kwargs
        self.client = client

    @property
    def query(self) -> dict[str, str]:
        """Query parameters from the URL merged with `params=`."""
        out: dict[str, str] = {k: v for k, v in parse_qsl(urlsplit(self.url).query)}
        params = self.kwargs.get("params") or {}
        for k, v in dict(params).items():
            out[str(k)] = ",".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)
        return out

    @property
    def path(self) -> str:
        return urlsplit(self.url).path

    def header(self, name: str) -> str | None:
        for src in (self.kwargs.get("headers") or {}, self.client.init_kwargs.get("headers") or {}):
            for k, v in dict(src).items():
                if k.lower() == name.lower():
                    return v
        return None


class _StubClient:
    handlers: list[Handler] = []
    calls: list[_Call] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.init_kwargs = kwargs

    async def __aenter__(self) -> "_StubClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def aclose(self) -> None:
        return None

    @classmethod
    def reset(cls) -> None:
        cls.handlers = []
        cls.calls = []

    @classmethod
    def on(cls, handler: Handler) -> None:
        cls.handlers.append(handler)

    def _dispatch(self, method: str, url: str, kwargs: dict[str, Any]) -> _StubResponse:
        call = _Call(method, url, kwargs, self)
        type(self).calls.append(call)
        for h in type(self).handlers:
            resp = h(method, url, kwargs)
            if resp is not None:
                return resp
        return _StubResponse(status_code=404, payload={"detail": "not found"})

    async def get(self, url: str, **kwargs: Any) -> _StubResponse:
        return self._dispatch("GET", url, kwargs)

    async def post(self, url: str, **kwargs: Any) -> _StubResponse:
        return self._dispatch("POST", url, kwargs)

    async def request(self, method: str, url: str, **kwargs: Any) -> _StubResponse:
        return self._dispatch(method.upper(), url, kwargs)


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> type[_StubClient]:
    _StubClient.reset()
    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
    return _StubClient


@pytest.fixture
def client() -> PaperlessClient:
    return PaperlessClient(base_url=BASE_URL, token=TOKEN)


def _query(url: str, kwargs: dict[str, Any]) -> dict[str, str]:
    return _Call("GET", url, kwargs, _StubClient()).query


def _page(results: list[dict[str, Any]], *, next_url: str | None, count: int | None = None) -> dict:
    return {
        "count": count if count is not None else len(results),
        "next": next_url,
        "previous": None,
        "results": results,
    }


def _connect_error(method: str, url: str, kwargs: dict[str, Any]) -> _StubResponse | None:
    raise httpx.ConnectError("boom", request=httpx.Request(method, url))


# --- Headers -------------------------------------------------------------------


class TestHeaders:
    async def test_every_request_has_token_and_version_headers(self, stub, client) -> None:
        stub.on(
            lambda m, u, k: (
                _StubResponse(payload={"id": 1, "title": "x"}) if "/api/documents/1/" in u else None
            )
        )
        await client.get_document(1)
        assert stub.calls, "no HTTP call recorded"
        for call in stub.calls:
            assert call.header("Authorization") == f"Token {TOKEN}"
            assert call.header("Accept") == "application/json; version=10"


# --- list_documents ------------------------------------------------------------


class TestListDocuments:
    async def test_params_and_pagination(self, stub, client) -> None:
        page2_url = f"{BASE_URL}/api/documents/?page=2&ordering=modified&page_size=2"

        def handler(m, u, k):
            if "/api/documents/" not in u or m != "GET":
                return None
            if _query(u, k).get("page") == "2":
                return _StubResponse(
                    payload=_page([{"id": 3, "title": "c"}], next_url=None, count=3)
                )
            return _StubResponse(
                payload=_page(
                    [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}], next_url=page2_url, count=3
                )
            )

        stub.on(handler)
        rows = [
            d
            async for d in client.list_documents(
                modified_after="2026-08-01T00:00:00Z", fields=["id", "title"], page_size=2
            )
        ]
        assert [d["id"] for d in rows] == [1, 2, 3]

        first = stub.calls[0]
        q = first.query
        assert q["ordering"] == "modified"
        assert q["modified__gt"] == "2026-08-01T00:00:00Z"
        assert q["fields"] == "id,title"
        assert q["page_size"] == "2"
        # Two page fetches, the second following `next`.
        doc_calls = [c for c in stub.calls if "/api/documents/" in c.url]
        assert len(doc_calls) == 2
        assert doc_calls[1].query.get("page") == "2"

    async def test_no_modified_gt_when_cursor_is_none(self, stub, client) -> None:
        stub.on(
            lambda m, u, k: (
                _StubResponse(payload=_page([], next_url=None)) if "/api/documents/" in u else None
            )
        )
        rows = [d async for d in client.list_documents(modified_after=None, fields=["id"])]
        assert rows == []
        assert "modified__gt" not in stub.calls[0].query
        assert stub.calls[0].query["ordering"] == "modified"

    async def test_page_size_default_100(self, stub, client) -> None:
        stub.on(
            lambda m, u, k: (
                _StubResponse(payload=_page([], next_url=None)) if "/api/documents/" in u else None
            )
        )
        _ = [d async for d in client.list_documents(modified_after=None, fields=["id"])]
        assert stub.calls[0].query["page_size"] == "100"


# --- list_document_ids ---------------------------------------------------------


class TestListDocumentIds:
    async def test_complete_enumeration(self, stub, client) -> None:
        page2 = f"{BASE_URL}/api/documents/?page=2"

        def handler(m, u, k):
            if "/api/documents/" not in u:
                return None
            if _query(u, k).get("page") == "2":
                return _StubResponse(payload=_page([{"id": 9}], next_url=None, count=3))
            return _StubResponse(payload=_page([{"id": 4}, {"id": 5}], next_url=page2, count=3))

        stub.on(handler)
        assert sorted(await client.list_document_ids()) == [4, 5, 9]

    async def test_http_failure_midway_raises_enumeration_error(self, stub, client) -> None:
        page2 = f"{BASE_URL}/api/documents/?page=2"

        def handler(m, u, k):
            if "/api/documents/" not in u:
                return None
            if _query(u, k).get("page") == "2":
                return _StubResponse(status_code=500, payload={"detail": "boom"})
            return _StubResponse(payload=_page([{"id": 4}, {"id": 5}], next_url=page2, count=3))

        stub.on(handler)
        with pytest.raises(PaperlessEnumerationError):
            await client.list_document_ids()

    async def test_connect_error_midway_raises_enumeration_error(self, stub, client) -> None:
        page2 = f"{BASE_URL}/api/documents/?page=2"

        def handler(m, u, k):
            if "/api/documents/" not in u:
                return None
            if _query(u, k).get("page") == "2":
                return _connect_error(m, u, k)
            return _StubResponse(payload=_page([{"id": 4}], next_url=page2, count=2))

        stub.on(handler)
        with pytest.raises(PaperlessEnumerationError):
            await client.list_document_ids()

    async def test_first_page_failure_raises_enumeration_error(self, stub, client) -> None:
        stub.on(
            lambda m, u, k: (
                _StubResponse(status_code=502, payload={}) if "/api/documents/" in u else None
            )
        )
        with pytest.raises(PaperlessEnumerationError):
            await client.list_document_ids()


# --- get_task -------------------------------------------------------------------


class TestGetTask:
    async def test_parses_success_with_result_key(self, stub, client) -> None:
        stub.on(
            lambda m, u, k: (
                _StubResponse(
                    payload=[
                        {
                            "task_id": "abc",
                            "status": "SUCCESS",
                            "related_document": 12,
                            "result": "Success. New document id 12 created",
                        }
                    ]
                )
                if "/api/tasks/" in u
                else None
            )
        )
        ts = await client.get_task("abc")
        assert isinstance(ts, TaskStatus)
        assert ts.status == "SUCCESS"
        assert ts.related_document == 12
        assert "New document id 12" in ts.message
        assert stub.calls[0].query["task_id"] == "abc"

    async def test_parses_results_envelope_and_failure(self, stub, client) -> None:
        stub.on(
            lambda m, u, k: (
                _StubResponse(
                    payload={
                        "results": [
                            {
                                "status": "FAILURE",
                                "related_document": None,
                                "result": "Not consuming x.pdf: It is a duplicate of y.pdf (#12)",
                            }
                        ]
                    }
                )
                if "/api/tasks/" in u
                else None
            )
        )
        ts = await client.get_task("abc")
        assert ts.status == "FAILURE"
        assert ts.related_document is None
        assert "duplicate" in ts.message

    async def test_accepts_message_key_alias(self, stub, client) -> None:
        stub.on(
            lambda m, u, k: (
                _StubResponse(
                    payload={
                        "results": [
                            {"status": "PENDING", "related_document": None, "message": "waiting"}
                        ]
                    }
                )
                if "/api/tasks/" in u
                else None
            )
        )
        ts = await client.get_task("abc")
        assert ts.status == "PENDING"
        assert ts.message == "waiting"


# --- post_document ---------------------------------------------------------------


class TestPostDocument:
    async def test_multipart_fields_and_uuid(self, stub, client, tmp_path: Path) -> None:
        f = tmp_path / "receipt.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        stub.on(
            lambda m, u, k: (
                _StubResponse(payload="7b4a0d2e-1111-2222-3333-444455556666")
                if m == "POST" and "/api/documents/post_document/" in u
                else None
            )
        )
        uuid = await client.post_document(f, title="Receipt", created="2026-08-01", tag_ids=[3, 4])
        assert uuid == "7b4a0d2e-1111-2222-3333-444455556666"

        post = next(c for c in stub.calls if c.method == "POST")
        files = post.kwargs.get("files") or {}
        assert "document" in files, post.kwargs
        data = post.kwargs.get("data") or {}
        assert data.get("title") == "Receipt"
        assert data.get("created") == "2026-08-01"
        tags = data.get("tags")
        tags_list = list(tags) if isinstance(tags, (list, tuple)) else [tags]
        assert [int(t) for t in tags_list] == [3, 4]

    async def test_optional_fields_omitted(self, stub, client, tmp_path: Path) -> None:
        f = tmp_path / "scan.png"
        f.write_bytes(b"png")
        stub.on(lambda m, u, k: _StubResponse(payload="u-1") if m == "POST" else None)
        assert await client.post_document(f) == "u-1"
        post = next(c for c in stub.calls if c.method == "POST")
        data = post.kwargs.get("data") or {}
        assert not data.get("title")
        assert not data.get("created")
        assert not data.get("tags")


# --- ensure_tags / name_maps ---------------------------------------------------


class TestTags:
    async def test_ensure_tags_creates_missing(self, stub, client) -> None:
        def handler(m, u, k):
            if "/api/tags/" not in u:
                return None
            if m == "GET":
                return _StubResponse(payload=_page([{"id": 3, "name": "utilities"}], next_url=None))
            if m == "POST":
                body = k.get("json") or k.get("data") or {}
                return _StubResponse(status_code=201, payload={"id": 9, "name": body.get("name")})
            return None

        stub.on(handler)
        ids = await client.ensure_tags(["utilities", "new-tag"])
        assert ids == [3, 9]
        posts = [c for c in stub.calls if c.method == "POST"]
        assert len(posts) == 1
        body = posts[0].kwargs.get("json") or posts[0].kwargs.get("data") or {}
        assert body.get("name") == "new-tag"

    async def test_ensure_tags_all_known_no_post(self, stub, client) -> None:
        stub.on(
            lambda m, u, k: (
                _StubResponse(payload=_page([{"id": 3, "name": "utilities"}], next_url=None))
                if m == "GET" and "/api/tags/" in u
                else None
            )
        )
        assert await client.ensure_tags(["utilities"]) == [3]
        assert not [c for c in stub.calls if c.method == "POST"]

    async def test_name_maps(self, stub, client) -> None:
        def handler(m, u, k):
            if "/api/tags/" in u:
                return _StubResponse(payload=_page([{"id": 1, "name": "Purchase"}], next_url=None))
            if "/api/correspondents/" in u:
                return _StubResponse(payload=_page([{"id": 2, "name": "Railway"}], next_url=None))
            if "/api/document_types/" in u:
                return _StubResponse(payload=_page([{"id": 5, "name": "Receipt"}], next_url=None))
            return None

        stub.on(handler)
        maps = await client.name_maps()
        assert maps == NameMaps(
            tags={1: "Purchase"}, correspondents={2: "Railway"}, document_types={5: "Receipt"}
        )


# --- Error ladder -----------------------------------------------------------------


class TestErrorLadder:
    async def test_connect_error_is_retryable(self, stub, client) -> None:
        stub.on(_connect_error)
        with pytest.raises(ToolRetryable):
            await client.get_document(1)

    async def test_401_names_token_config_key(self, stub, client) -> None:
        stub.on(lambda m, u, k: _StubResponse(status_code=401, payload={"detail": "bad token"}))
        with pytest.raises(ToolError) as ei:
            await client.get_document(1)
        assert "PAPERLESS_API_TOKEN" in str(ei.value)
        assert not isinstance(ei.value, ToolRetryable)

    async def test_403_names_token_config_key(self, stub, client) -> None:
        stub.on(lambda m, u, k: _StubResponse(status_code=403, payload={"detail": "forbidden"}))
        with pytest.raises(ToolError) as ei:
            await client.get_document(1)
        assert "PAPERLESS_API_TOKEN" in str(ei.value)

    async def test_500_is_tool_error(self, stub, client) -> None:
        stub.on(lambda m, u, k: _StubResponse(status_code=500, payload={"detail": "boom"}))
        with pytest.raises(ToolError):
            await client.get_document(1)
