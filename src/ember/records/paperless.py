"""paperless-ngx client (doc #59 FC-34).

httpx per call (sidecar idiom, `tools/mealie.py`), API version pinned via
`Accept: application/json; version=10` so a paperless major that drops
v10 fails loudly rather than silently changing shapes under the sync.
Error ladder: `ConnectError → ToolRetryable`; 401/403 → `ToolError`
naming `PAPERLESS_API_TOKEN`; other 4xx/5xx → `ToolError`.

The client is deliberately thin and deterministic — no LLM anywhere
(TLC-29). Its only paperless WRITES are `post_document` and tag creation
inside `ensure_tags`.
"""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ember.errors import PaperlessEnumerationError, ToolError, ToolRetryable

_log = logging.getLogger("ember.records.paperless")

_DEFAULT_TIMEOUT = 30.0
_UPLOAD_TIMEOUT = 120.0
_API_VERSION = "10"


@dataclass(frozen=True)
class NameMaps:
    """id → name for the three metadata namespaces, fetched once per sync."""

    tags: dict[int, str]
    correspondents: dict[int, str]
    document_types: dict[int, str]


@dataclass(frozen=True)
class TaskStatus:
    """One consumption task. `status` is paperless's own string
    (PENDING | STARTED | SUCCESS | FAILURE | RETRY | REVOKED)."""

    status: str
    related_document: int | None
    message: str


class PaperlessClient:
    def __init__(self, *, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token

    # --- transport --------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Token {self._token}",
            "Accept": f"application/json; version={_API_VERSION}",
        }

    def _url(self, path_or_url: str) -> str:
        # `next` links from paginated responses are absolute; follow them
        # verbatim. Relative API paths are joined to the base.
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        return f"{self._base_url}{path_or_url}"

    async def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> httpx.Response:
        url = self._url(path_or_url)
        kwargs: dict[str, Any] = {"headers": self._headers()}
        if params is not None:
            kwargs["params"] = params
        if json is not None:
            kwargs["json"] = json
        if data is not None:
            kwargs["data"] = data
        if files is not None:
            kwargs["files"] = files
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, url, **kwargs)
        except httpx.ConnectError as e:
            raise ToolRetryable(f"paperless unreachable: {e}") from e
        except httpx.HTTPError as e:
            raise ToolError(f"paperless HTTP error: {e}") from e
        return resp

    @staticmethod
    def _raise_for_status(resp: httpx.Response, what: str) -> None:
        if resp.status_code in (401, 403):
            raise ToolError(
                f"paperless {what}: auth rejected (HTTP {resp.status_code}) — "
                "check PAPERLESS_API_TOKEN"
            )
        if resp.status_code == 404:
            raise ToolError(f"paperless {what}: not found")
        if resp.status_code >= 500:
            raise ToolError(f"paperless {what}: server error (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise ToolError(f"paperless {what}: HTTP {resp.status_code}: {resp.text[:200]}")

    async def _get_json(
        self, path_or_url: str, *, what: str, params: dict[str, Any] | None = None
    ) -> Any:
        resp = await self._request("GET", path_or_url, params=params)
        self._raise_for_status(resp, what)
        try:
            return resp.json()
        except ValueError as e:
            raise ToolError(f"paperless {what}: non-JSON response") from e

    async def _iter_pages(
        self, path: str, *, what: str, params: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield each page body of a paginated list endpoint, following
        `next` URLs verbatim (they already carry the query string)."""
        body = await self._get_json(path, what=what, params=params)
        while True:
            if not isinstance(body, dict) or not isinstance(body.get("results"), list):
                raise ToolError(f"paperless {what}: unexpected page shape")
            yield body
            next_url = body.get("next")
            if not next_url:
                return
            body = await self._get_json(str(next_url), what=what)

    async def _iter_results(
        self, path: str, *, what: str, params: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        async for page in self._iter_pages(path, what=what, params=params):
            for item in page["results"]:
                if isinstance(item, dict):
                    yield item

    # --- documents --------------------------------------------------------

    async def list_documents(
        self,
        *,
        modified_after: str | None,
        fields: Sequence[str],
        page_size: int = 100,
    ) -> AsyncIterator[dict]:
        params: dict[str, Any] = {
            "ordering": "modified",
            "page_size": page_size,
            "fields": ",".join(fields),
        }
        if modified_after:
            params["modified__gt"] = modified_after
        async for item in self._iter_results("/api/documents/", what="documents", params=params):
            yield item

    async def list_document_ids(self) -> list[int]:
        """Every live document id, or `PaperlessEnumerationError` — never
        a partial list (TLC-30 fail-safe for reconciliation)."""
        ids: list[int] = []
        reported: int | None = None
        try:
            async for page in self._iter_pages(
                "/api/documents/",
                what="document ids",
                params={"ordering": "id", "page_size": 100, "fields": "id"},
            ):
                if reported is None:
                    reported = int(page.get("count"))
                for item in page["results"]:
                    ids.append(int(item["id"]))
        except Exception as e:
            raise PaperlessEnumerationError(f"document id enumeration failed: {e}") from e
        if reported is None or len(ids) != reported:
            raise PaperlessEnumerationError(
                f"document id enumeration incomplete: collected {len(ids)}, reported {reported}"
            )
        return ids

    async def get_document(self, doc_id: int) -> dict:
        body = await self._get_json(f"/api/documents/{int(doc_id)}/", what=f"document {doc_id}")
        if not isinstance(body, dict):
            raise ToolError(f"paperless document {doc_id}: unexpected response shape")
        return body

    # --- metadata ---------------------------------------------------------

    async def _name_map(self, path: str, *, what: str) -> dict[int, str]:
        out: dict[int, str] = {}
        async for item in self._iter_results(path, what=what, params={"page_size": 100}):
            try:
                out[int(item["id"])] = str(item["name"])
            except (KeyError, TypeError, ValueError):
                _log.warning("paperless %s: skipping malformed item %r", what, item)
        return out

    async def name_maps(self) -> NameMaps:
        return NameMaps(
            tags=await self._name_map("/api/tags/", what="tags"),
            correspondents=await self._name_map("/api/correspondents/", what="correspondents"),
            document_types=await self._name_map("/api/document_types/", what="document types"),
        )

    async def ensure_tags(self, names: Sequence[str]) -> list[int]:
        """Resolve tag names → ids, creating unknown names (POST /api/tags/).
        Existing names match exactly first, then case-insensitively
        (paperless enforces case-insensitive uniqueness)."""
        wanted = [n.strip() for n in names if n and n.strip()]
        if not wanted:
            return []
        existing = await self._name_map("/api/tags/", what="tags")
        by_exact = {name: tid for tid, name in existing.items()}
        by_fold = {name.casefold(): tid for tid, name in existing.items()}
        ids: list[int] = []
        for name in wanted:
            tid = by_exact.get(name)
            if tid is None:
                tid = by_fold.get(name.casefold())
            if tid is None:
                resp = await self._request("POST", "/api/tags/", json={"name": name})
                self._raise_for_status(resp, f"create tag {name!r}")
                try:
                    tid = int(resp.json()["id"])
                except (ValueError, KeyError, TypeError) as e:
                    raise ToolError(f"paperless create tag {name!r}: unexpected response") from e
                by_exact[name] = tid
                by_fold[name.casefold()] = tid
            if tid not in ids:
                ids.append(tid)
        return ids

    # --- intake -----------------------------------------------------------

    async def post_document(
        self,
        path: Path,
        *,
        title: str | None = None,
        created: str | None = None,
        tag_ids: Sequence[int] = (),
    ) -> str:
        """Upload one file; returns the consumption task uuid."""
        path = Path(path)
        data: dict[str, Any] = {}
        if title:
            data["title"] = title
        if created:
            data["created"] = created
        if tag_ids:
            data["tags"] = [int(t) for t in tag_ids]
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as fh:
            resp = await self._request(
                "POST",
                "/api/documents/post_document/",
                data=data,
                files={"document": (path.name, fh, mime)},
                timeout=_UPLOAD_TIMEOUT,
            )
        self._raise_for_status(resp, "post_document")
        try:
            body = resp.json()
        except ValueError:
            body = resp.text.strip().strip('"')
        if isinstance(body, dict):
            body = body.get("task_id") or body.get("id")
        if not body:
            raise ToolError("paperless post_document: no task id in response")
        return str(body)

    async def get_task(self, task_uuid: str) -> TaskStatus:
        body = await self._get_json("/api/tasks/", what="task", params={"task_id": task_uuid})
        items: list[Any]
        if isinstance(body, list):
            items = body
        elif isinstance(body, dict) and isinstance(body.get("results"), list):
            items = body["results"]
        elif isinstance(body, dict) and "status" in body:
            items = [body]
        else:
            items = []
        if not items or not isinstance(items[0], dict):
            raise ToolError(f"paperless task {task_uuid}: not found")
        item = items[0]
        related_id = _task_related_document(item)
        message = _task_message(item)
        return TaskStatus(
            status=str(item.get("status") or "PENDING"),
            related_document=related_id,
            message=message,
        )


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _task_related_document(item: dict[str, Any]) -> int | None:
    """The document id a consumption task produced, across paperless
    versions: 2.x `related_document`; 3.x `result_data.document_id` and
    `related_document_ids: [id]` (observed live on 3.1.1, 2026-09-02)."""
    rid = _as_int(item.get("related_document"))
    if rid is not None:
        return rid
    result_data = item.get("result_data")
    if isinstance(result_data, dict):
        rid = _as_int(result_data.get("document_id"))
        if rid is not None:
            return rid
    ids = item.get("related_document_ids")
    if isinstance(ids, (list, tuple)) and ids:
        return _as_int(ids[0])
    return None


def _task_message(item: dict[str, Any]) -> str:
    """Human message for a task: 2.x `result` (string) or `message`; 3.x
    may carry it inside `result_data` (`message`/`error`/`result`). `result`
    wins when several are present."""
    for key in ("result", "message"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    result_data = item.get("result_data")
    if isinstance(result_data, dict):
        for key in ("message", "error", "result"):
            value = result_data.get(key)
            if isinstance(value, str) and value:
                return value
    elif isinstance(result_data, str) and result_data:
        return result_data
    return ""
