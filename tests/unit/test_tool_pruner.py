"""Tool-output pruner (TLC-18 / UoW-19 / FC-19).

Golden-file-style tests over the pure-function core (`apply_policy`)
plus a few I/O-wrapper tests that exercise atomic rename + failure
modes. Each test builds a synthetic claude CLI session jsonl by hand —
mirrors the on-disk shape (one event per line, `assistant` events
carry `tool_use` blocks, `user` events carry `tool_result` blocks).

The pruner module is mechanical (one author wrote tests + impl per the
contracts-doc carve-out), so these tests are spec-pinned: same
`(events, policy)` always produces byte-equal output.
"""

from __future__ import annotations

import json
from pathlib import Path


from ember.context.tool_pruner import (
    DEFAULT_POLICY,
    RetentionPolicy,
    ToolPolicy,
    apply_policy,
    prune_session_jsonl,
    stub_for,
)


# --- helpers ---------------------------------------------------------------


def _assistant(tool_use_id: str, name: str, args: dict) -> dict:
    """Build an `assistant` event carrying a single tool_use block."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": name,
                    "input": args,
                }
            ],
        },
    }


def _user_result(tool_use_id: str, output: dict | str) -> dict:
    """Build a `user` event carrying a single tool_result block. The
    inner text is the JSON-stringified output (matches claude CLI's
    on-disk format)."""
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": [{"type": "text", "text": text}],
                }
            ],
        },
    }


def _make_pair(idx: int, tool: str, args: dict, output: dict | str) -> list[dict]:
    """Convenience: one assistant + one user event sharing a tool_use_id."""
    tu_id = f"toolu_{tool}_{idx:03d}"
    return [
        _assistant(tu_id, f"mcp__agent__{tool}", args),
        _user_result(tu_id, output),
    ]


def _result_text(event: dict) -> str:
    """Pull the tool_result content text out of a user event."""
    msg = event.get("message") or {}
    for block in msg.get("content") or []:
        if block.get("type") == "tool_result":
            for part in block.get("content") or []:
                if part.get("type") == "text":
                    return part.get("text") or ""
    return ""


# --- stub_for: byte-stability + projection ---------------------------------


def test_stub_for_generate_image_uses_prompt_and_path():
    s = stub_for(
        "mcp__agent__generate_image",
        {"prompt": "a giant turd with a birthday hat"},
        {"path": "/data/generated_images/abc.jpg", "bytes": 12345},
    )
    assert s == "[image: a giant turd with a birthday hat, path=/data/generated_images/abc.jpg]"


def test_stub_for_generate_image_truncates_long_prompt():
    long_prompt = "x" * 500
    s = stub_for(
        "mcp__agent__generate_image",
        {"prompt": long_prompt},
        {"path": "/p"},
    )
    # exactly 80 chars of prompt
    assert s == f"[image: {'x' * 80}, path=/p]"


def test_stub_for_generate_image_falls_back_when_path_missing():
    s = stub_for("mcp__agent__generate_image", {"prompt": "p"}, {"bytes": 1})
    assert s == "[image: p, path=unknown]"


def test_stub_for_generate_image_uses_paths_list_when_path_absent():
    s = stub_for(
        "mcp__agent__generate_image",
        {"prompt": "p"},
        {"paths": ["/a", "/b"]},
    )
    assert s == "[image: p, path=/a]"


def test_stub_for_web_fetch_uses_url_title_content_len():
    s = stub_for(
        "mcp__agent__web_fetch",
        {"url": "https://example.com/page"},
        {
            "url": "https://example.com/page",
            "title": "Example Page",
            "content": "x" * 3500,
        },
    )
    assert s == "[fetched https://example.com/page: Example Page, 3500c elided]"


def test_stub_for_web_fetch_falls_back_to_domain_when_no_title():
    s = stub_for(
        "mcp__agent__web_fetch",
        {"url": "https://example.com/page"},
        {"url": "https://example.com/page", "title": "", "content": "x" * 100},
    )
    assert s == "[fetched https://example.com/page: example.com, 100c elided]"


def test_stub_for_web_browse_uses_browsed_verb():
    s = stub_for(
        "mcp__agent__web_browse",
        {"url": "https://x.com"},
        {"url": "https://x.com", "title": "X", "content": "abc"},
    )
    assert s.startswith("[browsed ")


def test_stub_for_rss_fetch_counts_feeds_and_items():
    s = stub_for(
        "mcp__agent__rss_fetch",
        {"urls": ["a", "b"]},
        {
            "feeds": [
                {"url": "a", "items": [1, 2, 3]},
                {"url": "b", "items": [1, 2]},
            ]
        },
    )
    assert s == "[rss 2 feeds: 5 items elided]"


def test_stub_for_rss_fetch_handles_empty():
    s = stub_for("mcp__agent__rss_fetch", {}, {"feeds": []})
    assert s == "[rss 0 feeds: 0 items elided]"


def test_stub_for_gmail_read_uses_sender_subject():
    s = stub_for(
        "mcp__agent__gmail_read",
        {"uid": 42, "label": "INBOX"},
        {"from": "alice@example.com", "subject": "Hello", "body_text": "..."},
    )
    assert s == "[email from alice@example.com: Hello]"


def test_stub_for_gmail_read_uses_sentinels_when_missing():
    s = stub_for(
        "mcp__agent__gmail_read",
        {"uid": 1},
        {"body_text": "..."},
    )
    assert s == "[email from unknown sender: (no subject)]"


def test_stub_for_http_request_uses_method_url_status_body_len():
    s = stub_for(
        "mcp__agent__http_request",
        {"url": "https://api.example.com/v1/x", "method": "POST"},
        {
            "url": "https://api.example.com/v1/x",
            "method": "POST",
            "status_code": 201,
            "body": "x" * 4321,
        },
    )
    assert s == "[http POST https://api.example.com/v1/x: 201, 4321c elided]"


def test_stub_for_http_request_defaults_to_get_when_method_missing():
    s = stub_for(
        "mcp__agent__http_request",
        {"url": "https://x.com/health"},
        {"url": "https://x.com/health", "status_code": 200, "body": "ok"},
    )
    assert s == "[http GET https://x.com/health: 200, 2c elided]"


def test_stub_for_http_request_handles_missing_status():
    s = stub_for(
        "mcp__agent__http_request",
        {"url": "https://x.com", "method": "DELETE"},
        {"url": "https://x.com", "method": "DELETE", "body": ""},
    )
    assert s == "[http DELETE https://x.com: ?, 0c elided]"


def test_apply_policy_recognizes_http_stubs_for_idempotency():
    """Already-stubbed http_request results must not get re-stubbed."""
    policy = RetentionPolicy(tools={"http_request": ToolPolicy(keep_latest=0)})
    events = _make_pair(
        0,
        "http_request",
        {"url": "https://x.com"},
        "[http GET https://x.com: 200, 100c elided]",
    )
    _, result = apply_policy(events, policy)
    assert result.tool_results_rewritten == 0


def test_apply_policy_http_request_size_exemption_keeps_health_probes():
    """http_request policy: keep_latest=3 OR <8KB. Small probes survive."""
    policy = RetentionPolicy(tools={"http_request": ToolPolicy(keep_latest=3, keep_under_kb=8)})
    events = []
    # 5 small health-checks (way under 8KB) — all should survive.
    for i in range(5):
        events += _make_pair(
            i,
            "http_request",
            {"url": f"https://x{i}.com/health", "method": "GET"},
            {"url": f"https://x{i}.com/health", "status_code": 200, "body": '{"ok":true}'},
        )
    _, result = apply_policy(events, policy)
    assert result.tool_results_rewritten == 0


def test_apply_policy_http_request_stubs_oldest_chunky_responses():
    """Big http_request bodies past latest-3 get stubbed."""
    policy = RetentionPolicy(tools={"http_request": ToolPolicy(keep_latest=3, keep_under_kb=8)})
    big_body = "x" * (20 * 1024)  # 20KB — over the 8KB exemption
    events = []
    for i in range(6):
        events += _make_pair(
            i,
            "http_request",
            {"url": f"https://api.x.com/{i}", "method": "GET"},
            {"url": f"https://api.x.com/{i}", "status_code": 200, "body": big_body},
        )
    new_events, result = apply_policy(events, policy)
    # 6 chunky responses; latest 3 kept verbatim, oldest 3 stubbed.
    assert result.tool_results_rewritten == 3
    user_events = [e for e in new_events if e.get("type") == "user"]
    assert _result_text(user_events[0]).startswith("[http GET ")
    assert _result_text(user_events[2]).startswith("[http GET ")
    # Latest 3 kept JSON.
    assert json.loads(_result_text(user_events[5]))["status_code"] == 200


def test_stub_for_unknown_tool_uses_generic_form():
    s = stub_for("mcp__agent__some_other_tool", {}, {"result": "x" * 100})
    # `[<short_name>: <Nc> elided]`
    assert s.startswith("[some_other_tool: ")
    assert s.endswith("c elided]")


def test_stub_for_raw_text_fallback_when_output_unparseable():
    s = stub_for("mcp__agent__web_fetch", {}, {"_raw": "not json " * 50})
    # Generic form: tool_name + raw byte length
    assert s.startswith("[web_fetch: ")
    assert s.endswith("c elided]")


def test_stub_for_is_byte_stable():
    """Same inputs must produce byte-equal output across calls (FC-19)."""
    args = {"prompt": "test"}
    output = {"path": "/x"}
    a = stub_for("mcp__agent__generate_image", args, output)
    b = stub_for("mcp__agent__generate_image", args, output)
    assert a == b
    assert a.encode("utf-8") == b.encode("utf-8")


# --- apply_policy: latest-N exemption -------------------------------------


def test_apply_policy_keeps_latest_two_generate_images():
    policy = RetentionPolicy(tools={"generate_image": ToolPolicy(keep_latest=2)})
    events = []
    for i in range(5):
        events += _make_pair(i, "generate_image", {"prompt": f"p{i}"}, {"path": f"/img{i}.jpg"})

    new_events, result = apply_policy(events, policy)

    assert result.tool_results_rewritten == 3  # 5 - latest 2 = 3 stubbed
    # The latest two (indices 3 and 4) keep their JSON; earlier are stubs.
    user_events = [e for e in new_events if e.get("type") == "user"]
    assert _result_text(user_events[0]).startswith("[image: p0,")
    assert _result_text(user_events[1]).startswith("[image: p1,")
    assert _result_text(user_events[2]).startswith("[image: p2,")
    # Latest two retain JSON content.
    assert json.loads(_result_text(user_events[3]))["path"] == "/img3.jpg"
    assert json.loads(_result_text(user_events[4]))["path"] == "/img4.jpg"


def test_apply_policy_size_exemption_keeps_small_results():
    """web_fetch policy: keep_latest=3 OR result <16KB."""
    policy = RetentionPolicy(tools={"web_fetch": ToolPolicy(keep_latest=3, keep_under_kb=16)})
    events = []
    # 5 fetches: indices 0,1,2 are tiny; 3,4 are huge.
    for i in range(3):
        events += _make_pair(
            i,
            "web_fetch",
            {"url": f"https://x{i}.com"},
            {"url": f"https://x{i}.com", "title": "T", "content": "small"},
        )
    for i in range(3, 5):
        events += _make_pair(
            i,
            "web_fetch",
            {"url": f"https://x{i}.com"},
            {"url": f"https://x{i}.com", "title": "T", "content": "x" * 50_000},
        )
    new_events, result = apply_policy(events, policy)
    # All small fetches are under 16KB → kept regardless of latest-N.
    # Latest 3 fetches (indices 2,3,4) are also kept.
    # So indices 0, 1 are kept (small), 2 is kept (latest-3 + small),
    # 3 and 4 are kept (latest-3) — none get stubbed.
    assert result.tool_results_rewritten == 0


def test_apply_policy_unknown_tool_is_kept_verbatim():
    policy = RetentionPolicy(tools={"generate_image": ToolPolicy(keep_latest=0)})
    events = _make_pair(0, "some_random_tool", {"x": 1}, {"result": "y"})
    new_events, result = apply_policy(events, policy)
    assert result.tool_results_rewritten == 0
    assert new_events == events


# --- apply_policy: idempotency --------------------------------------------


def test_apply_policy_is_idempotent():
    policy = RetentionPolicy(tools={"generate_image": ToolPolicy(keep_latest=1)})
    events = []
    for i in range(4):
        events += _make_pair(i, "generate_image", {"prompt": f"p{i}"}, {"path": f"/{i}.jpg"})
    once, _ = apply_policy(events, policy)
    twice, second_result = apply_policy(once, policy)
    assert second_result.tool_results_rewritten == 0
    # Byte-equal on serialization.
    assert json.dumps(once) == json.dumps(twice)


def test_apply_policy_recognizes_existing_stubs():
    """A jsonl that's already been stubbed must not get re-stubbed."""
    policy = RetentionPolicy(tools={"web_fetch": ToolPolicy(keep_latest=0)})
    events = [
        # Already-stubbed tool_result (matches the regex).
        *_make_pair(0, "web_fetch", {"url": "x"}, "[fetched https://x.com: x.com, 100c elided]"),
    ]
    _, result = apply_policy(events, policy)
    assert result.tool_results_rewritten == 0


def test_apply_policy_recognizes_hard_cap_suffix():
    policy = RetentionPolicy(tools={"web_fetch": ToolPolicy(keep_latest=0)})
    events = [
        *_make_pair(
            0,
            "web_fetch",
            {"url": "x"},
            "{...lots of stuff...}\n\n[tool output capped: original was 2048KB]",
        ),
    ]
    _, result = apply_policy(events, policy)
    assert result.tool_results_rewritten == 0


# --- apply_policy: global session ceiling ---------------------------------


def test_apply_policy_session_ceiling_stubs_oldest_first():
    """Total kept-verbatim bytes > ceiling → stub additional oldest-first
    even if they'd otherwise be exempt by latest-N."""
    big_payload = "x" * (50 * 1024)  # 50KB each
    policy = RetentionPolicy(
        tools={"web_fetch": ToolPolicy(keep_latest=10)},  # large enough to exempt all
        session_ceiling_kb=100,  # 100KB ceiling — only ~2 fit
    )
    events = []
    for i in range(5):
        events += _make_pair(
            i,
            "web_fetch",
            {"url": f"https://x{i}.com"},
            {"url": f"https://x{i}.com", "title": "t", "content": big_payload},
        )
    _, result = apply_policy(events, policy)
    # 5 × ~50KB = 250KB; need to drop 150KB; oldest 3 get stubbed.
    assert result.tool_results_rewritten >= 3


# --- apply_policy: tool_use_id missing/orphan ------------------------------


def test_apply_policy_orphan_tool_result_kept_verbatim():
    """tool_result with no preceding tool_use → can't classify, leave alone."""
    events = [_user_result("toolu_orphan_xxx", {"some": "output"})]
    _, result = apply_policy(events, DEFAULT_POLICY)
    assert result.tool_results_rewritten == 0


# --- apply_policy: malformed output JSON ---------------------------------


def test_apply_policy_malformed_output_uses_raw_fallback():
    policy = RetentionPolicy(tools={"web_fetch": ToolPolicy(keep_latest=0)})
    events = _make_pair(
        0,
        "web_fetch",
        {"url": "https://x.com"},
        "this is not json at all, just raw text" * 100,
    )
    new_events, result = apply_policy(events, policy)
    assert result.tool_results_rewritten == 1
    user_events = [e for e in new_events if e.get("type") == "user"]
    text = _result_text(user_events[0])
    assert text.startswith("[web_fetch: ")
    assert "elided]" in text


# --- I/O wrapper: prune_session_jsonl -------------------------------------


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def test_prune_session_jsonl_writes_atomically(tmp_path: Path):
    policy = RetentionPolicy(tools={"generate_image": ToolPolicy(keep_latest=1)})
    events = []
    for i in range(3):
        events += _make_pair(i, "generate_image", {"prompt": f"p{i}"}, {"path": f"/{i}.jpg"})
    session = tmp_path / "test.jsonl"
    _write_jsonl(session, events)
    inode_before = session.stat().st_ino

    result = prune_session_jsonl(session, policy)
    assert result.skipped_reason is None
    assert result.tool_results_rewritten == 2

    # Atomic rename: we should see a *different* inode after.
    inode_after = session.stat().st_ino
    assert inode_after != inode_before, "expected tmpfile + rename, got in-place rewrite"

    # No tmpfiles left behind.
    leftover = [p for p in tmp_path.iterdir() if ".prune-tmp" in p.name]
    assert leftover == []


def test_prune_session_jsonl_idempotent_on_disk(tmp_path: Path):
    policy = RetentionPolicy(tools={"generate_image": ToolPolicy(keep_latest=1)})
    events = []
    for i in range(3):
        events += _make_pair(i, "generate_image", {"prompt": f"p{i}"}, {"path": f"/{i}.jpg"})
    session = tmp_path / "test.jsonl"
    _write_jsonl(session, events)

    first = prune_session_jsonl(session, policy)
    after_first_text = session.read_text("utf-8")
    second = prune_session_jsonl(session, policy)
    after_second_text = session.read_text("utf-8")

    assert first.tool_results_rewritten == 2
    assert second.tool_results_rewritten == 0
    assert after_first_text == after_second_text


def test_prune_session_jsonl_missing_file_returns_skipped(tmp_path: Path):
    result = prune_session_jsonl(tmp_path / "nope.jsonl", DEFAULT_POLICY)
    assert result.skipped_reason == "missing_session_file"


def test_prune_session_jsonl_parse_error_returns_skipped(tmp_path: Path):
    session = tmp_path / "bad.jsonl"
    session.write_text("{good: 1}\nthis is not json\n", encoding="utf-8")
    result = prune_session_jsonl(session, DEFAULT_POLICY)
    assert result.skipped_reason is not None
    assert "parse_error" in result.skipped_reason


def test_prune_session_jsonl_empty_file_no_op(tmp_path: Path):
    session = tmp_path / "empty.jsonl"
    session.write_text("", encoding="utf-8")
    result = prune_session_jsonl(session, DEFAULT_POLICY)
    # Empty file: no tool_results to rewrite → 0 rewrites, no error.
    assert result.tool_results_rewritten == 0
    assert result.skipped_reason is None


def test_prune_session_jsonl_no_tool_results_no_op(tmp_path: Path):
    """Session with only assistant text + user text (no tool calls) is unchanged."""
    events = [
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hello!"}],
            },
        },
    ]
    session = tmp_path / "chatonly.jsonl"
    _write_jsonl(session, events)
    text_before = session.read_text("utf-8")
    result = prune_session_jsonl(session, DEFAULT_POLICY)
    assert result.tool_results_rewritten == 0
    text_after = session.read_text("utf-8")
    assert text_before == text_after  # unchanged when no rewrite happens
