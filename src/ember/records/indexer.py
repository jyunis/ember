"""DocumentIndexer — scheduled sync + single-document indexing (doc #59
UoW-33 / UoW-34, TLC-29 / TLC-30).

paperless is the system of record; `documents` in records.db is a
derived mirror. Every step here is deterministic plumbing — no LLM, no
provider/router import (TLC-29). Exclusion is a pure function of the
resolved tag names and runs before any write (TLC-30); reconciliation
deletes only against a COMPLETE id enumeration (fail-safe).

`client` is duck-typed to the `PaperlessClient` surface so tests can
pass a fake.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ember.records.paperless import NameMaps
from ember.records.store import RecordsStore
from ember.records.types import IndexedDocument, SyncReport

_log = logging.getLogger("ember.records.indexer")

# UoW-33 step 3: the `fields=` projection requested per document.
_SYNC_FIELDS = (
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
_PAGE_SIZE = 100


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DocumentIndexer:
    def __init__(
        self,
        *,
        client: Any,
        store: RecordsStore,
        exclude_tag: str,
        source: str = "paperless",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self._exclude_tag = exclude_tag
        self._source = source
        self._now = now or _utc_now

    # --- helpers ----------------------------------------------------------

    def _is_excluded(self, tags: tuple[str, ...]) -> bool:
        """TLC-30: excluded iff any resolved tag name equals the configured
        exclusion tag, case-insensitively."""
        needle = self._exclude_tag.casefold()
        return any(t.casefold() == needle for t in tags)

    @staticmethod
    def _opt_str(value: Any) -> str | None:
        if value is None or value == "":
            return None
        return str(value)

    @staticmethod
    def _date_only(value: Any) -> str | None:
        """paperless `created` is YYYY-MM-DD under API v10 but a datetime
        under older versions; keep the date part either way."""
        if value is None or value == "":
            return None
        return str(value)[:10]

    def _to_row(self, doc: dict[str, Any], maps: NameMaps) -> IndexedDocument:
        """Shape one paperless document dict into an `IndexedDocument`,
        resolving tag/correspondent/type ids to names. Raises on a
        malformed row (missing id/title, unparseable ids)."""
        doc_id = int(doc["id"])
        title = doc.get("title")
        if title is None:
            raise ValueError("missing title")
        raw_tags = doc.get("tags") or []
        if not isinstance(raw_tags, (list, tuple)):
            raise ValueError("tags is not a list")
        tags: list[str] = []
        for t in raw_tags:
            tid = int(t)
            name = maps.tags.get(tid)
            if name is None:
                # Fail CLOSED (TLC-30): if a tag id cannot be resolved the
                # exclusion check cannot be evaluated, so the row is a bad
                # row and is skipped rather than indexed. Typical cause:
                # the tag is owned by another paperless user and invisible
                # to the ember API user (paperless object permissions).
                raise ValueError(
                    f"unresolvable tag id {tid} (tag not visible to the API user? "
                    "grant view permission on the tag, or clear its owner)"
                )
            tags.append(name)
        corr_id = doc.get("correspondent")
        correspondent = maps.correspondents.get(int(corr_id), "") if corr_id is not None else ""
        type_id = doc.get("document_type")
        document_type = maps.document_types.get(int(type_id), "") if type_id is not None else ""
        # A None/non-string body means paperless has not finished (or has
        # failed) extraction; indexing an empty body would hide the doc
        # until its next `modified` bump, so treat it as a bad row and let
        # a later sync pick it up. An empty STRING is valid and indexed.
        content = doc.get("content")
        if not isinstance(content, str):
            raise ValueError("content is missing or not a string")
        page_count = doc.get("page_count")
        return IndexedDocument(
            doc_id=doc_id,
            source=self._source,
            title=str(title),
            created=self._date_only(doc.get("created")),
            added=self._opt_str(doc.get("added")),
            modified=self._opt_str(doc.get("modified")),
            correspondent=correspondent,
            document_type=document_type,
            tags=tuple(tags),
            original_filename=self._opt_str(doc.get("original_file_name")),
            mime_type=self._opt_str(doc.get("mime_type")),
            page_count=int(page_count) if page_count is not None else None,
            content=content,
        )

    async def _apply(self, row: IndexedDocument) -> str:
        """Exclusion check then write. Returns "indexed" or "excluded"."""
        if self._is_excluded(row.tags):
            await self._store.delete_document(self._source, str(row.doc_id))
            return "excluded"
        await self._store.upsert_document(row, now=self._now())
        return "indexed"

    # --- UoW-33 -----------------------------------------------------------

    async def sync(self, *, full: bool = False) -> SyncReport:
        maps = await self._client.name_maps()
        stored_cursor = await self._store.get_cursor(self._source)
        cursor = None if full else stored_cursor

        scanned = upserted = excluded = removed = skipped = 0
        max_modified: str | None = None

        async for doc in self._client.list_documents(
            modified_after=cursor, fields=_SYNC_FIELDS, page_size=_PAGE_SIZE
        ):
            scanned += 1
            try:
                row = self._to_row(doc, maps)
            except (KeyError, TypeError, ValueError) as e:
                skipped += 1
                _log.info(
                    "sync: skipping document row id=%r: %s",
                    doc.get("id") if isinstance(doc, dict) else doc,
                    e,
                )
                continue
            outcome = await self._apply(row)
            if outcome == "excluded":
                excluded += 1
            else:
                upserted += 1
            if row.modified is not None and (max_modified is None or row.modified > max_modified):
                max_modified = row.modified

        # Step 5: reconciliation — delete only against a complete
        # enumeration; any failure removes nothing (TLC-30 fail-safe).
        reconciliation_skipped = False
        try:
            live_ids = await self._client.list_document_ids()
        except Exception as e:
            _log.warning("sync: id enumeration failed, reconciliation skipped: %s", e)
            reconciliation_skipped = True
        else:
            live = {str(int(i)) for i in live_ids}
            indexed = await self._store.indexed_document_ids(self._source)
            for source_id in sorted(indexed - live, key=lambda s: (len(s), s)):
                if await self._store.delete_document(self._source, source_id):
                    removed += 1

        # Step 6: cursor advances only after step 4's writes committed
        # (each upsert commits), to the max `modified` seen this run.
        new_cursor = stored_cursor
        if max_modified is not None and (stored_cursor is None or max_modified > stored_cursor):
            new_cursor = max_modified
        if max_modified is not None:
            new_cursor = max_modified if full else new_cursor
            await self._store.set_cursor(self._source, new_cursor, now=self._now())

        if skipped:
            _log.warning(
                "sync: %d document row(s) skipped (unresolvable tags or malformed rows) — "
                "check the ember API user's paperless permissions",
                skipped,
            )
        report = SyncReport(
            scanned=scanned,
            upserted=upserted,
            excluded=excluded,
            removed=removed,
            reconciliation_skipped=reconciliation_skipped,
            cursor=new_cursor,
            skipped=skipped,
        )
        _log.info("docs sync (full=%s): %s", full, report.as_dict())
        return report

    # --- UoW-34 -----------------------------------------------------------

    async def index_one(self, doc_id: int) -> str:
        """Fetch one document from paperless and apply TLC-30 + upsert.
        Returns "indexed" or "excluded" (an excluded document that was
        previously indexed is deleted)."""
        maps = await self._client.name_maps()
        doc = await self._client.get_document(int(doc_id))
        row = self._to_row(doc, maps)
        return await self._apply(row)
