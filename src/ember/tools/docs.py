"""Document tools (doc #59 FC-36) — search, fetch, file, sync.

Registered only when `PAPERLESS_API_TOKEN` is set. All four are
unconfirmed (Decision #4): search/fetch are read-only over the Ember
index; `docs_file` is the user's own "file this" (reversible via the
paperless trash); `docs_sync` writes only Ember's own derived index.

Zero LLM calls anywhere in these paths (TLC-29).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ember.errors import RecordsStoreError, ToolError
from ember.records.indexer import DocumentIndexer
from ember.records.store import RecordsStore
from ember.tools._fts_terms import _or_expr, _plain_terms
from ember.tools.base import ToolResult

_SOURCE = "paperless"

# UoW-34 step 3: poll the consumption task every 3s for at most 45s.
_FILE_POLL_BUDGET_S = 45.0
_FILE_POLL_INTERVAL_S = 3.0

# UoW-34 extension allowlist. Office formats need the Tika + Gotenberg
# sidecars (Decision #12) and are accepted only behind the flag.
_NATIVE_EXTENSIONS = ("pdf", "png", "jpg", "jpeg", "tif", "tiff", "gif", "webp", "txt", "csv", "md")
_TIKA_EXTENSIONS = ("docx", "doc", "odt", "xlsx", "xls", "ods", "pptx", "eml")

_DUPLICATE_ID_RE = re.compile(r"#(\d+)")


def _doc_url(public_url: str, doc_id: int) -> str:
    return f"{public_url.rstrip('/')}/documents/{doc_id}/details"


class DocsSearchTool:
    name = "docs_search"
    description = (
        "Search the user's filed documents (receipts, statements, invoices, scans) by keyword; "
        "returns snippets with links to the originals.\n"
        "FTS5 keyword match over title, correspondent, document type, tags and OCR text "
        "(so 'plumber' hits a document whose correspondent is a plumbing company). Every "
        "word must match, so prefer 1-2 distinctive terms; a plain multi-word query with no "
        "hits is retried OR-ed and labeled match='broadened' (treat those as leads). Use "
        "docs_fetch with a hit's doc_id for the full text. When you cite a document in "
        "your reply, include its url so the user can open the original."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search terms (FTS5 keyword match)."},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 15,
                "default": 6,
                "description": "Maximum hits (default 6).",
            },
            "created_after": {
                "type": "string",
                "description": "Only documents dated on/after YYYY-MM-DD.",
            },
            "created_before": {
                "type": "string",
                "description": "Only documents dated on/before YYYY-MM-DD.",
            },
            "tag": {"type": "string", "description": "Only documents carrying this exact tag."},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    requires_confirmation: bool = False

    def __init__(self, *, store: RecordsStore, public_url: str) -> None:
        self._store = store
        self._public_url = public_url

    async def call(self, args: dict[str, Any]) -> ToolResult:
        query = str(args["query"])
        if not query.strip():
            raise ToolError("docs_search: query must be non-empty")
        limit = int(args.get("limit", 6))
        filters = {
            "created_after": args.get("created_after"),
            "created_before": args.get("created_before"),
            "tag": args.get("tag"),
        }
        try:
            hits = await self._store.search_documents(query, limit=limit, **filters)
        except RecordsStoreError as e:
            raise ToolError(f"docs_search failed: {e}") from e

        # UoW-35 step 4: zero-hit broadening, same rule as history_search.
        match_kind = "exact"
        if not hits:
            terms = _plain_terms(query)
            if terms is not None:
                try:
                    hits = await self._store.search_documents(
                        _or_expr(terms), limit=limit, **filters
                    )
                except RecordsStoreError as e:
                    raise ToolError(f"docs_search failed: {e}") from e
                match_kind = "broadened"

        return ToolResult(
            output={
                "match": match_kind,
                "hits": [
                    {
                        "doc_id": h.doc_id,
                        "title": h.title,
                        "created": h.created,
                        "correspondent": h.correspondent,
                        "document_type": h.document_type,
                        "tags": list(h.tags),
                        "snippet": h.snippet,
                        "url": _doc_url(self._public_url, h.doc_id),
                    }
                    for h in hits
                ],
            }
        )


class DocsFetchTool:
    name = "docs_fetch"
    description = (
        "Read the extracted text of one filed document (doc_id from a docs_search hit), "
        "paged by offset/max_chars; next_offset is null at the end. Include the url when "
        "you cite the document."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "doc_id": {"type": "integer", "description": "Document id from docs_search."},
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Character offset to start from (default 0).",
            },
            "max_chars": {
                "type": "integer",
                "minimum": 500,
                "maximum": 12000,
                "default": 6000,
                "description": "Characters to return (default 6000).",
            },
        },
        "required": ["doc_id"],
        "additionalProperties": False,
    }
    requires_confirmation: bool = False

    def __init__(self, *, store: RecordsStore, public_url: str) -> None:
        self._store = store
        self._public_url = public_url

    async def call(self, args: dict[str, Any]) -> ToolResult:
        doc_id = int(args["doc_id"])
        offset = int(args.get("offset", 0))
        max_chars = int(args.get("max_chars", 6000))
        try:
            doc = await self._store.get_document(_SOURCE, str(doc_id))
        except RecordsStoreError as e:
            raise ToolError(f"docs_fetch failed: {e}") from e
        if doc is None:
            raise ToolError(
                f"docs_fetch: document {doc_id} is not in the index — "
                "it may be excluded or not yet synced"
            )
        total = len(doc.content)
        chunk = doc.content[offset : offset + max_chars]
        end = offset + len(chunk)
        return ToolResult(
            output={
                "doc_id": doc.doc_id,
                "title": doc.title,
                "created": doc.created,
                "correspondent": doc.correspondent,
                "document_type": doc.document_type,
                "tags": list(doc.tags),
                "url": _doc_url(self._public_url, doc.doc_id),
                "content": chunk,
                "offset": offset,
                "total_chars": total,
                "next_offset": end if end < total else None,
            }
        )


class DocsFileTool:
    name = "docs_file"
    description = (
        "File an uploaded attachment (path from the attachment manifest) permanently into "
        "the user's documents (paperless) and index it for docs_search. Pass title/tags/created "
        "only when the user stated them."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path of the uploaded file (must be under /data/uploads/).",
            },
            "title": {"type": "string", "description": "Title, if the user gave one."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tag names the user asked for (created if new).",
            },
            "created": {"type": "string", "description": "Document date YYYY-MM-DD, if stated."},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    requires_confirmation: bool = False

    def __init__(
        self,
        *,
        client: Any,
        indexer: DocumentIndexer,
        data_dir: Path,
        public_url: str,
        tika_enabled: bool = False,
        poll_budget_s: float = _FILE_POLL_BUDGET_S,
        poll_interval_s: float = _FILE_POLL_INTERVAL_S,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._indexer = indexer
        self._uploads_dir = (Path(data_dir) / "uploads").resolve()
        self._public_url = public_url
        self._tika_enabled = tika_enabled
        self._poll_budget_s = poll_budget_s
        self._poll_interval_s = poll_interval_s
        self._sleep = sleep

    def _allowed_extensions(self) -> tuple[str, ...]:
        if self._tika_enabled:
            return _NATIVE_EXTENSIONS + _TIKA_EXTENSIONS
        return _NATIVE_EXTENSIONS

    def _check_path(self, raw: str) -> Path:
        """UoW-34 preconditions: under uploads/, exists, allowed extension.
        Runs before any HTTP call."""
        try:
            src = Path(raw).resolve()
        except OSError as e:
            raise ToolError(f"docs_file: path resolution failed: {e}") from e
        try:
            src.relative_to(self._uploads_dir)
        except ValueError:
            raise ToolError(
                f"docs_file: refusing to read {raw!r} — path must be under {self._uploads_dir}/"
            ) from None
        ext = src.suffix.lower().lstrip(".")
        allowed = self._allowed_extensions()
        if ext not in allowed:
            hint = (
                " Office documents are not supported on this install — export to PDF or CSV first."
                if ext in _TIKA_EXTENSIONS
                else ""
            )
            raise ToolError(
                f"docs_file: unsupported file type {('.' + ext) if ext else '(none)'!r}; "
                f"supported types: {', '.join(allowed)}.{hint}"
            )
        if not src.is_file():
            raise ToolError(f"docs_file: file not found: {raw!r}")
        return src

    async def call(self, args: dict[str, Any]) -> ToolResult:
        src = self._check_path(str(args["path"]))
        title = args.get("title") or None
        created = args.get("created") or None
        tag_names = [str(t) for t in (args.get("tags") or []) if str(t).strip()]

        tag_ids = await self._client.ensure_tags(tag_names) if tag_names else []
        task_id = await self._client.post_document(
            src, title=title, created=created, tag_ids=tag_ids
        )

        elapsed = 0.0
        while True:
            task = await self._client.get_task(task_id)
            status = (task.status or "").upper()
            if status == "SUCCESS":
                return await self._on_success(task, task_id, title)
            if status == "FAILURE":
                if "duplicate" in task.message.lower():
                    m = _DUPLICATE_ID_RE.search(task.message)
                    dup_id = int(m.group(1)) if m else None
                    return ToolResult(
                        output={
                            "status": "duplicate",
                            "doc_id": dup_id,
                            "title": None,
                            "url": _doc_url(self._public_url, dup_id) if dup_id else None,
                            "task_id": task_id,
                            "note": (
                                "paperless already has this file; nothing new was filed. "
                                + task.message
                            ),
                        }
                    )
                raise ToolError(f"docs_file: paperless rejected the document: {task.message}")
            if status == "REVOKED":
                raise ToolError(f"docs_file: paperless task {task_id} was revoked")
            if elapsed + self._poll_interval_s > self._poll_budget_s:
                return ToolResult(
                    output={
                        "status": "queued",
                        "doc_id": None,
                        "title": title,
                        "url": None,
                        "task_id": task_id,
                        "note": (
                            "paperless is still processing (OCR); the scheduled sync indexes "
                            "it within ~15 minutes, or call docs_sync to refresh sooner."
                        ),
                    }
                )
            await self._sleep(self._poll_interval_s)
            elapsed += self._poll_interval_s

    async def _on_success(self, task: Any, task_id: str, title: str | None) -> ToolResult:
        doc_id = task.related_document
        if doc_id is None:
            raise ToolError(f"docs_file: paperless task {task_id} succeeded without a document id")
        try:
            outcome = await self._indexer.index_one(int(doc_id))
        except RecordsStoreError as e:
            raise ToolError(f"docs_file: indexing document {doc_id} failed: {e}") from e
        except (KeyError, TypeError, ValueError) as e:
            raise ToolError(f"docs_file: document {doc_id} has an unreadable shape: {e}") from e
        stored = await self._indexer._store.get_document(_SOURCE, str(doc_id))  # noqa: SLF001
        final_title = stored.title if stored is not None else title
        if outcome == "excluded":
            note = (
                "filed in paperless, but it carries the exclusion tag so it is NOT in the "
                "search index."
            )
        else:
            note = "filed and indexed; searchable now via docs_search."
        return ToolResult(
            output={
                "status": "indexed",
                "doc_id": int(doc_id),
                "title": final_title,
                "url": _doc_url(self._public_url, int(doc_id)),
                "task_id": task_id,
                "note": note,
            }
        )


class DocsSyncTool:
    name = "docs_sync"
    description = (
        "Refresh the documents index from paperless now (the scheduled sync runs every "
        "15 min). full=true rebuilds from scratch."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "full": {
                "type": "boolean",
                "default": False,
                "description": "Ignore the sync cursor and re-index everything.",
            },
        },
        "additionalProperties": False,
    }
    requires_confirmation: bool = False

    def __init__(self, *, indexer: DocumentIndexer) -> None:
        self._indexer = indexer

    async def call(self, args: dict[str, Any]) -> ToolResult:
        full = bool(args.get("full", False))
        try:
            report = await self._indexer.sync(full=full)
        except RecordsStoreError as e:
            raise ToolError(f"docs_sync failed: {e}") from e
        return ToolResult(output=report.as_dict())
