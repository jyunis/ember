"""TLC-30 fail-closed amendment (2026-09-02, post-build, doc #59 Decision #16).

Property under test: a document carrying a tag id the API user cannot
resolve through `name_maps()` is NOT indexed — the exclusion check cannot
be evaluated, so the row is skipped and counted in `SyncReport.skipped`.

Why: paperless object permissions. A tag created by another paperless
user (e.g. the `ember-exclude` tag created from the owner's account) is
invisible to the `ember` API user; dropping the unknown id and indexing
the document would silently defeat the exclusion on exactly the documents
it exists for.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ember.records.indexer import DocumentIndexer
from ember.records.store import RecordsStore
from tests.fakes.fake_paperless import FakePaperlessClient

FIXED_NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


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
    c.add_document(1, title="Receipt", content="coffee 4 usd", modified="2026-08-01T10:00:00Z")
    c.add_document(
        2,
        title="Passport scan",
        content="identity numbers",
        modified="2026-08-02T10:00:00Z",
        tags=("ember-exclude",),
    )
    # Simulate the tag being invisible to the API user: the document still
    # references its id, but name_maps() no longer returns it.
    hidden_id = next(i for i, n in c.tag_names.items() if n == "ember-exclude")
    del c.tag_names[hidden_id]
    return c


def _indexer(client: FakePaperlessClient, store: RecordsStore) -> DocumentIndexer:
    return DocumentIndexer(
        client=client, store=store, exclude_tag="ember-exclude", now=lambda: FIXED_NOW
    )


async def test_sync_skips_document_with_unresolvable_tag(client, store) -> None:
    report = await _indexer(client, store).sync()
    assert await store.indexed_document_ids("paperless") == {"1"}
    assert report.skipped == 1
    assert report.upserted == 1
    assert report.as_dict()["skipped"] == 1


async def test_index_one_refuses_document_with_unresolvable_tag(client, store) -> None:
    with pytest.raises(ValueError, match="unresolvable tag id"):
        await _indexer(client, store).index_one(2)
    assert await store.indexed_document_ids("paperless") == set()


async def test_skipped_is_zero_when_all_tags_resolve(store) -> None:
    c = FakePaperlessClient()
    c.add_document(1, title="Receipt", content="x", modified="2026-08-01T10:00:00Z", tags=("a",))
    report = await _indexer(c, store).sync()
    assert report.skipped == 0
    assert await store.indexed_document_ids("paperless") == {"1"}
