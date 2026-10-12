"""Tool-output pruner (UoW-19, FC-19, TLC-18).

Rewrites a `claude_cli` session jsonl in place, replacing stale
`tool_result` blocks with deterministic stubs per the policy table in
the architecture doc § Tool-output retention.

Runs immediately before each `--resume` invocation. Failures are
non-fatal: on any error the pruner returns a `PruneResult` with
`skipped_reason` set and the turn proceeds against the unpruned
session.

Pure-function core (`apply_policy`) is determinism-stable and used
directly in tests; the I/O wrapper (`prune_session_jsonl`) handles
the read/tmpfile/rename dance.

NOTE (FC-19): no clocks, randomness, PIDs, or UUIDs in this module.
Stubs MUST be byte-stable given `(tool_name, args, output)` so the
prompt cache survives across turns.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_log = logging.getLogger("ember.context.tool_pruner")


# Tool-name suffix matching: claude CLI sees our tools as
# `mcp__agent__<name>`. Match by suffix so we don't depend on the prefix.
_GENERATE_IMAGE = "generate_image"
_WEB_FETCH = "web_fetch"
_WEB_BROWSE = "web_browse"
_RSS_FETCH = "rss_fetch"
_GMAIL_READ = "gmail_read"
_HTTP_REQUEST = "http_request"

# Recognizes already-stubbed tool_result content. Used for idempotency:
# a second pass on the same session is a no-op.
_STUB_RE = re.compile(r"^\[(image|fetched|browsed|rss|email|http)[^\]]*\]$")
_HARD_CAP_SUFFIX_RE = re.compile(r"\[tool output capped: original was \d+KB\]\s*$")


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    keep_latest: int
    keep_under_kb: int | None = None  # None = no size exemption


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Per-tool retention rules + global ceilings.

    Tools without an entry in `tools` are kept verbatim. The global
    `session_ceiling_kb` is the cap on total stub-eligible content
    after the latest-N exemption is applied; over-cap content gets
    stubbed oldest-first.
    """

    tools: Mapping[str, ToolPolicy]
    session_ceiling_kb: int = 200


@dataclass(slots=True)
class PruneResult:
    tool_results_rewritten: int = 0
    bytes_reclaimed: int = 0
    skipped_reason: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


# Default policy table (architecture doc § Tool-output retention).
DEFAULT_POLICY = RetentionPolicy(
    tools={
        _GENERATE_IMAGE: ToolPolicy(keep_latest=2),
        _WEB_FETCH: ToolPolicy(keep_latest=3, keep_under_kb=16),
        _WEB_BROWSE: ToolPolicy(keep_latest=3, keep_under_kb=16),
        _RSS_FETCH: ToolPolicy(keep_latest=1),
        _GMAIL_READ: ToolPolicy(keep_latest=5),
        # http_request is the catch-all probe tool — short health-check
        # responses are tiny and worth keeping verbatim, but MCP/API
        # debugging often dumps multi-KB JSON catalogs that bloat fast.
        # Size exemption keeps the small probes; latest-3 keeps the
        # current debugging context.
        _HTTP_REQUEST: ToolPolicy(keep_latest=3, keep_under_kb=8),
    },
    session_ceiling_kb=200,
)


# --- Stub generation -------------------------------------------------------


def _truncate(s: str, n: int) -> str:
    """Byte-length-aware truncate by code points (good enough; Anthropic
    tokenizer is char-stable for ASCII)."""
    if len(s) <= n:
        return s
    return s[:n]


def _domain_of(url: str) -> str:
    try:
        parsed = urlparse(url)
        return parsed.hostname or url
    except Exception:
        return url


def _stub_generate_image(args: Mapping[str, Any], output: Mapping[str, Any]) -> str:
    prompt = _truncate(str(args.get("prompt") or ""), 80)
    path = output.get("path")
    if not path:
        paths = output.get("paths")
        if isinstance(paths, list) and paths:
            path = paths[0]
    if not path:
        path = "unknown"
    return f"[image: {prompt}, path={path}]"


def _stub_web_fetch(args: Mapping[str, Any], output: Mapping[str, Any], *, verb: str) -> str:
    final_url = output.get("url") or args.get("url") or "unknown"
    title = output.get("title") or ""
    if title:
        title_or_domain = _truncate(str(title), 80)
    else:
        title_or_domain = _domain_of(str(final_url))
    content = output.get("content") or ""
    n = len(content) if isinstance(content, str) else 0
    return f"[{verb} {final_url}: {title_or_domain}, {n}c elided]"


def _stub_rss_fetch(args: Mapping[str, Any], output: Mapping[str, Any]) -> str:
    feeds = output.get("feeds") or []
    feed_count = len(feeds) if isinstance(feeds, list) else 0
    items = 0
    if isinstance(feeds, list):
        for f in feeds:
            if isinstance(f, Mapping):
                fitems = f.get("items") or []
                if isinstance(fitems, list):
                    items += len(fitems)
    return f"[rss {feed_count} feeds: {items} items elided]"


def _stub_gmail_read(args: Mapping[str, Any], output: Mapping[str, Any]) -> str:
    sender = str(output.get("from") or "")
    sender = _truncate(sender, 60) if sender else "unknown sender"
    subject = str(output.get("subject") or "")
    subject = _truncate(subject, 100) if subject else "(no subject)"
    return f"[email from {sender}: {subject}]"


def _stub_http_request(args: Mapping[str, Any], output: Mapping[str, Any]) -> str:
    method = str(args.get("method") or output.get("method") or "GET").upper()
    final_url = output.get("url") or args.get("url") or "unknown"
    status = output.get("status_code")
    status_str = str(status) if status is not None else "?"
    body = output.get("body") or ""
    n = len(body) if isinstance(body, str) else 0
    return f"[http {method} {final_url}: {status_str}, {n}c elided]"


def _stub_generic(tool_name: str, content_len: int) -> str:
    """Fallback when output JSON parse failed and we only have raw bytes
    to work with. Used as the last-resort stub."""
    return f"[{tool_name}: {content_len}c elided]"


def _short_tool_name(full_name: str) -> str:
    """`mcp__agent__generate_image` → `generate_image`. Pass-through if
    the prefix isn't there."""
    if "__" in full_name:
        # Take the trailing segment — handles `mcp__agent__x`, `agent__x`, etc.
        return full_name.rsplit("__", 1)[-1]
    return full_name


def stub_for(
    tool_name: str,
    args: Mapping[str, Any],
    output: Any,
) -> str:
    """Pure function: produce a deterministic stub from `(tool_name, args, output)`.

    `output` is the parsed JSON of the tool_result content (or a
    `{"_raw": text}` dict when JSON parse failed). Per FC-19, no clocks,
    no randomness — same inputs produce byte-equal output across runs.
    """
    short = _short_tool_name(tool_name)
    if not isinstance(output, Mapping):
        # Pathological: output isn't an object. Use the generic form.
        raw = output if isinstance(output, str) else json.dumps(output, default=str)
        return _stub_generic(short, len(raw))

    if "_raw" in output and len(output) == 1:
        # JSON parse failed earlier; we have raw text only.
        raw = str(output.get("_raw") or "")
        return _stub_generic(short, len(raw))

    if short == _GENERATE_IMAGE:
        return _stub_generate_image(args or {}, output)
    if short == _WEB_FETCH:
        return _stub_web_fetch(args or {}, output, verb="fetched")
    if short == _WEB_BROWSE:
        return _stub_web_fetch(args or {}, output, verb="browsed")
    if short == _RSS_FETCH:
        return _stub_rss_fetch(args or {}, output)
    if short == _GMAIL_READ:
        return _stub_gmail_read(args or {}, output)
    if short == _HTTP_REQUEST:
        return _stub_http_request(args or {}, output)

    # Unknown tool — generic fallback.
    serialized = json.dumps(output, default=str)
    return _stub_generic(short, len(serialized))


# --- Already-stubbed detection (idempotency) ------------------------------


def _content_is_stubbed(text: str) -> bool:
    """Match the byte-locked stub patterns or the hard-cap suffix.
    A stubbed tool_result is left alone on subsequent passes."""
    stripped = text.strip()
    if _STUB_RE.match(stripped):
        return True
    if _HARD_CAP_SUFFIX_RE.search(stripped):
        return True
    return False


# --- jsonl event helpers --------------------------------------------------


def _iter_assistant_tool_uses(event: Mapping[str, Any]) -> list[tuple[str, str, Mapping[str, Any]]]:
    """Yield `(tool_use_id, tool_name, input_args)` for every tool_use
    block in an assistant event. Empty list if none."""
    if event.get("type") != "assistant":
        return []
    msg = event.get("message") or {}
    content = msg.get("content") or []
    out: list[tuple[str, str, Mapping[str, Any]]] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_use":
                tu_id = block.get("id")
                name = block.get("name") or ""
                inp = block.get("input") or {}
                if tu_id and isinstance(inp, Mapping):
                    out.append((str(tu_id), str(name), inp))
    return out


def _user_tool_result_block(event: Mapping[str, Any]) -> tuple[Mapping[str, Any], list[Any]] | None:
    """If event is a user tool_result event, return `(message, content_list)`.
    Otherwise None. The content_list is the same list referenced inside
    the message — mutating an entry mutates the event in place."""
    if event.get("type") != "user":
        return None
    msg = event.get("message") or {}
    content = msg.get("content") or []
    if not isinstance(content, list):
        return None
    has_tool_result = any(
        isinstance(b, Mapping) and b.get("type") == "tool_result" for b in content
    )
    if not has_tool_result:
        return None
    return (msg, content)


def _extract_text_from_tool_result_content(tr_content: Any) -> str:
    """tool_result.content is `list[{type:"text", text:"..."}]` in claude
    CLI sessions. Concatenate the text parts."""
    if isinstance(tr_content, str):
        return tr_content
    if not isinstance(tr_content, list):
        return ""
    parts: list[str] = []
    for c in tr_content:
        if isinstance(c, Mapping) and c.get("type") == "text":
            parts.append(str(c.get("text") or ""))
        elif isinstance(c, str):
            parts.append(c)
    return "".join(parts)


def _set_tool_result_text(tr_content: Any, new_text: str) -> Any:
    """Return a new content list with all text parts collapsed into a
    single text block carrying `new_text`. Non-text parts (rare; e.g.
    image refs) are preserved in order, with the single text block
    inserted at the position of the first text part — or appended if
    there were none."""
    if not isinstance(tr_content, list):
        return [{"type": "text", "text": new_text}]
    out: list[Any] = []
    inserted = False
    for c in tr_content:
        if isinstance(c, Mapping) and c.get("type") == "text":
            if not inserted:
                out.append({"type": "text", "text": new_text})
                inserted = True
            # drop subsequent text parts (collapsed)
        else:
            out.append(c)
    if not inserted:
        out.append({"type": "text", "text": new_text})
    return out


def _parse_output(text: str) -> Any:
    """Parse the tool_result text as JSON. On failure, wrap in
    `{"_raw": text}` so stub generators can still extract a length.
    """
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {"_raw": text}


# --- Core: apply_policy ---------------------------------------------------


@dataclass
class _ResultRef:
    """Mutable handle to a tool_result block within the events list."""

    event_index: int
    block_index_in_content: int
    tool_use_id: str
    tool_name: str  # short name (e.g. "web_fetch")
    args: Mapping[str, Any]
    text: str
    output: Any  # parsed JSON or {"_raw": text}
    bytes_size: int  # len(text encoded utf-8)


def _walk_results(
    events: list[dict[str, Any]],
) -> tuple[dict[str, tuple[str, Mapping[str, Any]]], list[_ResultRef]]:
    """First pass: build `tool_use_id → (full_name, args)` and a list of
    every tool_result block in chronological order."""
    by_id: dict[str, tuple[str, Mapping[str, Any]]] = {}
    refs: list[_ResultRef] = []

    for i, ev in enumerate(events):
        for tu_id, name, inp in _iter_assistant_tool_uses(ev):
            by_id[tu_id] = (name, inp)

        u = _user_tool_result_block(ev)
        if u is None:
            continue
        _, content = u
        for j, block in enumerate(content):
            if not isinstance(block, Mapping):
                continue
            if block.get("type") != "tool_result":
                continue
            tu_id = str(block.get("tool_use_id") or "")
            if not tu_id or tu_id not in by_id:
                # Orphan tool_result — skip (we have no name to match policy against).
                continue
            full_name, args = by_id[tu_id]
            short = _short_tool_name(full_name)
            text = _extract_text_from_tool_result_content(block.get("content"))
            refs.append(
                _ResultRef(
                    event_index=i,
                    block_index_in_content=j,
                    tool_use_id=tu_id,
                    tool_name=short,
                    args=args,
                    text=text,
                    output=_parse_output(text),
                    bytes_size=len(text.encode("utf-8")),
                )
            )
    return by_id, refs


def _select_to_stub(
    refs: list[_ResultRef],
    policy: RetentionPolicy,
) -> list[_ResultRef]:
    """Return refs that should be replaced with a stub.

    Rules:
      1. Already-stubbed (text matches stub pattern) → never re-stub.
      2. Per-tool: keep latest `keep_latest` verbatim; also keep any
         whose bytes < `keep_under_kb * 1024` (size exemption).
      3. Global session ceiling: if total kept-verbatim bytes for
         stub-eligible tools exceeds `session_ceiling_kb * 1024`, stub
         additional oldest-first beyond the latest-N exemption.
    """
    to_stub: list[_ResultRef] = []

    # Bucket by tool_name in chronological order (refs already in order).
    buckets: dict[str, list[_ResultRef]] = {}
    for ref in refs:
        if _content_is_stubbed(ref.text):
            continue
        if ref.tool_name not in policy.tools:
            continue  # unknown tool — keep verbatim
        buckets.setdefault(ref.tool_name, []).append(ref)

    # Per-tool latest-N + size exemption.
    kept: list[_ResultRef] = []
    for tool_name, lst in buckets.items():
        tp = policy.tools[tool_name]
        latest_n = lst[-tp.keep_latest :] if tp.keep_latest > 0 else []
        latest_set = {id(r) for r in latest_n}
        for r in lst:
            if id(r) in latest_set:
                kept.append(r)
                continue
            if tp.keep_under_kb is not None and r.bytes_size < tp.keep_under_kb * 1024:
                kept.append(r)
                continue
            to_stub.append(r)

    # Global session ceiling: kept content beyond the ceiling gets stubbed
    # oldest-first. The latest-N exemption per tool is *not* respected
    # here — once we're over the global ceiling, even latest-N is fair
    # game. (Keeps the ceiling actually a ceiling.)
    ceiling_bytes = policy.session_ceiling_kb * 1024
    total_kept_bytes = sum(r.bytes_size for r in kept)
    if total_kept_bytes > ceiling_bytes:
        # Sort `kept` by event_index ascending = chronological oldest-first.
        kept_sorted = sorted(kept, key=lambda r: r.event_index)
        for r in kept_sorted:
            if total_kept_bytes <= ceiling_bytes:
                break
            to_stub.append(r)
            total_kept_bytes -= r.bytes_size
    return to_stub


def apply_policy(
    events: list[dict[str, Any]],
    policy: RetentionPolicy = DEFAULT_POLICY,
) -> tuple[list[dict[str, Any]], PruneResult]:
    """Pure-function core (FC-19).

    Returns `(rewritten_events, PruneResult)`. The input list is not
    mutated; returned events share unmodified inner objects with the
    input. Blocks that get stubbed are returned with a freshly-built
    content list.

    Determinism: same `(events, policy)` always produces byte-equal
    output. No clocks, no randomness.
    """
    _, refs = _walk_results(events)
    to_stub_list = _select_to_stub(refs, policy)
    to_stub_keys: set[tuple[int, int]] = {
        (r.event_index, r.block_index_in_content) for r in to_stub_list
    }

    if not to_stub_keys:
        return events, PruneResult(tool_results_rewritten=0, bytes_reclaimed=0)

    # Build a copy with stubbed blocks rewritten.
    out_events: list[dict[str, Any]] = []
    bytes_reclaimed = 0
    rewritten = 0

    # Quick lookup: ref by (event_index, block_index_in_content).
    ref_lookup: dict[tuple[int, int], _ResultRef] = {
        (r.event_index, r.block_index_in_content): r for r in refs
    }

    for i, ev in enumerate(events):
        # Only rewrite if this event has any stubbed block.
        relevant = [k for k in to_stub_keys if k[0] == i]
        if not relevant:
            out_events.append(ev)
            continue

        # Deep-copy just the message.content list (not the whole event).
        new_ev = dict(ev)
        msg = dict(ev.get("message") or {})
        old_content = msg.get("content") or []
        new_content = list(old_content) if isinstance(old_content, list) else []
        for _, j in relevant:
            ref = ref_lookup[(i, j)]
            block = new_content[j]
            if not isinstance(block, Mapping):
                continue
            new_block = dict(block)
            stub_text = stub_for(ref.tool_name, ref.args, ref.output)
            new_block["content"] = _set_tool_result_text(block.get("content"), stub_text)
            new_content[j] = new_block
            bytes_reclaimed += ref.bytes_size - len(stub_text.encode("utf-8"))
            rewritten += 1
        msg["content"] = new_content
        new_ev["message"] = msg
        out_events.append(new_ev)

    return out_events, PruneResult(
        tool_results_rewritten=rewritten,
        bytes_reclaimed=max(bytes_reclaimed, 0),
    )


# --- I/O wrapper ----------------------------------------------------------


def prune_session_jsonl(
    path: Path,
    policy: RetentionPolicy = DEFAULT_POLICY,
) -> PruneResult:
    """Read the session jsonl at `path`, apply the policy, write back
    via tmpfile + atomic rename.

    Never raises out (FC-19 failure semantics). All errors are returned
    as a `PruneResult` with `skipped_reason` set; the original file is
    left untouched.
    """
    try:
        if not path.exists():
            return PruneResult(skipped_reason="missing_session_file")
        raw_lines: list[str] = []
        events: list[dict[str, Any]] = []
        parse_failed_at: int | None = None
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                raw_lines.append(line)
                stripped = line.strip()
                if not stripped:
                    events.append({})  # placeholder so indices align
                    continue
                try:
                    events.append(json.loads(stripped))
                except json.JSONDecodeError:
                    parse_failed_at = line_no
                    break
        if parse_failed_at is not None:
            return PruneResult(skipped_reason=f"parse_error: line {parse_failed_at}")

        new_events, result = apply_policy(events, policy)
        if result.tool_results_rewritten == 0:
            return result

        # Serialize each event back to a single JSON line. Preserve
        # blank-line placeholders we represented as empty dicts above.
        new_lines: list[str] = []
        for orig_line, ev in zip(raw_lines, new_events, strict=False):
            if not orig_line.strip():
                new_lines.append(orig_line)
                continue
            # ensure_ascii=False matches claude CLI's serialization
            # (utf-8 chars stay literal). separators kept compact —
            # claude CLI doesn't pretty-print its session jsonl.
            new_lines.append(json.dumps(ev, ensure_ascii=False) + "\n")

        # Tmpfile + atomic rename.
        try:
            dirname = path.parent
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(dirname),
                prefix=f".{path.name}.",
                suffix=".prune-tmp",
                delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                tmp.writelines(new_lines)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(str(tmp_path), str(path))
        except OSError as e:
            # Clean up the tmpfile if rename failed but we already wrote it.
            try:
                if "tmp_path" in locals() and tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            return PruneResult(skipped_reason=f"fs_error: {e.errno}")

        return result
    except Exception as e:  # last-resort: never raise out
        _log.warning("tool_pruner: unexpected error: %s: %s", type(e).__name__, e)
        return PruneResult(skipped_reason=f"unexpected: {type(e).__name__}")
