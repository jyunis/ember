"""Document tools (`tools/docs.py`, UoW-35 / FC-36) — through a real `Registry`.

Properties under test:

- TLC-6: the registry rejects schema-invalid args (`limit=0`, missing
  `query`, wrong types, unknown properties, `max_chars` out of 500..12000)
  with `ToolValidationError` before the tool runs.
- `docs_search` result shape `{match, hits}`; each hit carries the FC-36
  fields and `url == <public_url>/documents/<id>/details` (trailing
  slash on `public_url` stripped).
- `match` is "exact" on hits; a zero-hit plain multi-word query is
  retried OR-joined and labeled "broadened"; single terms and explicit
  FTS syntax are never rewritten (empty result, still "exact").
- Empty / whitespace query → `ToolError`.
- `docs_fetch` pages by `offset`/`max_chars` with `next_offset` (None at
  the end); unknown id → `ToolError` naming the id.
- Size budgets: 15 hits with long titles/snippets and a 12000-char fetch
  both serialize (`json.dumps(ensure_ascii=False)`) under 16384 bytes.
- All four tools have `requires_confirmation is False`; `docs_sync` is
  in `DEFAULT_CATALOG_EXCLUDE`.

Documented choices:
- The "broadened but still empty" case follows the history precedent:
  a plain multi-word query that finds nothing either way is labeled
  "broadened" with an empty hit list.
- `docs_sync`'s schema is exercised only for the `full` flag's type; its
  behavior is the indexer's (covered in test_document_indexer.py).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ember.errors import ToolError, ToolValidationError
from ember.records.indexer import DocumentIndexer
from ember.records.store import RecordsStore
from ember.records.types import IndexedDocument
from ember.skills.router_prompt import DEFAULT_CATALOG_EXCLUDE
from ember.tools.base import Tool, ToolResult
from ember.tools.docs import DocsFetchTool, DocsFileTool, DocsSearchTool, DocsSyncTool
from ember.tools.registry import Registry
from tests.fakes.fake_paperless import FakePaperlessClient

PUBLIC = "http://localhost:9926"
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
HIT_KEYS = {
    "doc_id",
    "title",
    "created",
    "correspondent",
    "document_type",
    "tags",
    "snippet",
    "url",
}
FETCH_KEYS = {
    "doc_id",
    "title",
    "created",
    "correspondent",
    "document_type",
    "tags",
    "url",
    "content",
    "offset",
    "total_chars",
    "next_offset",
}


def _doc(
    doc_id: int,
    *,
    title: str = "Untitled",
    content: str = "",
    created: str | None = "2026-06-15",
    correspondent: str = "",
    document_type: str = "",
    tags: tuple[str, ...] = (),
) -> IndexedDocument:
    return IndexedDocument(
        doc_id=doc_id,
        source="paperless",
        title=title,
        created=created,
        added="2026-06-15T09:00:00Z",
        modified="2026-06-15T10:00:00Z",
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


@pytest.fixture
def registry(store: RecordsStore) -> Registry:
    reg = Registry()
    reg.register(DocsSearchTool(store=store, public_url=PUBLIC))
    reg.register(DocsFetchTool(store=store, public_url=PUBLIC))
    return reg


@pytest.fixture
async def seeded(store: RecordsStore) -> RecordsStore:
    await store.upsert_document(
        _doc(
            1,
            title="Summer bites note",
            content="I get these bites every summer and they itch badly",
            created="2026-07-04",
            correspondent="Dermatology Associates",
            document_type="Receipt",
            tags=("health", "Purchase"),
        ),
        now=NOW,
    )
    await store.upsert_document(
        _doc(
            2,
            title="Garden centre invoice",
            content="unrelated chatter about gardens",
            created="2026-05-01",
        ),
        now=NOW,
    )
    return store


# --- Static surface --------------------------------------------------------------


class TestStaticSurface:
    def test_all_four_unconfirmed_and_named(self, store: RecordsStore) -> None:
        client = FakePaperlessClient()
        indexer = DocumentIndexer(client=client, store=store, exclude_tag="ember-exclude")
        tools = [
            DocsSearchTool(store=store, public_url=PUBLIC),
            DocsFetchTool(store=store, public_url=PUBLIC),
            DocsFileTool(
                client=client,
                indexer=indexer,
                data_dir=Path("/nonexistent-data-dir"),
                public_url=PUBLIC,
            ),
            DocsSyncTool(indexer=indexer),
        ]
        assert [t.name for t in tools] == ["docs_search", "docs_fetch", "docs_file", "docs_sync"]
        for t in tools:
            assert isinstance(t, Tool)
            assert t.requires_confirmation is False
            assert isinstance(t.description, str) and t.description.strip()
            assert t.input_schema["type"] == "object"

    def test_docs_sync_is_catalog_excluded(self) -> None:
        assert "docs_sync" in DEFAULT_CATALOG_EXCLUDE

    def test_search_schema(self, store: RecordsStore) -> None:
        s = DocsSearchTool(store=store, public_url=PUBLIC).input_schema
        props = s["properties"]
        assert set(s["required"]) == {"query"}
        assert s["additionalProperties"] is False
        assert props["query"]["type"] == "string"
        assert props["limit"]["type"] == "integer"
        assert props["limit"]["minimum"] == 1
        assert props["limit"]["maximum"] == 15
        assert props["limit"].get("default") == 6
        for k in ("created_after", "created_before", "tag"):
            assert props[k]["type"] == "string"

    def test_fetch_schema(self, store: RecordsStore) -> None:
        s = DocsFetchTool(store=store, public_url=PUBLIC).input_schema
        props = s["properties"]
        assert set(s["required"]) == {"doc_id"}
        assert props["doc_id"]["type"] == "integer"
        assert props["offset"]["type"] == "integer"
        assert props["offset"]["minimum"] == 0
        assert props["max_chars"]["minimum"] == 500
        assert props["max_chars"]["maximum"] == 12000
        assert props["max_chars"].get("default") == 6000

    def test_sync_schema(self, store: RecordsStore) -> None:
        indexer = DocumentIndexer(client=FakePaperlessClient(), store=store, exclude_tag="x")
        s = DocsSyncTool(indexer=indexer).input_schema
        assert s["properties"]["full"]["type"] == "boolean"


# --- TLC-6 registry validation ---------------------------------------------------


class TestRegistryValidation:
    @pytest.mark.parametrize(
        "args",
        [
            {"query": "x", "limit": 0},
            {"query": "x", "limit": 16},
            {},
            {"query": 5},
            {"query": "x", "bogus": 1},
            {"query": "x", "tag": 3},
        ],
    )
    async def test_search_invalid_args(self, registry: Registry, args: dict) -> None:
        with pytest.raises(ToolValidationError):
            await registry.call_tool("docs_search", args)

    @pytest.mark.parametrize(
        "args",
        [
            {},
            {"doc_id": "1"},
            {"doc_id": 1, "max_chars": 100},
            {"doc_id": 1, "max_chars": 20000},
            {"doc_id": 1, "offset": -1},
        ],
    )
    async def test_fetch_invalid_args(self, registry: Registry, args: dict) -> None:
        with pytest.raises(ToolValidationError):
            await registry.call_tool("docs_fetch", args)


# --- docs_search -----------------------------------------------------------------


class TestDocsSearch:
    async def test_exact_hit_shape_and_url(self, registry: Registry, seeded: RecordsStore) -> None:
        result = await registry.call_tool("docs_search", {"query": "bites"})
        assert isinstance(result, ToolResult)
        assert result.is_error is False
        out = result.output
        assert out["match"] == "exact"
        assert len(out["hits"]) == 1
        hit = out["hits"][0]
        assert set(hit) == HIT_KEYS
        assert hit["doc_id"] == 1
        assert hit["title"] == "Summer bites note"
        assert hit["created"] == "2026-07-04"
        assert hit["correspondent"] == "Dermatology Associates"
        assert hit["document_type"] == "Receipt"
        assert hit["tags"] == ["health", "Purchase"]
        assert "bites" in hit["snippet"]
        assert hit["url"] == f"{PUBLIC}/documents/1/details"

    async def test_public_url_trailing_slash_stripped(self, seeded: RecordsStore) -> None:
        tool = DocsSearchTool(store=seeded, public_url=PUBLIC + "/")
        result = await tool.call({"query": "bites"})
        assert result.output["hits"][0]["url"] == f"{PUBLIC}/documents/1/details"

    async def test_zero_hit_multiword_is_broadened(
        self, registry: Registry, seeded: RecordsStore
    ) -> None:
        # AND-semantics miss: no document has all four words; OR finds doc 1.
        result = await registry.call_tool("docs_search", {"query": "itchy rash bug bites"})
        assert result.output["match"] == "broadened"
        assert [h["doc_id"] for h in result.output["hits"]] == [1]

    async def test_broadened_but_still_empty(
        self, registry: Registry, seeded: RecordsStore
    ) -> None:
        result = await registry.call_tool("docs_search", {"query": "zeppelin quasar marmalade"})
        assert result.output["match"] == "broadened"
        assert result.output["hits"] == []

    async def test_single_term_never_broadened(
        self, registry: Registry, seeded: RecordsStore
    ) -> None:
        result = await registry.call_tool("docs_search", {"query": "zeppelin"})
        assert result.output["match"] == "exact"
        assert result.output["hits"] == []

    @pytest.mark.parametrize(
        "query", ["zeppelin OR quasar", '"bug bites rash"', "bites NOT summer"]
    )
    async def test_explicit_fts_syntax_never_rewritten(
        self, registry: Registry, seeded: RecordsStore, query: str
    ) -> None:
        result = await registry.call_tool("docs_search", {"query": query})
        assert result.output["match"] == "exact"
        assert result.output["hits"] == []

    @pytest.mark.parametrize("query", ["", "   "])
    async def test_empty_query_is_tool_error(
        self, registry: Registry, seeded: RecordsStore, query: str
    ) -> None:
        with pytest.raises(ToolError):
            await registry.call_tool("docs_search", {"query": query})

    async def test_filters_pass_through(self, registry: Registry, seeded: RecordsStore) -> None:
        # Both docs mention nothing in common except the OR-broadening; use
        # a term present in both via metadata: 'invoice' is only in doc 2.
        r = await registry.call_tool(
            "docs_search", {"query": "bites", "created_after": "2026-07-04"}
        )
        assert [h["doc_id"] for h in r.output["hits"]] == [1]
        r = await registry.call_tool(
            "docs_search", {"query": "bites", "created_before": "2026-07-03"}
        )
        assert r.output["hits"] == []
        r = await registry.call_tool("docs_search", {"query": "bites", "tag": "health"})
        assert [h["doc_id"] for h in r.output["hits"]] == [1]
        r = await registry.call_tool("docs_search", {"query": "bites", "tag": "nope"})
        assert r.output["hits"] == []

    async def test_limit_default_and_explicit(
        self, registry: Registry, store: RecordsStore
    ) -> None:
        for i in range(1, 11):
            await store.upsert_document(
                _doc(i, title=f"doc {i}", content="common keyword here"), now=NOW
            )
        r = await registry.call_tool("docs_search", {"query": "keyword"})
        assert len(r.output["hits"]) == 6
        r = await registry.call_tool("docs_search", {"query": "keyword", "limit": 3})
        assert len(r.output["hits"]) == 3

    async def test_fifteen_long_hits_under_envelope_threshold(
        self, registry: Registry, store: RecordsStore
    ) -> None:
        vocab = [
            "statement",
            "quarterly",
            "amount",
            "reference",
            "account",
            "transaction",
            "merchant",
            "balance",
            "payment",
            "invoice",
        ]
        for i in range(1, 16):
            body_words = [vocab[(i + j) % len(vocab)] for j in range(300)]
            body_words[150] = "budgetterm"
            content = " ".join(body_words)
            title = f"Statement {i:02d} " + " ".join(["Longwordy Title Segment"] * 5)
            await store.upsert_document(
                _doc(
                    i,
                    title=title,
                    content=content,
                    correspondent="Some Reasonably Long Correspondent Name",
                    document_type="Bank Statement",
                    tags=("finance-records", "tax-year-2026", "Purchase"),
                ),
                now=NOW,
            )
        r = await registry.call_tool("docs_search", {"query": "budgetterm", "limit": 15})
        assert len(r.output["hits"]) == 15
        size = len(json.dumps(r.output, ensure_ascii=False).encode("utf-8"))
        assert size < 16384, size


# --- docs_fetch --------------------------------------------------------------------


class TestDocsFetch:
    @pytest.fixture
    async def long_doc(self, store: RecordsStore) -> str:
        content = "".join(chr(ord("a") + (i % 26)) for i in range(1500))
        await store.upsert_document(
            _doc(
                5,
                title="Long one",
                content=content,
                created="2026-03-03",
                correspondent="Acme",
                document_type="Manual",
                tags=("gear",),
            ),
            now=NOW,
        )
        return content

    async def test_default_fetch_returns_whole_short_doc(
        self, registry: Registry, long_doc: str
    ) -> None:
        r = await registry.call_tool("docs_fetch", {"doc_id": 5})
        out = r.output
        assert set(out) == FETCH_KEYS
        assert out["doc_id"] == 5
        assert out["title"] == "Long one"
        assert out["created"] == "2026-03-03"
        assert out["correspondent"] == "Acme"
        assert out["document_type"] == "Manual"
        assert out["tags"] == ["gear"]
        assert out["url"] == f"{PUBLIC}/documents/5/details"
        assert out["content"] == long_doc
        assert out["offset"] == 0
        assert out["total_chars"] == 1500
        assert out["next_offset"] is None

    async def test_paging(self, registry: Registry, long_doc: str) -> None:
        r = await registry.call_tool("docs_fetch", {"doc_id": 5, "max_chars": 500})
        assert r.output["content"] == long_doc[:500]
        assert r.output["offset"] == 0
        assert r.output["next_offset"] == 500

        r = await registry.call_tool("docs_fetch", {"doc_id": 5, "offset": 500, "max_chars": 500})
        assert r.output["content"] == long_doc[500:1000]
        assert r.output["offset"] == 500
        assert r.output["next_offset"] == 1000

        r = await registry.call_tool("docs_fetch", {"doc_id": 5, "offset": 1000, "max_chars": 500})
        assert r.output["content"] == long_doc[1000:1500]
        assert r.output["next_offset"] is None

        r = await registry.call_tool("docs_fetch", {"doc_id": 5, "offset": 1400, "max_chars": 500})
        assert r.output["content"] == long_doc[1400:]
        assert r.output["total_chars"] == 1500
        assert r.output["next_offset"] is None

    async def test_unknown_id_is_tool_error_naming_id(
        self, registry: Registry, long_doc: str
    ) -> None:
        with pytest.raises(ToolError) as ei:
            await registry.call_tool("docs_fetch", {"doc_id": 999})
        assert "999" in str(ei.value)

    async def test_twelve_thousand_char_fetch_under_envelope_threshold(
        self, registry: Registry, store: RecordsStore
    ) -> None:
        content = ("lorem ipsum dolor sit amet " * 1000)[:20000]
        await store.upsert_document(
            _doc(
                6,
                title="Big manual " + "x" * 100,
                content=content,
                correspondent="A" * 60,
                document_type="Manual",
                tags=("gear", "warranty", "kitchen"),
            ),
            now=NOW,
        )
        r = await registry.call_tool("docs_fetch", {"doc_id": 6, "max_chars": 12000})
        assert len(r.output["content"]) == 12000
        assert r.output["next_offset"] == 12000
        size = len(json.dumps(r.output, ensure_ascii=False).encode("utf-8"))
        assert size < 16384, size
