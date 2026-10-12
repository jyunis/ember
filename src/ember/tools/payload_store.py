"""Payload store (v15, FC-30 / TLC-26 / UoW-29).

Oversize bridge tool results become content-addressed references instead
of multi-KB dumps replayed into the model's context every turn. The
bridge invoker calls `should_store` on the serialized output; over the
threshold, `put` persists the full payload to `<root>/<ref>.json` and
returns a small deterministic envelope (excerpt + ref + note). The model
retrieves slices on demand via the `read_payload` tool (`get`).

Determinism (TLC-26, FC-19 lint precedent): NO clocks, NO randomness,
NO uuids anywhere in this module. The envelope is a pure function of the
serialized output — same output, byte-identical envelope, every time —
so a repeated tool result stays prompt-cache-stable. The ref is
`sha256(serialized)[:16]`, which also makes writes idempotent: an
existing file for a ref holds the same content by construction, so it is
never rewritten.

GC is deferred (doc #56): the flat content-addressed layout keeps a
future sweep trivial.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

# Envelope note — fixed text (byte-stable).
_NOTE = "output stored; call read_payload(ref) for more"

# Refs are sha256[:16] lowercase hex, and only that shape is ever looked
# up — this doubles as the path-traversal guard on `get`.
_REF_RE = re.compile(r"^[0-9a-f]{16}$")


class PayloadStore:
    """Content-addressed store for oversize serialized tool outputs."""

    def __init__(
        self,
        root: Path,
        *,
        threshold_bytes: int = 16384,
        excerpt_chars: int = 2048,
    ) -> None:
        self._root = root
        self._threshold_bytes = threshold_bytes
        self._excerpt_chars = excerpt_chars

    def should_store(self, serialized: str) -> bool:
        """True iff the UTF-8 byte length exceeds the threshold —
        strictly greater, so a payload of exactly `threshold_bytes`
        passes through inline (TLC-26 boundary)."""
        return len(serialized.encode("utf-8")) > self._threshold_bytes

    def put(self, tool_name: str, serialized: str) -> dict:
        """Persist `serialized` and return its envelope.

        The file is written only when absent (idempotent: same ref ⇒
        same content). `tool_name` is recorded inside the file for
        debugging/GC provenance but does NOT participate in addressing
        or in the envelope — the envelope is a pure function of the
        serialized output alone.
        """
        payload_bytes = serialized.encode("utf-8")
        ref = hashlib.sha256(payload_bytes).hexdigest()[:16]
        path = self._root / f"{ref}.json"
        if not path.exists():
            self._root.mkdir(parents=True, exist_ok=True)
            document = json.dumps(
                {"tool": tool_name, "content": serialized},
                ensure_ascii=False,
            )
            # Write-then-rename so a crash mid-write never leaves a
            # truncated file under a valid ref. The tmp name is derived
            # from the ref (deterministic — no tempfile randomness);
            # concurrent writers of the same ref write identical bytes,
            # so the race is benign.
            tmp = self._root / f"{ref}.json.tmp"
            tmp.write_text(document, encoding="utf-8")
            os.replace(tmp, path)
        return {
            "payload_ref": ref,
            "total_bytes": len(payload_bytes),
            "excerpt": serialized[: self._excerpt_chars],
            "note": _NOTE,
        }

    def get(self, ref: str, *, offset: int = 0, max_bytes: int = 16384) -> dict:
        """Read a slice of a stored payload.

        Returns `{"content": <slice>, "offset": <o>, "total_bytes": <N>}`
        where the slice is `max_bytes` of the payload's UTF-8 encoding
        starting at byte `offset` (decoded with partial code points at
        the cut edges dropped). Unknown/malformed ref → an error dict,
        never a raise (TLC-26).
        """
        if not isinstance(ref, str) or not _REF_RE.match(ref):
            return {"error": "unknown payload_ref"}
        path = self._root / f"{ref}.json"
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            serialized = document["content"]
        except (OSError, ValueError, TypeError, KeyError):
            return {"error": "unknown payload_ref"}

        payload_bytes = serialized.encode("utf-8")
        offset = max(0, int(offset))
        max_bytes = max(0, int(max_bytes))
        window = payload_bytes[offset : offset + max_bytes]
        return {
            "content": window.decode("utf-8", errors="ignore"),
            "offset": offset,
            "total_bytes": len(payload_bytes),
        }
