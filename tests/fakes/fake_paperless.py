"""In-memory stand-in for `ember.records.paperless.PaperlessClient` (FC-34).

Implements the duck surface the indexer and `docs_file` tool consume:

- `list_documents(*, modified_after, fields, page_size)` — async iterator
  over raw paperless-shaped document dicts, ordered by `modified`
  ascending, filtered with `modified__gt` semantics (strictly greater,
  ISO-string comparison), projected to `fields`.
- `list_document_ids()` — every live id, or `PaperlessEnumerationError`
  when `fail_enumeration` is set (the TLC-30 fail-safe trigger).
- `get_document(doc_id)` — the raw dict; `ToolError` for unknown ids
  (what a 404 becomes in the real client).
- `name_maps()` — `NameMaps` built from the id→name tables.
- `ensure_tags(names)` — resolves names to ids, creating unknown ones;
  every call is recorded in `ensure_tags_calls`.
- `post_document(path, *, title, created, tag_ids)` — records the call in
  `post_calls` and returns a fixed uuid string.
- `get_task(task_uuid)` — pops from the scripted `task_statuses` queue;
  the last scripted status is sticky (repeated forever), so a single
  `PENDING` models a task that never finishes.

Documents are stored the way paperless's API returns them: `tags` is a
list of tag ids, `correspondent` / `document_type` are ids or None.
`add_document` accepts NAMES for tags/correspondent/type and resolves
them to ids so tests read naturally. The returned dict is the live
stored object — tests may mutate it (e.g. `doc["content"] = None`) to
model a bad row.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ember.errors import PaperlessEnumerationError, ToolError
from ember.records.paperless import NameMaps, TaskStatus

# The projection the indexer requests (UoW-33 step 3). Kept here so tests
# can build expectations without depending on the indexer's constant.
DOCUMENT_FIELDS: tuple[str, ...] = (
    "id",
    "title",
    "content",
    "checksum",
    "created",
    "added",
    "modified",
    "correspondent",
    "document_type",
    "tags",
    "original_file_name",
    "mime_type",
    "page_count",
)


@dataclass
class PostCall:
    path: Path
    title: str | None
    created: str | None
    tag_ids: tuple[int, ...]


@dataclass
class FakePaperlessClient:
    docs: dict[int, dict[str, Any]] = field(default_factory=dict)
    tag_names: dict[int, str] = field(default_factory=dict)
    correspondent_names: dict[int, str] = field(default_factory=dict)
    document_type_names: dict[int, str] = field(default_factory=dict)
    fail_enumeration: bool = False
    task_statuses: list[TaskStatus] = field(default_factory=list)
    task_uuid: str = "task-uuid-1"
    post_calls: list[PostCall] = field(default_factory=list)
    ensure_tags_calls: list[tuple[str, ...]] = field(default_factory=list)
    list_documents_calls: list[dict[str, Any]] = field(default_factory=list)
    get_task_calls: int = 0

    # --- test-side helpers -------------------------------------------------

    def _id_for(self, table: dict[int, str], name: str) -> int:
        for k, v in table.items():
            if v == name:
                return k
        new_id = max(table, default=0) + 1
        table[new_id] = name
        return new_id

    def tag_id(self, name: str) -> int:
        return self._id_for(self.tag_names, name)

    def add_document(
        self,
        doc_id: int,
        *,
        title: str,
        content: str,
        modified: str,
        created: str | None = None,
        added: str | None = None,
        tags: Sequence[str] = (),
        correspondent: str | None = None,
        document_type: str | None = None,
        original_file_name: str | None = None,
        mime_type: str | None = "application/pdf",
        page_count: int | None = 1,
        checksum: str | None = None,
    ) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "id": doc_id,
            "title": title,
            "content": content,
            "checksum": checksum or f"sum-{doc_id}",
            "created": created,
            "added": added or modified,
            "modified": modified,
            "correspondent": (
                self._id_for(self.correspondent_names, correspondent) if correspondent else None
            ),
            "document_type": (
                self._id_for(self.document_type_names, document_type) if document_type else None
            ),
            "tags": [self.tag_id(t) for t in tags],
            "original_file_name": original_file_name or f"{title}.pdf",
            "mime_type": mime_type,
            "page_count": page_count,
        }
        self.docs[doc_id] = doc
        return doc

    def set_tags(self, doc_id: int, tags: Sequence[str], *, modified: str) -> None:
        """Retag a stored document (the 'tag added later' scenario)."""
        self.docs[doc_id]["tags"] = [self.tag_id(t) for t in tags]
        self.docs[doc_id]["modified"] = modified

    def remove_document(self, doc_id: int) -> None:
        del self.docs[doc_id]

    def script_task(self, *statuses: TaskStatus) -> None:
        self.task_statuses = list(statuses)

    # --- PaperlessClient duck surface -------------------------------------

    async def list_documents(
        self,
        *,
        modified_after: str | None,
        fields: Sequence[str],
        page_size: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        self.list_documents_calls.append(
            {"modified_after": modified_after, "fields": tuple(fields), "page_size": page_size}
        )
        rows = sorted(self.docs.values(), key=lambda d: d.get("modified") or "")
        for d in rows:
            if modified_after is not None and not ((d.get("modified") or "") > modified_after):
                continue
            yield {k: d[k] for k in fields if k in d}

    async def list_document_ids(self) -> list[int]:
        if self.fail_enumeration:
            raise PaperlessEnumerationError("paperless: enumeration failed mid-page")
        return sorted(self.docs)

    async def get_document(self, doc_id: int) -> dict[str, Any]:
        if doc_id not in self.docs:
            raise ToolError(f"paperless: document {doc_id} not found")
        return dict(self.docs[doc_id])

    async def name_maps(self) -> NameMaps:
        return NameMaps(
            tags=dict(self.tag_names),
            correspondents=dict(self.correspondent_names),
            document_types=dict(self.document_type_names),
        )

    async def ensure_tags(self, names: Sequence[str]) -> list[int]:
        self.ensure_tags_calls.append(tuple(names))
        return [self.tag_id(n) for n in names]

    async def post_document(
        self,
        path: Path,
        *,
        title: str | None = None,
        created: str | None = None,
        tag_ids: Sequence[int] = (),
    ) -> str:
        self.post_calls.append(
            PostCall(path=Path(path), title=title, created=created, tag_ids=tuple(tag_ids))
        )
        return self.task_uuid

    async def get_task(self, task_uuid: str) -> TaskStatus:
        self.get_task_calls += 1
        if not self.task_statuses:
            return TaskStatus(status="PENDING", related_document=None, message="")
        if len(self.task_statuses) > 1:
            return self.task_statuses.pop(0)
        return self.task_statuses[0]
