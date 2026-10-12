"""RecordsStore — the life-memory data plane, v1 (doc #59 FC-35).

Single instance per process, constructed in `main.py` next to `Store`
and shut down in the reverse-order path. Owns `records.db` (WAL), its
own schema ladder (`RECORDS_SCHEMA_VERSION` + `_MIGRATIONS` +
`records_schema.sql` mirror), and the FTS5 index over `documents`.

FTS maintenance is trigger-internal: no method here writes
`documents_fts` directly. `search_documents` has exact FTS5 semantics
plus the quoted-retry fallback; OR-broadening is the tool layer's
obligation (as FC-27 for history).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from ember.errors import RecordsStoreError
from ember.records.types import DocumentHit, IndexedDocument

__all__ = [
    "RECORDS_SCHEMA_VERSION",
    "CONTENT_MAX_CHARS",
    "RecordsStore",
]

RECORDS_SCHEMA_VERSION = 1
_SCHEMA_PATH = Path(__file__).parent / "records_schema.sql"

# Stored `content` cap. A 65k-char OCR body is the current corpus max;
# the cap bounds pathological scans without touching normal documents.
CONTENT_MAX_CHARS = 500_000

# bm25 column weights: title, correspondent, document_type, tags, content
# (Decision #11 — metadata between title and body).
_BM25_WEIGHTS = "3.0, 2.0, 1.5, 1.5, 1.0"

# Forward migrations from each version to the next. Keyed by
# `from_version`. Empty at v1 — the ladder exists so v2 has a slot.
_MIGRATIONS: dict[int, list[str]] = {}


class RecordsStore:
    """SQLite-backed documents index."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    # --- Lifecycle --------------------------------------------------------

    async def open(self) -> None:
        if self._conn is not None:
            raise RecordsStoreError("RecordsStore is already open")

        if str(self._db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            conn = await aiosqlite.connect(self._db_path)
        except (aiosqlite.Error, OSError) as e:
            raise RecordsStoreError(f"records.db open failed: {e}") from e
        conn.row_factory = aiosqlite.Row

        try:
            await conn.execute("PRAGMA journal_mode = WAL")
            await self._verify_fts5_available(conn)
            await self._verify_or_apply_schema(conn)
        except RecordsStoreError:
            await conn.close()
            raise
        except BaseException as e:
            await conn.close()
            if isinstance(e, aiosqlite.Error):
                raise RecordsStoreError(f"records.db schema check failed: {e}") from e
            raise

        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @staticmethod
    async def _verify_fts5_available(conn: aiosqlite.Connection) -> None:
        """Startup gate: raise `RecordsStoreError` if the linked SQLite has
        no FTS5 (TLC-7: no silent degradation). Cheap probe: create and
        drop a temp virtual table."""
        try:
            await conn.execute("CREATE VIRTUAL TABLE temp._records_fts5_probe USING fts5(x)")
            await conn.execute("DROP TABLE temp._records_fts5_probe")
        except aiosqlite.OperationalError as e:
            raise RecordsStoreError(
                f"SQLite FTS5 is unavailable ({e}); refusing to open records.db (TLC-7)"
            ) from e

    async def _verify_or_apply_schema(self, conn: aiosqlite.Connection) -> None:
        """Fresh DB → apply records_schema.sql. Existing DB → forward-migrate
        via the ladder; refuse a version newer than this build knows."""
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1")
        any_table = await cur.fetchone()
        await cur.close()

        if any_table is None:
            await conn.executescript(_SCHEMA_PATH.read_text())
            await conn.commit()
            return

        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='records_schema_version'"
        )
        sv_table = await cur.fetchone()
        await cur.close()
        if sv_table is None:
            raise RecordsStoreError(
                "records.db exists but has no records_schema_version table; refusing to open"
            )

        cur = await conn.execute(
            "SELECT version FROM records_schema_version ORDER BY version DESC LIMIT 1"
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise RecordsStoreError("records_schema_version table is empty")
        version = int(row[0])
        if version == RECORDS_SCHEMA_VERSION:
            return
        if version > RECORDS_SCHEMA_VERSION:
            raise RecordsStoreError(
                f"records_schema_version={version} is newer than this build's "
                f"{RECORDS_SCHEMA_VERSION}; refusing to open"
            )
        while version < RECORDS_SCHEMA_VERSION and version in _MIGRATIONS:
            for stmt in _MIGRATIONS[version]:
                await conn.execute(stmt)
            await conn.commit()
            version += 1
        if version != RECORDS_SCHEMA_VERSION:
            raise RecordsStoreError(
                f"records_schema_version={version}, expected {RECORDS_SCHEMA_VERSION}; "
                "no migration path"
            )

    # --- Internal helpers -------------------------------------------------

    @property
    def _c(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RecordsStoreError("RecordsStore is not open")
        return self._conn

    @staticmethod
    def _cap_content(content: str) -> str:
        if len(content) <= CONTENT_MAX_CHARS:
            return content
        return (
            content[:CONTENT_MAX_CHARS]
            + f"\n\n[content truncated: original was {len(content)} chars]"
        )

    @staticmethod
    def _row_to_document(r: aiosqlite.Row) -> IndexedDocument:
        raw_tags = json.loads(r["tags"] or "[]")
        return IndexedDocument(
            doc_id=int(r["source_id"]),
            source=r["source"],
            title=r["title"],
            created=r["created"],
            added=r["added"],
            modified=r["modified"],
            correspondent=r["correspondent"],
            document_type=r["document_type"],
            tags=tuple(str(t) for t in raw_tags),
            original_filename=r["original_filename"],
            mime_type=r["mime_type"],
            page_count=r["page_count"],
            content=r["content"],
        )

    # --- Documents --------------------------------------------------------

    async def upsert_document(self, row: IndexedDocument, *, now: datetime) -> None:
        """Insert or replace one indexed document keyed on (source,
        source_id). The FTS triggers keep `documents_fts` in step."""
        try:
            await self._c.execute(
                """
                INSERT INTO documents (
                    source, source_id, title, correspondent, document_type, tags,
                    content, created, added, modified, original_filename, mime_type,
                    page_count, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source, source_id) DO UPDATE SET
                    title = excluded.title,
                    correspondent = excluded.correspondent,
                    document_type = excluded.document_type,
                    tags = excluded.tags,
                    content = excluded.content,
                    created = excluded.created,
                    added = excluded.added,
                    modified = excluded.modified,
                    original_filename = excluded.original_filename,
                    mime_type = excluded.mime_type,
                    page_count = excluded.page_count,
                    indexed_at = excluded.indexed_at
                """,
                (
                    row.source,
                    str(row.doc_id),
                    row.title,
                    row.correspondent or "",
                    row.document_type or "",
                    json.dumps(list(row.tags), ensure_ascii=False),
                    self._cap_content(row.content),
                    row.created,
                    row.added,
                    row.modified,
                    row.original_filename,
                    row.mime_type,
                    row.page_count,
                    now.isoformat(),
                ),
            )
            await self._c.commit()
        except aiosqlite.Error as e:
            raise RecordsStoreError(f"upsert_document({row.source}/{row.doc_id}): {e}") from e

    async def delete_document(self, source: str, source_id: str) -> bool:
        """Remove one row (and its FTS entry via trigger). True iff a row
        was deleted."""
        try:
            cur = await self._c.execute(
                "DELETE FROM documents WHERE source = ? AND source_id = ?",
                (source, str(source_id)),
            )
            deleted = cur.rowcount
            await cur.close()
            await self._c.commit()
        except aiosqlite.Error as e:
            raise RecordsStoreError(f"delete_document({source}/{source_id}): {e}") from e
        return bool(deleted and deleted > 0)

    async def indexed_document_ids(self, source: str) -> set[str]:
        try:
            cur = await self._c.execute(
                "SELECT source_id FROM documents WHERE source = ?", (source,)
            )
            rows = await cur.fetchall()
            await cur.close()
        except aiosqlite.Error as e:
            raise RecordsStoreError(f"indexed_document_ids({source}): {e}") from e
        return {str(r[0]) for r in rows}

    async def get_document(self, source: str, source_id: str) -> IndexedDocument | None:
        try:
            cur = await self._c.execute(
                "SELECT * FROM documents WHERE source = ? AND source_id = ?",
                (source, str(source_id)),
            )
            r = await cur.fetchone()
            await cur.close()
        except aiosqlite.Error as e:
            raise RecordsStoreError(f"get_document({source}/{source_id}): {e}") from e
        if r is None:
            return None
        return self._row_to_document(r)

    async def document_count(self, source: str | None = None) -> int:
        try:
            if source is None:
                cur = await self._c.execute("SELECT COUNT(*) FROM documents")
            else:
                cur = await self._c.execute(
                    "SELECT COUNT(*) FROM documents WHERE source = ?", (source,)
                )
            row = await cur.fetchone()
            await cur.close()
        except aiosqlite.Error as e:
            raise RecordsStoreError(f"document_count: {e}") from e
        return int(row[0]) if row is not None else 0

    # --- Search (UoW-35) --------------------------------------------------

    async def search_documents(
        self,
        query: str,
        *,
        limit: int = 6,
        created_after: str | None = None,
        created_before: str | None = None,
        tag: str | None = None,
    ) -> list[DocumentHit]:
        """FTS5 MATCH over the five indexed columns, weighted-bm25 ranked.
        Malformed FTS5 syntax is retried once with every whitespace term
        double-quoted (literal AND search); `RecordsStoreError` only if the
        quoted fallback also fails. Exact semantics — no OR-broadening
        here (the tool layer owns that). Read-only."""
        kwargs: dict[str, Any] = {
            "limit": limit,
            "created_after": created_after,
            "created_before": created_before,
            "tag": tag,
        }
        try:
            return await self._search_documents_once(query, **kwargs)
        except aiosqlite.OperationalError as first:
            quoted = self._quote_fts_terms(query)
            if not quoted:
                raise RecordsStoreError(
                    f"search_documents: query {query!r} has no searchable terms: {first}"
                ) from first
            try:
                return await self._search_documents_once(quoted, **kwargs)
            except aiosqlite.OperationalError as e:
                raise RecordsStoreError(
                    f"search_documents: query {query!r} failed even after quoting: {e}"
                ) from e
        except aiosqlite.Error as e:
            raise RecordsStoreError(f"search_documents: {e}") from e

    @staticmethod
    def _quote_fts_terms(query: str) -> str:
        """Fallback form of a query FTS5 rejected: every whitespace-separated
        term double-quoted (embedded double quotes escaped by doubling), so
        operator characters are matched literally."""
        return " ".join('"' + term.replace('"', '""') + '"' for term in query.split())

    async def _search_documents_once(
        self,
        match_expr: str,
        *,
        limit: int,
        created_after: str | None,
        created_before: str | None,
        tag: str | None,
    ) -> list[DocumentHit]:
        clauses: list[str] = []
        params: list[Any] = [match_expr]
        if created_after is not None:
            clauses.append("AND d.created >= ?")
            params.append(created_after)
        if created_before is not None:
            clauses.append("AND d.created <= ?")
            params.append(created_before)
        if tag is not None:
            # Exact, case-sensitive name match against the JSON array.
            clauses.append("AND EXISTS (SELECT 1 FROM json_each(d.tags) je WHERE je.value = ?)")
            params.append(tag)
        params.append(limit)
        where_extra = (" " + " ".join(clauses)) if clauses else ""
        # snippet(): column 4 (`content`), `[`/`]` match markers, `…` for
        # elided context, ~32 tokens.
        cur = await self._c.execute(
            f"""
            SELECT d.source_id AS source_id,
                   d.title AS title,
                   d.created AS created,
                   d.correspondent AS correspondent,
                   d.document_type AS document_type,
                   d.tags AS tags,
                   snippet(documents_fts, 4, '[', ']', '…', 32) AS snippet
            FROM documents_fts
            JOIN documents d ON d.id = documents_fts.rowid
            WHERE documents_fts MATCH ?{where_extra}
            ORDER BY bm25(documents_fts, {_BM25_WEIGHTS}), d.id
            LIMIT ?
            """,
            tuple(params),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [
            DocumentHit(
                doc_id=int(r["source_id"]),
                title=r["title"],
                created=r["created"],
                correspondent=r["correspondent"],
                document_type=r["document_type"],
                tags=tuple(str(t) for t in json.loads(r["tags"] or "[]")),
                snippet=r["snippet"],
            )
            for r in rows
        ]

    # --- Sync state -------------------------------------------------------

    async def get_cursor(self, source: str) -> str | None:
        try:
            cur = await self._c.execute("SELECT cursor FROM sync_state WHERE source = ?", (source,))
            row = await cur.fetchone()
            await cur.close()
        except aiosqlite.Error as e:
            raise RecordsStoreError(f"get_cursor({source}): {e}") from e
        return None if row is None else row[0]

    async def set_cursor(self, source: str, cursor: str | None, *, now: datetime) -> None:
        try:
            await self._c.execute(
                """
                INSERT INTO sync_state (source, cursor, updated_at) VALUES (?, ?, ?)
                ON CONFLICT (source) DO UPDATE SET
                    cursor = excluded.cursor,
                    updated_at = excluded.updated_at
                """,
                (source, cursor, now.isoformat()),
            )
            await self._c.commit()
        except aiosqlite.Error as e:
            raise RecordsStoreError(f"set_cursor({source}): {e}") from e
