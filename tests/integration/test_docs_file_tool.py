"""`docs_file` (`tools/docs.py`, UoW-34) — with `FakePaperlessClient`, a real
indexer + `RecordsStore`, the runtime uploads layout
(`data/uploads/<conv>/<msg>/<file>`), an injected async no-op `sleep`, and
small poll budgets.

Properties under test:

- Path outside `data_dir/uploads/` → `ToolError` and zero `post_document`
  calls; `../` traversal that escapes uploads is rejected the same way.
- `.docx` rejected with a message mentioning "PDF" when `tika_enabled`
  is False; accepted (posted) when True.
- SUCCESS task → status "indexed", `doc_id` set, `url` set, row present
  in the store (searchable immediately).
- FAILURE whose message says duplicate → status "duplicate" with the
  `#<id>` parsed into `doc_id` (None when absent) — not an error.
- Any other FAILURE → `ToolError` carrying paperless's message.
- Never-finishing task → status "queued" with `task_id`, and the number
  of sleeps bounded by `poll_budget_s / poll_interval_s`.
- title / created / tags forwarded to `post_document`; tag names
  resolved via `ensure_tags` (unknown names created).
- The source file still exists afterwards (no GC introduced).
- Static: name `docs_file`, `requires_confirmation is False`, schema
  `{path required, title, tags: string[], created}`; registry validation.

Documented choices:
- Poll-budget accounting is asserted as bounded iteration count
  (`<= budget/interval + 1` sleeps) — i.e. the tool counts elapsed time
  by accumulated interval rather than reading a wall clock, which is the
  only accounting an injected no-op `sleep` can make deterministic.
- `title` in the "indexed" result is asserted equal to the document's
  title in paperless, which the test makes identical to the title
  passed in, so either reading (args vs. indexed row) satisfies it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ember.errors import ToolError, ToolValidationError
from ember.records.indexer import DocumentIndexer
from ember.records.paperless import TaskStatus
from ember.records.store import RecordsStore
from ember.tools.base import Tool
from ember.tools.docs import DocsFileTool
from ember.tools.registry import Registry
from tests.fakes.fake_paperless import FakePaperlessClient

PUBLIC = "http://localhost:9926"
BUDGET_S = 1.0
INTERVAL_S = 0.25
FIXED_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

PENDING = TaskStatus(status="PENDING", related_document=None, message="")
STARTED = TaskStatus(status="STARTED", related_document=None, message="")


def _success(doc_id: int) -> TaskStatus:
    return TaskStatus(
        status="SUCCESS",
        related_document=doc_id,
        message=f"Success. New document id {doc_id} created",
    )


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    uploads = data_dir / "uploads" / "thread-1" / "m1"
    uploads.mkdir(parents=True)
    (uploads / "receipt.pdf").write_bytes(b"%PDF-1.4 fake receipt")
    (uploads / "memo.docx").write_bytes(b"PK fake docx")
    (uploads / "photo.jpg").write_bytes(b"\xff\xd8 fake jpeg")
    return data_dir, uploads


@pytest.fixture
async def store(tmp_path: Path):
    s = RecordsStore(tmp_path / "records.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


class _Sleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def client() -> FakePaperlessClient:
    c = FakePaperlessClient()
    c.tag_id("utilities")
    return c


@pytest.fixture
def sleeper() -> _Sleeper:
    return _Sleeper()


def _tool(
    client: FakePaperlessClient,
    store: RecordsStore,
    data_dir: Path,
    sleeper: _Sleeper,
    *,
    tika_enabled: bool = False,
) -> DocsFileTool:
    indexer = DocumentIndexer(
        client=client, store=store, exclude_tag="ember-exclude", now=lambda: FIXED_NOW
    )
    return DocsFileTool(
        client=client,
        indexer=indexer,
        data_dir=data_dir,
        public_url=PUBLIC,
        tika_enabled=tika_enabled,
        poll_budget_s=BUDGET_S,
        poll_interval_s=INTERVAL_S,
        sleep=sleeper,
    )


# --- Static surface --------------------------------------------------------------


class TestStaticSurface:
    def test_name_protocol_and_confirmation(self, client, store, layout, sleeper) -> None:
        data_dir, _ = layout
        tool = _tool(client, store, data_dir, sleeper)
        assert isinstance(tool, Tool)
        assert tool.name == "docs_file"
        assert tool.requires_confirmation is False

    def test_schema(self, client, store, layout, sleeper) -> None:
        data_dir, _ = layout
        s = _tool(client, store, data_dir, sleeper).input_schema
        props = s["properties"]
        assert set(s["required"]) == {"path"}
        assert props["path"]["type"] == "string"
        assert props["title"]["type"] == "string"
        assert props["created"]["type"] == "string"
        assert props["tags"]["type"] == "array"
        assert props["tags"]["items"]["type"] == "string"

    async def test_registry_validation(self, client, store, layout, sleeper) -> None:
        data_dir, _ = layout
        reg = Registry()
        reg.register(_tool(client, store, data_dir, sleeper))
        for args in (
            {},
            {"path": 1},
            {"path": "x", "tags": "utilities"},
            {"path": "x", "tags": [1]},
        ):
            with pytest.raises(ToolValidationError):
                await reg.call_tool("docs_file", args)
        assert client.post_calls == []


# --- Path guard ----------------------------------------------------------------------


class TestPathGuard:
    async def test_outside_uploads_rejected_without_http(
        self, client, store, layout, sleeper, tmp_path
    ) -> None:
        data_dir, _ = layout
        outside = tmp_path / "elsewhere.pdf"
        outside.write_bytes(b"%PDF")
        tool = _tool(client, store, data_dir, sleeper)
        with pytest.raises(ToolError):
            await tool.call({"path": str(outside)})
        assert client.post_calls == []
        assert client.get_task_calls == 0
        assert client.ensure_tags_calls == []

    async def test_data_dir_but_not_uploads_rejected(self, client, store, layout, sleeper) -> None:
        data_dir, _ = layout
        f = data_dir / "records-backup.pdf"
        f.write_bytes(b"%PDF")
        tool = _tool(client, store, data_dir, sleeper)
        with pytest.raises(ToolError):
            await tool.call({"path": str(f)})
        assert client.post_calls == []

    async def test_dotdot_traversal_rejected(self, client, store, layout, sleeper) -> None:
        data_dir, uploads = layout
        escape = data_dir / "escape.pdf"
        escape.write_bytes(b"%PDF")
        traversal = f"{uploads}/../../../escape.pdf"
        assert Path(traversal).resolve() == escape.resolve()  # sanity: it does escape
        tool = _tool(client, store, data_dir, sleeper)
        with pytest.raises(ToolError):
            await tool.call({"path": traversal})
        assert client.post_calls == []


# --- Extension allowlist ---------------------------------------------------------------


class TestExtensionAllowlist:
    async def test_docx_rejected_without_tika(self, client, store, layout, sleeper) -> None:
        data_dir, uploads = layout
        tool = _tool(client, store, data_dir, sleeper, tika_enabled=False)
        with pytest.raises(ToolError) as ei:
            await tool.call({"path": str(uploads / "memo.docx")})
        assert "PDF" in str(ei.value)
        assert client.post_calls == []

    async def test_docx_accepted_with_tika(self, client, store, layout, sleeper) -> None:
        data_dir, uploads = layout
        client.add_document(
            9, title="memo", content="memo body text", modified="2026-08-05T10:00:00Z"
        )
        client.script_task(_success(9))
        tool = _tool(client, store, data_dir, sleeper, tika_enabled=True)
        result = await tool.call({"path": str(uploads / "memo.docx")})
        assert result.output["status"] == "indexed"
        assert len(client.post_calls) == 1
        assert client.post_calls[0].path == uploads / "memo.docx"

    async def test_unknown_extension_rejected(self, client, store, layout, sleeper) -> None:
        data_dir, uploads = layout
        weird = uploads / "archive.zip"
        weird.write_bytes(b"PK")
        tool = _tool(client, store, data_dir, sleeper, tika_enabled=True)
        with pytest.raises(ToolError):
            await tool.call({"path": str(weird)})
        assert client.post_calls == []

    async def test_image_accepted(self, client, store, layout, sleeper) -> None:
        data_dir, uploads = layout
        client.add_document(11, title="photo", content="ocr text", modified="2026-08-05T10:00:00Z")
        client.script_task(_success(11))
        tool = _tool(client, store, data_dir, sleeper)
        result = await tool.call({"path": str(uploads / "photo.jpg")})
        assert result.output["status"] == "indexed"


# --- Task outcomes ---------------------------------------------------------------------


class TestOutcomes:
    async def test_success_indexes_immediately(self, client, store, layout, sleeper) -> None:
        data_dir, uploads = layout
        client.add_document(
            7,
            title="Water bill",
            content="city water quarterly charge 84.10",
            modified="2026-08-05T10:00:00Z",
            created="2026-08-01",
            tags=("utilities",),
        )
        client.script_task(PENDING, STARTED, _success(7))
        tool = _tool(client, store, data_dir, sleeper)
        result = await tool.call({"path": str(uploads / "receipt.pdf"), "title": "Water bill"})

        out = result.output
        assert result.is_error is False
        assert set(out) == {"status", "doc_id", "title", "url", "task_id", "note"}
        assert out["status"] == "indexed"
        assert out["doc_id"] == 7
        assert out["title"] == "Water bill"
        assert out["url"] == f"{PUBLIC}/documents/7/details"
        assert out["task_id"] == client.task_uuid
        assert isinstance(out["note"], str)
        # Two non-terminal polls → at least two sleeps before the SUCCESS poll.
        assert len(sleeper.calls) >= 2
        assert all(s == INTERVAL_S for s in sleeper.calls)

        row = await store.get_document("paperless", "7")
        assert row is not None
        assert row.tags == ("utilities",)
        assert [h.doc_id for h in await store.search_documents("quarterly")] == [7]

    async def test_duplicate_failure_parses_existing_id(
        self, client, store, layout, sleeper
    ) -> None:
        data_dir, uploads = layout
        client.script_task(
            TaskStatus(
                status="FAILURE",
                related_document=None,
                message="Not consuming receipt.pdf: It is a duplicate of receipt.pdf (#12)",
            )
        )
        tool = _tool(client, store, data_dir, sleeper)
        result = await tool.call({"path": str(uploads / "receipt.pdf")})
        out = result.output
        assert result.is_error is False
        assert out["status"] == "duplicate"
        assert out["doc_id"] == 12
        assert out["url"] == f"{PUBLIC}/documents/12/details"
        assert out["task_id"] == client.task_uuid

    async def test_duplicate_without_id(self, client, store, layout, sleeper) -> None:
        data_dir, uploads = layout
        client.script_task(
            TaskStatus(
                status="FAILURE", related_document=None, message="Not consuming: It is a DUPLICATE"
            )
        )
        tool = _tool(client, store, data_dir, sleeper)
        result = await tool.call({"path": str(uploads / "receipt.pdf")})
        assert result.output["status"] == "duplicate"
        assert result.output["doc_id"] is None

    async def test_other_failure_is_tool_error_with_message(
        self, client, store, layout, sleeper
    ) -> None:
        data_dir, uploads = layout
        client.script_task(
            TaskStatus(
                status="FAILURE",
                related_document=None,
                message="Not consuming receipt.pdf: File is password protected",
            )
        )
        tool = _tool(client, store, data_dir, sleeper)
        with pytest.raises(ToolError) as ei:
            await tool.call({"path": str(uploads / "receipt.pdf")})
        assert "password protected" in str(ei.value)

    async def test_never_finishing_task_is_queued_within_budget(
        self, client, store, layout, sleeper
    ) -> None:
        data_dir, uploads = layout
        client.script_task(PENDING)
        tool = _tool(client, store, data_dir, sleeper)
        result = await tool.call({"path": str(uploads / "receipt.pdf")})
        out = result.output
        assert out["status"] == "queued"
        assert out["task_id"] == client.task_uuid
        assert out["doc_id"] is None
        assert out["url"] is None
        assert isinstance(out["note"], str) and out["note"].strip()
        assert 1 <= len(sleeper.calls) <= int(BUDGET_S / INTERVAL_S) + 1, sleeper.calls
        assert client.get_task_calls >= 1
        assert await store.document_count() == 0


# --- Forwarding / side effects ------------------------------------------------------------


class TestForwarding:
    async def test_title_created_tags_forwarded(self, client, store, layout, sleeper) -> None:
        data_dir, uploads = layout
        client.add_document(
            8,
            title="Water bill",
            content="x",
            modified="2026-08-05T10:00:00Z",
            tags=("utilities", "new-tag"),
        )
        client.script_task(_success(8))
        tool = _tool(client, store, data_dir, sleeper)
        await tool.call(
            {
                "path": str(uploads / "receipt.pdf"),
                "title": "Water bill",
                "created": "2026-08-01",
                "tags": ["utilities", "new-tag"],
            }
        )
        assert len(client.post_calls) == 1
        call = client.post_calls[0]
        assert call.path == uploads / "receipt.pdf"
        assert call.title == "Water bill"
        assert call.created == "2026-08-01"
        assert client.ensure_tags_calls == [("utilities", "new-tag")]
        assert call.tag_ids == (client.tag_id("utilities"), client.tag_id("new-tag"))
        assert "new-tag" in client.tag_names.values()

    async def test_no_tags_means_no_ensure_tags_and_empty_ids(
        self, client, store, layout, sleeper
    ) -> None:
        data_dir, uploads = layout
        client.add_document(8, title="r", content="x", modified="2026-08-05T10:00:00Z")
        client.script_task(_success(8))
        tool = _tool(client, store, data_dir, sleeper)
        await tool.call({"path": str(uploads / "receipt.pdf")})
        call = client.post_calls[0]
        assert call.tag_ids == ()
        assert call.title is None
        assert call.created is None
        assert client.ensure_tags_calls in ([], [()])

    async def test_source_file_still_exists(self, client, store, layout, sleeper) -> None:
        data_dir, uploads = layout
        client.add_document(8, title="r", content="x", modified="2026-08-05T10:00:00Z")
        client.script_task(_success(8))
        tool = _tool(client, store, data_dir, sleeper)
        await tool.call({"path": str(uploads / "receipt.pdf")})
        assert (uploads / "receipt.pdf").exists()
        assert (uploads / "receipt.pdf").read_bytes() == b"%PDF-1.4 fake receipt"
