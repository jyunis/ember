"""DocumentIndexer (`records/indexer.py`, UoW-33 / TLC-29 / TLC-30) — with
`FakePaperlessClient` and a real `RecordsStore` over `tmp_path`.

Properties under test:

- Initial sync indexes every live document, reports the counts, and
  stores the cursor as the max `modified` seen.
- A second sync with no changes scans 0 and upserts 0 (cursor is
  `modified__gt`, so nothing re-scans).
- A modified document is re-indexed (new text searchable, old gone).
- A document carrying the exclusion tag (case-insensitive match on the
  configured name) is never indexed and counted `excluded`; when the tag
  is added later the row is removed on the next sync (TLC-30).
- A document deleted in paperless is removed by reconciliation; when
  enumeration fails, `reconciliation_skipped=True` and nothing is removed
  while upserts still happen (fail-safe).
- `full=True` on a fresh store reproduces the incremental path's rows
  exactly (`IndexedDocument` equality; `indexed_at` is not part of it).
- A bad row (`content` None / missing id) is skipped and the run completes.
- `index_one` returns "indexed" / "excluded"; an excluded doc that was
  previously indexed gets deleted.
- Tags/correspondent/document_type are resolved to NAMES in the stored row.
- TLC-29: none of the `records` modules import anything under
  `ember.providers`.

Documented choices:
- "Cursor == max modified" is asserted against the fake's ISO strings;
  string comparison equals chronological order for the fixed timestamps
  used here.
- For the bad-row case the fake's stored dict is mutated in place
  (`content = None`, and separately `del doc["id"]`); the assertion is
  only that the run completes and the other documents land.
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ember.records.indexer import DocumentIndexer
from ember.records.store import RecordsStore
from ember.records.types import IndexedDocument, SyncReport
from tests.fakes.fake_paperless import FakePaperlessClient

EXCLUDE = "ember-exclude"
T1 = "2026-08-01T10:00:00Z"
T2 = "2026-08-02T10:00:00Z"
T3 = "2026-08-03T10:00:00Z"
T4 = "2026-08-04T10:00:00Z"
FIXED_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
async def store(tmp_path: Path):
    s = RecordsStore(tmp_path / "records.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def client() -> FakePaperlessClient:
    c = FakePaperlessClient()
    c.add_document(
        1,
        title="Railway receipt",
        content="railway hosting charge 20 usd",
        modified=T1,
        created="2026-07-01",
        tags=("Purchase",),
        correspondent="Railway",
        document_type="Receipt",
    )
    c.add_document(
        2,
        title="Plumbing invoice",
        content="service call parts labor",
        modified=T2,
        created="2026-06-12",
        correspondent="Advantage Air Plumbing",
    )
    c.add_document(
        3,
        title="Drivers license",
        content="identity numbers",
        modified=T3,
        tags=("Ember-Exclude",),  # case differs from the configured name on purpose
    )
    return c


def _indexer(client: FakePaperlessClient, store: RecordsStore) -> DocumentIndexer:
    return DocumentIndexer(client=client, store=store, exclude_tag=EXCLUDE, now=lambda: FIXED_NOW)


async def _ids(store: RecordsStore) -> set[str]:
    return await store.indexed_document_ids("paperless")


# --- Initial / incremental sync ---------------------------------------------


class TestSync:
    async def test_initial_sync_indexes_all_and_sets_cursor(self, client, store) -> None:
        report = await _indexer(client, store).sync()
        assert isinstance(report, SyncReport)
        assert report.scanned == 3
        assert report.upserted == 2
        assert report.excluded == 1
        assert report.removed == 0
        assert report.reconciliation_skipped is False
        assert report.cursor == T3
        assert await store.get_cursor("paperless") == T3
        assert await _ids(store) == {"1", "2"}

    async def test_report_as_dict(self, client, store) -> None:
        report = await _indexer(client, store).sync()
        d = report.as_dict()
        assert d == {
            "scanned": 3,
            "upserted": 2,
            "excluded": 1,
            "removed": 0,
            "reconciliation_skipped": False,
            "cursor": T3,
            "skipped": 0,
        }

    async def test_second_sync_without_changes_is_noop(self, client, store) -> None:
        ix = _indexer(client, store)
        await ix.sync()
        report = await ix.sync()
        assert report.scanned == 0
        assert report.upserted == 0
        assert report.excluded == 0
        assert report.removed == 0
        assert await store.get_cursor("paperless") == T3
        # The second run asked paperless only for documents newer than the cursor.
        assert client.list_documents_calls[-1]["modified_after"] == T3

    async def test_modified_document_is_reindexed(self, client, store) -> None:
        ix = _indexer(client, store)
        await ix.sync()
        assert [h.doc_id for h in await store.search_documents("hosting")] == [1]

        client.docs[1]["content"] = "railway compute charge 25 usd"
        client.docs[1]["modified"] = T4
        report = await ix.sync()
        assert report.scanned == 1
        assert report.upserted == 1
        assert report.cursor == T4
        assert await store.search_documents("hosting") == []
        assert [h.doc_id for h in await store.search_documents("compute")] == [1]

    async def test_stored_row_uses_resolved_names(self, client, store) -> None:
        await _indexer(client, store).sync()
        row = await store.get_document("paperless", "1")
        assert row is not None
        assert row.tags == ("Purchase",)
        assert row.correspondent == "Railway"
        assert row.document_type == "Receipt"
        assert row.title == "Railway receipt"
        assert row.created == "2026-07-01"
        assert row.modified == T1
        assert row.source == "paperless"
        row2 = await store.get_document("paperless", "2")
        assert row2 is not None
        assert row2.correspondent == "Advantage Air Plumbing"
        assert row2.document_type == ""
        assert row2.tags == ()

    async def test_cursor_untouched_on_empty_paperless(self, store) -> None:
        empty = FakePaperlessClient()
        report = await _indexer(empty, store).sync()
        assert report.scanned == 0
        assert report.cursor is None
        assert await store.get_cursor("paperless") is None


# --- Exclusion (TLC-30) --------------------------------------------------------


class TestExclusion:
    async def test_excluded_tag_never_indexed_case_insensitive(self, client, store) -> None:
        await _indexer(client, store).sync()
        assert "3" not in await _ids(store)
        assert await store.search_documents("identity") == []
        assert await store.get_document("paperless", "3") is None

    async def test_tag_added_later_removes_row_on_next_sync(self, client, store) -> None:
        ix = _indexer(client, store)
        await ix.sync()
        assert "2" in await _ids(store)

        client.set_tags(2, (EXCLUDE,), modified=T4)
        report = await ix.sync()
        assert report.excluded == 1
        assert "2" not in await _ids(store)
        assert await store.search_documents("plumbing") == []


# --- Reconciliation ------------------------------------------------------------


class TestReconciliation:
    async def test_deleted_in_paperless_is_removed(self, client, store) -> None:
        ix = _indexer(client, store)
        await ix.sync()
        client.remove_document(1)
        report = await ix.sync()
        assert report.removed == 1
        assert report.reconciliation_skipped is False
        assert await _ids(store) == {"2"}

    async def test_failed_enumeration_removes_nothing(self, client, store) -> None:
        ix = _indexer(client, store)
        await ix.sync()
        client.remove_document(1)
        client.fail_enumeration = True
        client.add_document(4, title="New scan", content="fresh scan text", modified=T4)

        report = await ix.sync()
        assert report.reconciliation_skipped is True
        assert report.removed == 0
        assert report.upserted == 1
        assert report.scanned == 1
        assert await _ids(store) == {"1", "2", "4"}
        # The upsert committed and the cursor advanced regardless.
        assert await store.get_cursor("paperless") == T4

    async def test_enumeration_raising_other_exception_is_fail_safe(self, client, store) -> None:
        ix = _indexer(client, store)
        await ix.sync()
        client.remove_document(1)

        async def boom() -> list[int]:
            raise RuntimeError("unexpected")

        client.list_document_ids = boom  # type: ignore[method-assign]
        report = await ix.sync()
        assert report.reconciliation_skipped is True
        assert report.removed == 0
        assert "1" in await _ids(store)


# --- Full rebuild (TLC-29) -----------------------------------------------------


class TestFullRebuild:
    async def test_full_on_fresh_store_matches_incremental(self, client, tmp_path: Path) -> None:
        a = RecordsStore(tmp_path / "a.db")
        b = RecordsStore(tmp_path / "b.db")
        await a.open()
        await b.open()
        try:
            ix_a = _indexer(client, a)
            await ix_a.sync()
            client.docs[1]["content"] = "railway compute charge 25 usd"
            client.docs[1]["modified"] = T4
            await ix_a.sync()

            report_b = await _indexer(client, b).sync(full=True)
            assert report_b.scanned == 3
            assert report_b.cursor == T4
            assert await a.indexed_document_ids("paperless") == await b.indexed_document_ids(
                "paperless"
            )
            for sid in await a.indexed_document_ids("paperless"):
                ra = await a.get_document("paperless", sid)
                rb = await b.get_document("paperless", sid)
                assert isinstance(ra, IndexedDocument)
                assert ra == rb
        finally:
            await a.close()
            await b.close()

    async def test_full_ignores_cursor_but_writes_new_one(self, client, store) -> None:
        ix = _indexer(client, store)
        await ix.sync()
        report = await ix.sync(full=True)
        assert report.scanned == 3
        assert client.list_documents_calls[-1]["modified_after"] is None
        assert await store.get_cursor("paperless") == T3


# --- Bad rows ---------------------------------------------------------------------


class TestBadRows:
    async def test_none_content_row_is_skipped(self, client, store) -> None:
        client.docs[2]["content"] = None
        report = await _indexer(client, store).sync()
        assert "1" in await _ids(store)
        assert "2" not in await _ids(store)
        assert report.upserted == 1

    async def test_missing_id_row_is_skipped(self, client, store) -> None:
        del client.docs[2]["id"]
        report = await _indexer(client, store).sync()
        assert "1" in await _ids(store)
        assert "2" not in await _ids(store)
        assert report.upserted == 1


# --- index_one --------------------------------------------------------------------


class TestIndexOne:
    async def test_index_one_indexed(self, client, store) -> None:
        ix = _indexer(client, store)
        assert await ix.index_one(2) == "indexed"
        row = await store.get_document("paperless", "2")
        assert row is not None
        assert row.correspondent == "Advantage Air Plumbing"
        assert [h.doc_id for h in await store.search_documents("plumbing")] == [2]

    async def test_index_one_excluded(self, client, store) -> None:
        ix = _indexer(client, store)
        assert await ix.index_one(3) == "excluded"
        assert await store.get_document("paperless", "3") is None

    async def test_index_one_excluded_deletes_previous_row(self, client, store) -> None:
        ix = _indexer(client, store)
        assert await ix.index_one(2) == "indexed"
        client.set_tags(2, ("EMBER-EXCLUDE",), modified=T4)
        assert await ix.index_one(2) == "excluded"
        assert await store.get_document("paperless", "2") is None
        assert await store.search_documents("plumbing") == []


# --- TLC-29: no provider/router imports under records/ ---------------------------


class TestNoProviderImports:
    def test_records_modules_do_not_reference_providers(self) -> None:
        mods = [
            importlib.import_module(name)
            for name in ("ember.records.indexer", "ember.records.store", "ember.records.paperless")
        ]
        for mod in mods:
            for value in vars(mod).values():
                if isinstance(value, types.ModuleType):
                    assert not value.__name__.startswith("ember.providers"), (
                        f"{mod.__name__} references {value.__name__}"
                    )
                    assert not value.__name__.startswith("ember.skills.router"), (
                        f"{mod.__name__} references {value.__name__}"
                    )
        assert "ember.records.indexer" in sys.modules
