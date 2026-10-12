"""PayloadStore (v15) — FC-30 + TLC-26 + UoW-29.

Surface under test (doc #56):

    class PayloadStore:
        def __init__(self, root: Path, *, threshold_bytes: int = 16384,
                     excerpt_chars: int = 2048) -> None: ...
        def should_store(self, serialized: str) -> bool   # BYTE length > threshold
        def put(self, tool_name: str, serialized: str) -> dict  # envelope
        def get(self, ref: str, *, offset: int = 0, max_bytes: int = 16384) -> dict

- Envelope is a pure function of the serialized output:
  {"payload_ref": sha256(serialized)[:16], "total_bytes": N,
   "excerpt": first `excerpt_chars` chars, "note": fixed note} —
  byte-identical across repeats (TLC-26 determinism; no clocks /
  randomness / uuids).
- File write is idempotent: `<root>/<ref>.json`, existing file = same
  content, skip write.
- get() round-trips with offset/max_bytes slicing;
  unknown ref → {"error": "unknown payload_ref"} without raising.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ember.tools.payload_store import PayloadStore


def _ref_for(serialized: str) -> str:
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "payloads"


# --- should_store: exact byte-length boundary ------------------------------


class TestShouldStore:
    def test_at_threshold_is_false(self, root: Path) -> None:
        ps = PayloadStore(root, threshold_bytes=10)
        assert ps.should_store("x" * 10) is False

    def test_one_over_threshold_is_true(self, root: Path) -> None:
        ps = PayloadStore(root, threshold_bytes=10)
        assert ps.should_store("x" * 11) is True

    def test_under_threshold_is_false(self, root: Path) -> None:
        ps = PayloadStore(root, threshold_bytes=10)
        assert ps.should_store("x" * 9) is False

    def test_boundary_is_utf8_bytes_not_chars(self, root: Path) -> None:
        """'é' is 2 UTF-8 bytes: 6 chars → 12 bytes > 10 → store, even
        though the CHARACTER count is under the threshold."""
        ps = PayloadStore(root, threshold_bytes=10)
        s = "é" * 6
        assert len(s) == 6  # chars under threshold …
        assert len(s.encode("utf-8")) == 12  # … bytes over it
        assert ps.should_store(s) is True

    def test_multibyte_at_exact_byte_threshold_is_false(self, root: Path) -> None:
        ps = PayloadStore(root, threshold_bytes=10)
        s = "é" * 5  # exactly 10 bytes
        assert len(s.encode("utf-8")) == 10
        assert ps.should_store(s) is False

    def test_default_threshold_is_16384(self, root: Path) -> None:
        ps = PayloadStore(root)
        assert ps.should_store("x" * 16384) is False
        assert ps.should_store("x" * 16385) is True


# --- put: deterministic content-addressed envelope -------------------------


class TestPutEnvelope:
    def test_envelope_keys(self, root: Path) -> None:
        ps = PayloadStore(root, threshold_bytes=10, excerpt_chars=5)
        env = ps.put("web_fetch", "x" * 100)
        assert set(env.keys()) == {"payload_ref", "total_bytes", "excerpt", "note"}

    def test_ref_is_sha256_prefix_of_content(self, root: Path) -> None:
        ps = PayloadStore(root, threshold_bytes=10, excerpt_chars=5)
        serialized = "the quick brown fox " * 10
        env = ps.put("web_fetch", serialized)
        assert env["payload_ref"] == _ref_for(serialized)
        assert len(env["payload_ref"]) == 16

    def test_total_bytes_is_utf8_byte_count(self, root: Path) -> None:
        ps = PayloadStore(root, threshold_bytes=10, excerpt_chars=5)
        serialized = "é" * 40  # 40 chars, 80 bytes
        env = ps.put("web_fetch", serialized)
        assert env["total_bytes"] == 80

    def test_excerpt_is_first_n_chars(self, root: Path) -> None:
        ps = PayloadStore(root, threshold_bytes=10, excerpt_chars=8)
        serialized = "abcdefghijklmnop" * 10
        env = ps.put("web_fetch", serialized)
        assert env["excerpt"] == serialized[:8]

    def test_default_excerpt_chars_is_2048(self, root: Path) -> None:
        ps = PayloadStore(root)
        serialized = "a" * 20_000
        env = ps.put("web_fetch", serialized)
        assert env["excerpt"] == serialized[:2048]

    def test_note_is_fixed_and_mentions_read_payload(self, root: Path) -> None:
        ps = PayloadStore(root, threshold_bytes=10, excerpt_chars=5)
        env = ps.put("web_fetch", "y" * 50)
        assert isinstance(env["note"], str)
        assert "read_payload" in env["note"]

    def test_envelope_byte_identical_across_100_repeats(self, root: Path) -> None:
        """TLC-26 determinism: same output → byte-identical envelope,
        100×. Serialize with sort_keys so dict ordering can't hide a
        difference."""
        ps = PayloadStore(root, threshold_bytes=10, excerpt_chars=64)
        serialized = json.dumps({"content": "z" * 500, "title": "T"})
        first = json.dumps(ps.put("web_fetch", serialized), sort_keys=True).encode("utf-8")
        for _ in range(99):
            again = json.dumps(ps.put("web_fetch", serialized), sort_keys=True).encode("utf-8")
            assert again == first

    def test_different_content_different_ref(self, root: Path) -> None:
        ps = PayloadStore(root, threshold_bytes=10, excerpt_chars=5)
        a = ps.put("web_fetch", "a" * 50)
        b = ps.put("web_fetch", "b" * 50)
        assert a["payload_ref"] != b["payload_ref"]


# --- put: idempotent file write --------------------------------------------


class TestPutFileWrite:
    def test_writes_ref_named_json_file(self, root: Path) -> None:
        ps = PayloadStore(root, threshold_bytes=10, excerpt_chars=5)
        serialized = "w" * 50
        env = ps.put("web_fetch", serialized)
        assert (root / f"{env['payload_ref']}.json").exists()

    def test_repeat_put_leaves_one_file(self, root: Path) -> None:
        ps = PayloadStore(root, threshold_bytes=10, excerpt_chars=5)
        serialized = "v" * 50
        ps.put("web_fetch", serialized)
        ps.put("web_fetch", serialized)
        ps.put("some_other_tool", serialized)  # same content, other tool
        files = list(root.glob("*.json"))
        assert len(files) == 1

    def test_repeat_put_still_round_trips(self, root: Path) -> None:
        ps = PayloadStore(root, threshold_bytes=10, excerpt_chars=5)
        serialized = "u" * 50
        env1 = ps.put("web_fetch", serialized)
        env2 = ps.put("web_fetch", serialized)
        assert env1 == env2
        got = ps.get(env2["payload_ref"])
        assert got["content"] == serialized


# --- get: round-trip + slicing ---------------------------------------------


class TestGet:
    def test_round_trip_full_content(self, root: Path) -> None:
        ps = PayloadStore(root, threshold_bytes=10, excerpt_chars=5)
        serialized = "0123456789" * 10  # 100 ASCII chars
        ref = ps.put("web_fetch", serialized)["payload_ref"]
        got = ps.get(ref)
        assert got["content"] == serialized
        assert got["offset"] == 0
        assert got["total_bytes"] == 100

    def test_offset_and_max_bytes_slice(self, root: Path) -> None:
        ps = PayloadStore(root, threshold_bytes=10, excerpt_chars=5)
        serialized = "abcdefghij" * 10  # ASCII → bytes == chars
        ref = ps.put("web_fetch", serialized)["payload_ref"]
        got = ps.get(ref, offset=5, max_bytes=3)
        assert got["content"] == serialized[5:8]
        assert got["offset"] == 5
        assert got["total_bytes"] == 100

    def test_max_bytes_past_end_returns_tail(self, root: Path) -> None:
        ps = PayloadStore(root, threshold_bytes=10, excerpt_chars=5)
        serialized = "k" * 50
        ref = ps.put("web_fetch", serialized)["payload_ref"]
        got = ps.get(ref, offset=45, max_bytes=1000)
        assert got["content"] == serialized[45:]
        assert got["offset"] == 45
        assert got["total_bytes"] == 50

    def test_default_max_bytes_is_16384(self, root: Path) -> None:
        ps = PayloadStore(root, threshold_bytes=10, excerpt_chars=5)
        serialized = "m" * 20_000
        ref = ps.put("web_fetch", serialized)["payload_ref"]
        got = ps.get(ref)
        assert got["content"] == serialized[:16384]
        assert got["total_bytes"] == 20_000

    def test_unknown_ref_returns_error_dict_without_raising(self, root: Path) -> None:
        ps = PayloadStore(root, threshold_bytes=10, excerpt_chars=5)
        got = ps.get("0" * 16)
        assert got == {"error": "unknown payload_ref"}
