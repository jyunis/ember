"""`send_file` — path discipline and the attachments contract.

The tool's whole job is to put validated paths in
`ToolResult.metadata["attachments"]`; the agent loop and the MCP bridge
both upload whatever lands there. So the tests that matter are (a) the
metadata shape, and (b) every way a path must be refused before it can
be uploaded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.errors import ToolError
from ember.tools.send_file import MAX_FILE_BYTES, OUTBOX_SUBDIR, SendFileTool


def _tool(data_dir: Path) -> SendFileTool:
    return SendFileTool(data_dir=data_dir)


def _write(path: Path, content: bytes = b"hello") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_creates_outbox_dir(tmp_path: Path) -> None:
    _tool(tmp_path)
    assert (tmp_path / OUTBOX_SUBDIR).is_dir()


@pytest.mark.asyncio
async def test_attaches_files_via_metadata(tmp_path: Path) -> None:
    a = _write(tmp_path / OUTBOX_SUBDIR / "report.csv", b"a,b\n1,2\n")
    b = _write(tmp_path / "uploads" / "photo.png", b"\x89PNG-ish")
    tool = _tool(tmp_path)

    result = await tool.call({"paths": [str(a), str(b)]})

    assert result.is_error is False
    assert result.metadata["attachments"] == [str(a), str(b)]
    assert result.output["sent"] == 2
    assert result.output["bytes"] == a.stat().st_size + b.stat().st_size
    assert [f["name"] for f in result.output["files"]] == ["report.csv", "photo.png"]


@pytest.mark.asyncio
async def test_rejects_path_outside_data_dir(tmp_path: Path) -> None:
    outside = _write(tmp_path.parent / "elsewhere" / "secret.txt")
    tool = _tool(tmp_path)

    with pytest.raises(ToolError, match="outside"):
        await tool.call({"paths": [str(outside)]})


@pytest.mark.asyncio
async def test_rejects_traversal_escape(tmp_path: Path) -> None:
    """`..` is resolved before the containment check, so it can't escape."""
    _write(tmp_path.parent / "escape.txt")
    tool = _tool(tmp_path)

    with pytest.raises(ToolError, match="outside"):
        await tool.call({"paths": [str(tmp_path / OUTBOX_SUBDIR / ".." / ".." / "escape.txt")]})


@pytest.mark.asyncio
async def test_rejects_symlink_pointing_outside(tmp_path: Path) -> None:
    target = _write(tmp_path.parent / "outside-target.txt")
    link = tmp_path / OUTBOX_SUBDIR
    link.mkdir(parents=True, exist_ok=True)
    (link / "sneaky.txt").symlink_to(target)
    tool = _tool(tmp_path)

    with pytest.raises(ToolError, match="outside"):
        await tool.call({"paths": [str(link / "sneaky.txt")]})


@pytest.mark.asyncio
async def test_rejects_hidden_paths(tmp_path: Path) -> None:
    """`/data/.claude` holds the CLI's auth state — never attachable."""
    creds = _write(tmp_path / ".claude" / "credentials.json", b"{}")
    tool = _tool(tmp_path)

    with pytest.raises(ToolError, match="hidden"):
        await tool.call({"paths": [str(creds)]})


@pytest.mark.asyncio
async def test_rejects_directory_and_missing_file(tmp_path: Path) -> None:
    tool = _tool(tmp_path)

    with pytest.raises(ToolError, match="not a file"):
        await tool.call({"paths": [str(tmp_path / OUTBOX_SUBDIR)]})
    with pytest.raises(ToolError, match="not a file"):
        await tool.call({"paths": [str(tmp_path / OUTBOX_SUBDIR / "nope.txt")]})


@pytest.mark.asyncio
async def test_rejects_empty_file(tmp_path: Path) -> None:
    empty = _write(tmp_path / OUTBOX_SUBDIR / "empty.txt", b"")
    tool = _tool(tmp_path)

    with pytest.raises(ToolError, match="empty"):
        await tool.call({"paths": [str(empty)]})


@pytest.mark.asyncio
async def test_rejects_oversized_file(tmp_path: Path) -> None:
    big = _write(tmp_path / OUTBOX_SUBDIR / "big.bin", b"x" * (MAX_FILE_BYTES + 1))
    tool = _tool(tmp_path)

    with pytest.raises(ToolError, match="per-file limit"):
        await tool.call({"paths": [str(big)]})


@pytest.mark.asyncio
async def test_validation_is_all_or_nothing(tmp_path: Path) -> None:
    """One bad path fails the call — no partial upload of the good ones."""
    good = _write(tmp_path / OUTBOX_SUBDIR / "good.txt")
    tool = _tool(tmp_path)

    with pytest.raises(ToolError):
        await tool.call({"paths": [str(good), str(tmp_path / OUTBOX_SUBDIR / "missing.txt")]})
