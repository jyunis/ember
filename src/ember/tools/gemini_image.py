"""Gemini image generation (Nano Banana).

Calls the Google AI Studio REST endpoint for `gemini-2.5-flash-image-preview`
to generate or edit images. Saves the result under `data_dir/generated_images/`
and surfaces the path via `ToolResult.metadata["attachments"]` so the
channel adapter can post it as a Discord attachment in the same turn.

Reference images are accepted as URLs (downloaded inline) or as paths
under `data_dir` (so the model can iterate on a previous generation by
passing the saved path back). External paths are rejected to keep the
tool from stat'ing arbitrary files.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from ember.errors import (
    AuthError,
    RateLimitError,
    ToolError,
    ToolRetryable,
    TransportError,
)
from ember.tools.base import ToolResult

_log = logging.getLogger("ember.tools.gemini_image")

_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_TIMEOUT_S = 60.0
_REFERENCE_FETCH_TIMEOUT_S = 15.0
_REFERENCE_MAX_BYTES = 8 * 1024 * 1024
_GENERATED_SUBDIR = "generated_images"


class GenerateImageTool:
    """Generate or edit an image with Gemini Nano Banana.

    Errors:
        AuthError       — HTTP 401/403 (api key invalid or model gated)
        RateLimitError  — HTTP 429
        ToolRetryable   — HTTP 5xx
        ToolError       — other 4xx, malformed response, no image returned
        TransportError  — network failure / timeout
    """

    name: str = "generate_image"
    description: str = (
        "Generate an image from a text prompt using Gemini's Nano Banana model. "
        "Optionally pass `reference_images` (URLs or saved paths) to edit, "
        "compose, or restyle existing images. Posts the result as a Discord "
        "attachment in the same turn and returns the saved path."
    )
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Description of the image to generate or edit.",
            },
            "reference_images": {
                "type": "array",
                "default": [],
                "items": {"type": "string"},
                "description": (
                    "Optional list of reference images. Each entry is either an "
                    "https:// URL or a path under the agent's data dir (e.g. a "
                    "previously generated image). Up to 4 references."
                ),
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    }

    def __init__(self, *, api_key: str, model: str, data_dir: Path) -> None:
        self._api_key = api_key
        self._model = model
        self._out_dir = data_dir / _GENERATED_SUBDIR
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir = data_dir.resolve()

    async def call(self, args: dict[str, Any]) -> ToolResult:
        prompt: str = args["prompt"]
        refs: list[str] = list(args.get("reference_images") or [])[:4]

        parts: list[dict[str, Any]] = [{"text": prompt}]
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            for r in refs:
                inline = await self._load_reference(client, r)
                parts.append({"inline_data": inline})

            url = f"{_API_BASE}/{self._model}:generateContent?key={self._api_key}"
            try:
                resp = await client.post(url, json={"contents": [{"parts": parts}]})
            except (httpx.TimeoutException, httpx.TransportError) as e:
                raise TransportError(f"gemini_image: {e}") from e

        if resp.status_code in (401, 403):
            raise AuthError(f"gemini_image: HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code == 429:
            raise RateLimitError(f"gemini_image: HTTP 429: {resp.text[:200]}")
        if 500 <= resp.status_code < 600:
            raise ToolRetryable(f"gemini_image: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise ToolError(f"gemini_image: HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            payload = resp.json()
        except ValueError as e:
            raise ToolError(f"gemini_image: malformed JSON: {e}") from e

        candidates = payload.get("candidates") or []
        if not candidates:
            # Surface prompt feedback (safety blocks, etc.) when present.
            feedback = payload.get("promptFeedback") or {}
            raise ToolError(f"gemini_image: no candidates returned. promptFeedback={feedback}")
        out_parts = ((candidates[0].get("content") or {}).get("parts")) or []
        image_part = next(
            (p for p in out_parts if "inline_data" in p or "inlineData" in p),
            None,
        )
        if image_part is None:
            text_part = next((p.get("text") for p in out_parts if "text" in p), None)
            raise ToolError(
                f"gemini_image: response had no inline image. text_response={text_part!r}"
            )
        inline = image_part.get("inline_data") or image_part.get("inlineData") or {}
        b64 = inline.get("data")
        mime = inline.get("mime_type") or inline.get("mimeType") or "image/png"
        if not b64:
            raise ToolError("gemini_image: inline image had no data")
        raw = base64.b64decode(b64)

        ext = "png" if "png" in mime else ("jpg" if "jpeg" in mime or "jpg" in mime else "bin")
        sha = hashlib.sha256(raw).hexdigest()[:16]
        ts = int(time.time())
        filename = f"{ts}-{sha}.{ext}"
        path = self._out_dir / filename
        path.write_bytes(raw)
        _log.info(
            "generate_image: saved %d bytes to %s (refs=%d)",
            len(raw),
            path,
            len(refs),
        )

        return ToolResult(
            output={
                "path": str(path),
                "mime": mime,
                "bytes": len(raw),
                "prompt": prompt,
            },
            metadata={"attachments": [str(path)]},
        )

    async def _load_reference(self, client: httpx.AsyncClient, ref: str) -> dict[str, str]:
        """Fetch URL or read local path; return a `{mime_type, data}` block.

        Local paths must resolve under `data_dir` to keep the tool from
        reading arbitrary files (e.g. /etc/passwd) when the model
        passes a path it shouldn't have access to.
        """
        if ref.startswith(("http://", "https://")):
            try:
                r = await client.get(ref, timeout=_REFERENCE_FETCH_TIMEOUT_S)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                raise TransportError(f"gemini_image: reference fetch failed: {e}") from e
            if r.status_code >= 400:
                raise ToolError(f"gemini_image: reference {ref!r} returned HTTP {r.status_code}")
            data = r.content
            mime = r.headers.get("content-type", "image/png").split(";")[0].strip()
        else:
            p = Path(ref).resolve()
            try:
                p.relative_to(self._data_dir)
            except ValueError as e:
                raise ToolError(f"gemini_image: reference path {ref!r} is outside data dir") from e
            if not p.is_file():
                raise ToolError(f"gemini_image: reference path {ref!r} not found")
            data = p.read_bytes()
            mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"

        if len(data) > _REFERENCE_MAX_BYTES:
            raise ToolError(
                f"gemini_image: reference {ref!r} too large ({len(data)} > {_REFERENCE_MAX_BYTES})"
            )
        return {"mime_type": mime, "data": base64.b64encode(data).decode("ascii")}
