"""RecordsStore (`records/store.py`, FC-35) — the life-memory data plane v1.

Properties under test (doc #59 Test plan):

- Fresh open creates schema v1 (`records_schema_version` holds exactly
  one row, `1`); reopening an existing db is a no-op (rows survive, the
  version row is unchanged).
- A db whose version row is newer than `RECORDS_SCHEMA_VERSION` refuses
  to open with `RecordsStoreError`.
- Upsert / update / delete keep `documents_fts` in sync — verified with
  `MATCH` via `search_documents`, never bare row counts (the v14 lesson).
- Porter stemming: `receipts` finds a doc containing only `receipt`, and
  vice versa (Decision #10).
- Metadata columns are searchable: a correspondent-name term and a tag
  name hit docs whose content lacks the word (Decision #11).
- Ranking weights: a title-only hit outranks a content-only hit for the
  same term.
- `created_after` / `created_before` are inclusive on the `created`
  column; `tag` is an exact, case-sensitive name filter.
- Malformed FTS syntax is retried with each term double-quoted and does
  not raise; a hopeless query raises `RecordsStoreError`.
- Empty index → `[]`; stored content is capped at `CONTENT_MAX_CHARS`
  with the documented trailing marker.
- Cursor get/set round-trip incl. `None`; `get_document` returns the full
  row with `tags` as a tuple; `document_count`.
- Read-only: the sqlite change counter is untouched across search/get.

Documented choices (spec ambiguities):
- "Hopeless query": with inner quotes escaped, every non-empty term can
  be quoted into a valid phrase, so the only query the quoted retry
  cannot rescue is one with NO terms at all — a whitespace-only string,
  whose retry is the empty MATCH that FTS5 rejects. That is the case
  asserted to raise `RecordsStoreError`. (The tool layer rejects empty
  queries before the store sees them; this pins the store's own
  posture.)
- Read-only check uses the underlying aiosqlite connection when the
  store exposes it under a conventional private name (`_c`, as the
  conversation `Store` does; `_conn` / `_db` also probed). If none is
  present the test is skipped rather than guessed.
- The truncation marker text is pinned exactly as the interface file
  states: `"\\n\\n[content truncated: original was N chars]"`.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ember.errors import RecordsStoreError
from ember.records.store import CONTENT_MAX_CHARS, RECORDS_SCHEMA_VERSION, RecordsStore
from ember.records.types import DocumentHit, IndexedDocument

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _doc(
    doc_id: int,
    *,
    title: str = "Untitled",
    content: str = "",
    created: str | None = "2026-06-15",
    correspondent: str = "",
    document_type: str = "",
    tags: tuple[str, ...] = (),
    modified: str | None = "2026-06-15T10:00:00Z",
) -> IndexedDocument:
    return IndexedDocument(
        doc_id=doc_id,
        source="paperless",
        title=title,
        created=created,
        added="2026-06-15T09:00:00Z",
        modified=modified,
        correspondent=correspondent,
        document_type=document_type,
        tags=tags,
        original_filename=f"{doc_id}.pdf",
        mime_type="application/pdf",
        page_count=1,
        content=content,
    )


@pytest.fixture
async def store(tmp_path: Path):
    s = RecordsStore(tmp_path / "records.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


def _version_rows(db_path: Path) -> list[tuple[int]]:
    c = sqlite3.connect(db_path)
    try:
        return c.execute("SELECT version FROM records_schema_version").fetchall()
    finally:
        c.close()


def _ids(hits: list[DocumentHit]) -> list[int]:
    return [h.doc_id for h in hits]


# --- Schema lifecycle -----------------------------------------------------


class TestSchemaLifecycle:
    async def test_fresh_open_creates_schema_v1(self, tmp_path: Path) -> None:
        assert RECORDS_SCHEMA_VERSION == 1
        db = tmp_path / "records.db"
        s = RecordsStore(db)
        await s.open()
        await s.close()
        assert _version_rows(db) == [(1,)]

    async def test_reopen_is_noop_and_keeps_rows(self, tmp_path: Path) -> None:
        db = tmp_path / "records.db"
        s = RecordsStore(db)
        await s.open()
        await s.upsert_document(_doc(1, title="kept", content="survives reopen"), now=NOW)
        await s.close()

        s2 = RecordsStore(db)
        await s2.open()
        try:
            assert _version_rows(db) == [(1,)]
            assert await s2.document_count() == 1
            assert _ids(await s2.search_documents("survives")) == [1]
        finally:
            await s2.close()

    async def test_newer_schema_version_refuses_to_open(self, tmp_path: Path) -> None:
        db = tmp_path / "records.db"
        s = RecordsStore(db)
        await s.open()
        await s.close()

        c = sqlite3.connect(db)
        c.execute("UPDATE records_schema_version SET version = 99")
        c.commit()
        c.close()

        s2 = RecordsStore(db)
        with pytest.raises(RecordsStoreError):
            await s2.open()
        try:
            await s2.close()
        except Exception:
            pass


# --- FTS sync on write ----------------------------------------------------


class TestFtsSync:
    async def test_upsert_then_match(self, store: RecordsStore) -> None:
        await store.upsert_document(
            _doc(1, title="Water bill", content="quarterly aqueduct charge"), now=NOW
        )
        assert _ids(await store.search_documents("aqueduct")) == [1]

    async def test_update_changes_searchable_text(self, store: RecordsStore) -> None:
        await store.upsert_document(_doc(1, title="v1", content="the oldterm text"), now=NOW)
        assert _ids(await store.search_documents("oldterm")) == [1]

        await store.upsert_document(_doc(1, title="v2", content="the newterm text"), now=NOW)
        assert await store.search_documents("oldterm") == []
        assert _ids(await store.search_documents("newterm")) == [1]
        assert await store.document_count() == 1

    async def test_delete_removes_from_fts(self, store: RecordsStore) -> None:
        await store.upsert_document(_doc(1, content="ephemeral wombat"), now=NOW)
        assert _ids(await store.search_documents("wombat")) == [1]

        assert await store.delete_document("paperless", "1") is True
        assert await store.search_documents("wombat") == []
        assert await store.delete_document("paperless", "1") is False

    async def test_indexed_document_ids(self, store: RecordsStore) -> None:
        await store.upsert_document(_doc(3, content="a"), now=NOW)
        await store.upsert_document(_doc(7, content="b"), now=NOW)
        assert await store.indexed_document_ids("paperless") == {"3", "7"}
        assert await store.indexed_document_ids("gmail") == set()


# --- Stemming / metadata / ranking -----------------------------------------


class TestStemmingMetadataRanking:
    async def test_porter_stemming_both_directions(self, store: RecordsStore) -> None:
        await store.upsert_document(_doc(1, content="one receipt for the pump"), now=NOW)
        await store.upsert_document(_doc(2, content="several receipts attached"), now=NOW)
        assert sorted(_ids(await store.search_documents("receipts"))) == [1, 2]
        assert sorted(_ids(await store.search_documents("receipt"))) == [1, 2]

    async def test_correspondent_name_searchable(self, store: RecordsStore) -> None:
        await store.upsert_document(
            _doc(
                1,
                title="Invoice 4471",
                correspondent="Advantage Air Plumbing",
                content="service call parts labor total 312.50",
            ),
            now=NOW,
        )
        assert _ids(await store.search_documents("plumbing")) == [1]

    async def test_tag_name_searchable(self, store: RecordsStore) -> None:
        await store.upsert_document(
            _doc(1, title="Form 1099", tags=("taxes", "Purchase"), content="numbers only 1234"),
            now=NOW,
        )
        assert _ids(await store.search_documents("taxes")) == [1]

    async def test_document_type_searchable(self, store: RecordsStore) -> None:
        await store.upsert_document(
            _doc(1, title="Q2", document_type="Statement", content="balance 100"), now=NOW
        )
        assert _ids(await store.search_documents("statement")) == [1]

    async def test_title_hit_outranks_content_only_hit(self, store: RecordsStore) -> None:
        filler = " ".join(f"word{i}" for i in range(40))
        await store.upsert_document(_doc(1, title="Zephyr warranty card", content=filler), now=NOW)
        await store.upsert_document(
            _doc(2, title="Assorted paperwork", content=f"{filler} zephyr {filler}"), now=NOW
        )
        hits = await store.search_documents("zephyr")
        assert _ids(hits) == [1, 2]


# --- Filters / limit -------------------------------------------------------


class TestFilters:
    @pytest.fixture
    async def dated(self, store: RecordsStore) -> RecordsStore:
        await store.upsert_document(
            _doc(1, created="2026-01-10", content="invoice jan", tags=("Purchase", "tax")), now=NOW
        )
        await store.upsert_document(
            _doc(2, created="2026-02-10", content="invoice feb", tags=("Purchase",)), now=NOW
        )
        await store.upsert_document(
            _doc(3, created="2026-03-10", content="invoice mar", tags=()), now=NOW
        )
        return store

    async def test_created_after_inclusive(self, dated: RecordsStore) -> None:
        hits = await dated.search_documents("invoice", created_after="2026-02-10")
        assert sorted(_ids(hits)) == [2, 3]

    async def test_created_before_inclusive(self, dated: RecordsStore) -> None:
        hits = await dated.search_documents("invoice", created_before="2026-02-10")
        assert sorted(_ids(hits)) == [1, 2]

    async def test_created_range(self, dated: RecordsStore) -> None:
        hits = await dated.search_documents(
            "invoice", created_after="2026-02-01", created_before="2026-02-28"
        )
        assert _ids(hits) == [2]

    async def test_tag_exact_filter(self, dated: RecordsStore) -> None:
        assert _ids(await dated.search_documents("invoice", tag="tax")) == [1]
        assert sorted(_ids(await dated.search_documents("invoice", tag="Purchase"))) == [1, 2]

    async def test_tag_filter_is_case_sensitive(self, dated: RecordsStore) -> None:
        assert await dated.search_documents("invoice", tag="TAX") == []

    async def test_limit_respected(self, dated: RecordsStore) -> None:
        assert len(await dated.search_documents("invoice", limit=2)) == 2
        assert len(await dated.search_documents("invoice")) == 3

    async def test_hit_shape(self, dated: RecordsStore) -> None:
        hit = (await dated.search_documents("jan"))[0]
        assert isinstance(hit, DocumentHit)
        assert hit.doc_id == 1
        assert hit.created == "2026-01-10"
        assert hit.tags == ("Purchase", "tax")
        assert isinstance(hit.snippet, str) and "jan" in hit.snippet


# --- Malformed queries / empty index ----------------------------------------


class TestMalformedAndEmpty:
    async def test_unclosed_group_retried_as_literal(self, store: RecordsStore) -> None:
        await store.upsert_document(_doc(1, content="this unbalanced quote thing came up"), now=NOW)
        hits = await store.search_documents("unbalanced (quote")
        assert _ids(hits) == [1]

    async def test_leading_double_quote_does_not_raise(self, store: RecordsStore) -> None:
        await store.upsert_document(_doc(1, content="this unbalanced quote thing came up"), now=NOW)
        hits = await store.search_documents('"unbalanced (quote')
        assert isinstance(hits, list)

    async def test_trailing_lone_double_quote_does_not_raise(self, store: RecordsStore) -> None:
        await store.upsert_document(_doc(1, content="unbalanced quote"), now=NOW)
        assert isinstance(await store.search_documents('unbalanced (quote "'), list)

    async def test_hopeless_query_raises(self, store: RecordsStore) -> None:
        await store.upsert_document(_doc(1, content="anything"), now=NOW)
        with pytest.raises(RecordsStoreError):
            await store.search_documents("   ")

    async def test_empty_index_returns_empty_list(self, store: RecordsStore) -> None:
        assert await store.search_documents("anything") == []
        assert await store.document_count() == 0


# --- Content cap -------------------------------------------------------------


class TestContentCap:
    async def test_content_truncated_with_marker(self, store: RecordsStore) -> None:
        assert CONTENT_MAX_CHARS == 500_000
        original_len = CONTENT_MAX_CHARS + 1000
        big = "x" * original_len
        await store.upsert_document(_doc(1, title="big", content=big), now=NOW)

        row = await store.get_document("paperless", "1")
        assert row is not None
        marker = f"\n\n[content truncated: original was {original_len} chars]"
        assert row.content.endswith(marker)
        assert row.content[:CONTENT_MAX_CHARS] == "x" * CONTENT_MAX_CHARS
        assert len(row.content) == CONTENT_MAX_CHARS + len(marker)

    async def test_content_under_cap_stored_verbatim(self, store: RecordsStore) -> None:
        text = "short body " * 10
        await store.upsert_document(_doc(1, content=text), now=NOW)
        row = await store.get_document("paperless", "1")
        assert row is not None
        assert row.content == text


# --- Cursor / get_document / counts ----------------------------------------


class TestCursorAndRows:
    async def test_cursor_roundtrip(self, store: RecordsStore) -> None:
        assert await store.get_cursor("paperless") is None
        await store.set_cursor("paperless", "2026-08-01T00:00:00Z", now=NOW)
        assert await store.get_cursor("paperless") == "2026-08-01T00:00:00Z"
        await store.set_cursor("paperless", "2026-08-02T00:00:00Z", now=NOW)
        assert await store.get_cursor("paperless") == "2026-08-02T00:00:00Z"
        await store.set_cursor("paperless", None, now=NOW)
        assert await store.get_cursor("paperless") is None

    async def test_cursor_is_per_source(self, store: RecordsStore) -> None:
        await store.set_cursor("paperless", "a", now=NOW)
        assert await store.get_cursor("gmail") is None

    async def test_get_document_returns_full_row(self, store: RecordsStore) -> None:
        row_in = _doc(
            5,
            title="Passport renewal receipt",
            created="2026-04-02",
            correspondent="State Dept",
            document_type="Receipt",
            tags=("travel", "Purchase"),
            content="paid 130 usd",
        )
        await store.upsert_document(row_in, now=NOW)
        row = await store.get_document("paperless", "5")
        assert row == row_in
        assert isinstance(row.tags, tuple)

    async def test_get_document_unknown_is_none(self, store: RecordsStore) -> None:
        assert await store.get_document("paperless", "404") is None

    async def test_document_count_by_source(self, store: RecordsStore) -> None:
        await store.upsert_document(_doc(1, content="a"), now=NOW)
        await store.upsert_document(_doc(2, content="b"), now=NOW)
        assert await store.document_count() == 2
        assert await store.document_count("paperless") == 2
        assert await store.document_count("gmail") == 0


# --- Read-only guarantee -----------------------------------------------------


class TestReadOnly:
    async def test_search_and_get_issue_zero_writes(self, store: RecordsStore) -> None:
        await store.upsert_document(_doc(1, content="read only wallaby check"), now=NOW)

        conn = None
        for attr in ("_c", "_conn", "_db", "_connection"):
            cand = getattr(store, attr, None)
            if cand is not None and hasattr(cand, "total_changes"):
                conn = cand
                break
        if conn is None:
            pytest.skip("RecordsStore does not expose its aiosqlite connection")

        before = conn.total_changes
        assert _ids(await store.search_documents("wallaby")) == [1]
        assert await store.search_documents("nothing-matches") == []
        assert (await store.get_document("paperless", "1")) is not None
        assert (await store.get_document("paperless", "2")) is None
        assert await store.get_cursor("paperless") is None
        assert conn.total_changes == before
