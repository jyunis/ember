"""Row dataclasses for the records data plane (doc #59 FC-35).

Frozen + slotted, like `ember.store.types`. `IndexedDocument` is the
shape `RecordsStore.upsert_document` accepts and `get_document` returns;
`DocumentHit` is one `search_documents` row; `SyncReport` is what a
`DocumentIndexer.sync` run returns (and what the `docs_sync` tool emits
verbatim via `as_dict()`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class IndexedDocument:
    """One row of `documents`. `correspondent` / `document_type` are the
    resolved NAMES ('' when absent); `tags` are resolved names too."""

    doc_id: int
    source: str
    title: str
    created: str | None
    added: str | None
    modified: str | None
    correspondent: str
    document_type: str
    tags: tuple[str, ...]
    original_filename: str | None
    mime_type: str | None
    page_count: int | None
    content: str


@dataclass(frozen=True, slots=True)
class DocumentHit:
    """One bm25-ranked `docs_search` hit. `snippet` comes from `content`
    only (§ Data model)."""

    doc_id: int
    title: str
    created: str | None
    correspondent: str
    document_type: str
    tags: tuple[str, ...]
    snippet: str


@dataclass(frozen=True, slots=True)
class SyncReport:
    """UoW-33 step 7 result. `reconciliation_skipped` is True when the id
    enumeration failed or was partial — nothing was removed in that case
    (TLC-30 fail-safe)."""

    scanned: int
    upserted: int
    excluded: int
    removed: int
    reconciliation_skipped: bool
    cursor: str | None
    # Rows skipped as malformed — including the TLC-30 fail-closed case of
    # a tag id the API user cannot resolve (paperless object permissions).
    skipped: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "upserted": self.upserted,
            "excluded": self.excluded,
            "removed": self.removed,
            "reconciliation_skipped": self.reconciliation_skipped,
            "cursor": self.cursor,
            "skipped": self.skipped,
        }
