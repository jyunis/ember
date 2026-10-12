"""`send_file` — post local files to the user as channel attachments.

The attachment plumbing already exists: any tool that returns paths in
`ToolResult.metadata["attachments"]` has them uploaded into the active
conversation right after the call — by the in-process agent loop
(`Agent._handle_turn_inner`) and by the MCP bridge invoker
(`bridge.active.forward_attachments`) alike. `generate_image` uses it
for the image it just wrote. This tool exposes that same path as a
first-class action so the model can attach *any* file it has: a CSV it
generated, a log it pulled, a document the user uploaded earlier.

Path discipline (same shape as `import_skill` / `generate_image`):
- every path must resolve under `data_dir` — the tool refuses to upload
  arbitrary host files;
- no path component may start with `.`, which keeps the container's
  credential/state dirs (`/data/.claude`, dotfiles) off the wire even
  though they live under `data_dir`;
- per-file and per-call byte caps, checked before anything is posted.

Validation is all-or-nothing: if any path fails, nothing is uploaded.
A partially-sent batch is worse than a clean error the model can fix.

Writing files to send: `<data_dir>/outbox/` exists for exactly that —
the model can `Write` there and then `send_file` the result.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ember.errors import ToolError
from ember.tools.base import ToolResult

_log = logging.getLogger("ember.tools.send_file")

OUTBOX_SUBDIR = "outbox"

# Discord's per-message upload limit for a non-boosted server is 10 MiB;
# stay a little under so the request itself (multipart overhead) fits.
MAX_FILE_BYTES = 9 * 1024 * 1024
MAX_TOTAL_BYTES = 9 * 1024 * 1024
MAX_FILES = 10


class SendFileTool:
    """Attach one or more files under `data_dir` to the current conversation."""

    name: str = "send_file"
    requires_confirmation: bool = False
    description: str = (
        "Attach local files to your reply so the user can download them. "
        "Paths must be absolute and resolve under /data (e.g. a file you "
        "wrote to /data/outbox/, a generated image, or something the user "
        "uploaded to /data/uploads/). Use this whenever the user asks you "
        "to send, share, or hand over a file rather than pasting its "
        "contents. The files are posted to the conversation as attachments "
        "immediately; describe them in your own reply text, since the "
        "upload itself carries no message."
    )

    def __init__(self, *, data_dir: Path) -> None:
        self._data_dir = Path(data_dir).resolve()
        self._outbox = self._data_dir / OUTBOX_SUBDIR
        self._outbox.mkdir(parents=True, exist_ok=True)
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": MAX_FILES,
                    "description": (
                        "Absolute paths of the files to attach. Each must "
                        "resolve under /data. Up to "
                        f"{MAX_FILES} files, {MAX_TOTAL_BYTES // (1024 * 1024)}MB total."
                    ),
                },
            },
            "required": ["paths"],
            "additionalProperties": False,
        }

    async def call(self, args: dict[str, Any]) -> ToolResult:
        raw_paths: list[Any] = list(args["paths"])

        resolved: list[Path] = []
        total = 0
        for raw in raw_paths:
            path = self._validate(raw)
            size = path.stat().st_size
            if size == 0:
                raise ToolError(f"send_file: {raw!r} is empty (0 bytes)")
            if size > MAX_FILE_BYTES:
                raise ToolError(
                    f"send_file: {raw!r} is {size} bytes, over the "
                    f"{MAX_FILE_BYTES}-byte per-file limit"
                )
            total += size
            if total > MAX_TOTAL_BYTES:
                raise ToolError(
                    f"send_file: attachments total {total} bytes, over the "
                    f"{MAX_TOTAL_BYTES}-byte per-call limit; send fewer files per call"
                )
            resolved.append(path)

        files = [{"path": str(p), "name": p.name, "bytes": p.stat().st_size} for p in resolved]
        _log.info("send_file: attaching %d file(s), %d bytes total", len(files), total)
        return ToolResult(
            output={"sent": len(files), "files": files, "bytes": total},
            metadata={"attachments": [str(p) for p in resolved]},
        )

    def _validate(self, raw: Any) -> Path:
        """Resolve `raw` and enforce the data-dir / dotfile / is-file rules."""
        if not isinstance(raw, str) or not raw.strip():
            raise ToolError("send_file: each entry in `paths` must be a non-empty string")
        path = Path(raw).expanduser().resolve()
        try:
            relative = path.relative_to(self._data_dir)
        except ValueError as e:
            raise ToolError(
                f"send_file: {raw!r} is outside {self._data_dir} — only files under "
                "/data can be attached"
            ) from e
        if any(part.startswith(".") for part in relative.parts):
            raise ToolError(f"send_file: {raw!r} is in a hidden path; refusing to attach it")
        if not path.is_file():
            raise ToolError(f"send_file: {raw!r} is not a file")
        return path
