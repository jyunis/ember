"""Retrospective skill — nightly learning-loop Layer 3 (UoW-22 / FC-22, v12).

Exercises the installed imperative skill end-to-end against a real `Store`
(via the `StoreLearningContext` façade the scheduler wires into `ctx.learning`)
and a `FakeProvider` standing in for the cheap-tier extraction call.

The store-level TLC-20 gate is pinned separately in `test_store_v12.py`
(implementer/tester barrier). This file covers the skill's own deterministic
behavior: evidence assembly (user messages + 👎 feedback only), routing into
the right proposal kinds, the deterministic failed-run → finding pass, dedup
on re-run, and the skip-and-count paths (malformed output, no cheap tier).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ember.providers.base import ToolCall
from ember.scheduler.base import RunContext
from ember.scheduler.learning import StoreLearningContext
from ember.store.repo import Store
from tests.fakes.fake_provider import FakeProvider

THUMBS_DOWN = "\U0001f44e"


def _skill_path() -> Path:
    """Locate the installed retrospective skill.

    `data/` is gitignored runtime state that lives only in the main
    worktree, so `parents[2]` misses it from any `.claude/worktrees/*`
    checkout — which made this whole module error at fixture setup and,
    via `cwt-merge.sh`, wedged the pre-merge test gate shut. Fall back to
    the common git dir's parent (the main worktree) when the local tree
    has no `data/`.
    Since `data/skills/` became tracked, the committed copy wins.
    """
    tracked = Path(__file__).resolve().parents[2] / "data" / "skills" / "retrospective.py"
    if tracked.exists():
        return tracked

    local = Path(__file__).resolve().parents[2] / "data" / "skills" / "_drafts"
    if (local / "retrospective.py").exists():
        return local / "retrospective.py"

    try:
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - no git
        return local / "retrospective.py"

    main_tree = Path(common).resolve().parent
    return main_tree / "data" / "skills" / "_drafts" / "retrospective.py"


_SKILL_PATH = _skill_path()


def _load_skill():
    spec = importlib.util.spec_from_file_location("retrospective_skill", _SKILL_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def retro():
    return _load_skill()


@pytest.fixture
async def store(tmp_path: Path):
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


def _ctx(
    store: Store, provider: FakeProvider | None, *, cheap_available: bool = True
) -> RunContext:
    """Build a RunContext with ctx.learning wired to the store and a router
    whose cheap tier resolves to `provider` (or raises when unavailable)."""

    class _Router:
        def resolve_cheap_tier(self):
            if not cheap_available:
                raise RuntimeError("cheap tier not configured")
            return SimpleNamespace(provider=provider, model="fake-haiku")

    return RunContext(
        skill="retrospective",
        run_name="nightly",
        scheduled_for=None,
        profile="",
        soul="",
        tools=None,  # type: ignore[arg-type]
        router=_Router(),  # type: ignore[arg-type]
        notify=None,  # type: ignore[arg-type]
        now=lambda: datetime.now(timezone.utc),
        learning=StoreLearningContext(store),
    )


# --- Skill metadata -------------------------------------------------------


class TestSkillMetadata:
    def test_schedule_spec_is_wellformed(self, retro) -> None:
        from ember.scheduler.base import ScheduleSpec

        assert retro.name == "retrospective"
        # v12.5 adds the weekly digest alongside the nightly retrospective.
        assert len(retro.scheduled_runs) == 2
        specs = {s["name"]: ScheduleSpec(**s) for s in retro.scheduled_runs}  # must not raise
        nightly = specs["nightly"]
        assert nightly.function == "retrospective"
        assert nightly.cron == "30 2 * * *"
        assert nightly.timezone == "America/Chicago"
        weekly = specs["weekly"]
        assert weekly.function == "weekly_digest"
        assert weekly.cron == "0 16 * * 0"  # Sunday 16:00
        assert weekly.timezone == "America/Chicago"

    def test_declares_no_tools(self, retro) -> None:
        # FC-22: no privileged tool surface; evidence via ctx.learning only.
        assert tuple(retro.tools) == ()


# --- Routing --------------------------------------------------------------


class TestRouting:
    async def test_full_extraction_routes_to_correct_kinds(self, store: Store, retro) -> None:
        conv = await store.get_or_create_conversation("discord", "t")
        uid = await store.append_message(conv.id, "user", "no, I meant the OTHER calendar")
        aid = await store.append_message(conv.id, "assistant", "here it is")
        await store.record_feedback(aid, THUMBS_DOWN)  # 👎 on the agent reply

        canned = json.dumps(
            {
                "user_facts": [
                    {"fact": "prefers the work calendar", "evidence_message_ids": [uid]}
                ],
                "friction_events": [
                    {
                        "kind": "thumbs_down",
                        "summary": "used the wrong calendar",
                        "evidence_message_ids": [uid],
                    }
                ],
                "recurring": [
                    {
                        "pattern": "switch calendars",
                        "occurrences": 2,
                        "proposed_structure": "trigger",
                        "evidence_message_ids": [uid],
                    }
                ],
            }
        )
        fp = FakeProvider()
        fp.queue_text(canned)

        res = await retro.retrospective(_ctx(store, fp))

        assert res["llm_calls"] == 1
        assert res["extractions_ok"] == 1
        props = await store.list_proposals()
        by_kind = {p.kind for p in props}
        # Profile lane retired 2026-07-15: `user_facts` in the extraction are
        # ignored (no profile proposal); only friction → finding and
        # recurring → trigger route.
        assert by_kind == {"trigger", "finding"}
        # Every message-evidence id is the USER message; the assistant row is
        # never cited (TLC-20). thumbs_down finding carries feedback evidence.
        for p in props:
            ev = json.loads(p.evidence_json)
            assert aid not in ev["messages"]
            if p.kind == "finding" and "thumbs_down" in p.summary:
                assert ev["feedback"] and ev["messages"] == [uid]

    async def test_failed_run_becomes_finding_without_llm(self, store: Store, retro) -> None:
        # No conversations, no user signal → zero LLM calls, but a failed run
        # still produces a deterministic finding.
        rid = await store.record_run_started(
            skill="daily_digest",
            function="send_digest",
            run_name="morning",
            scheduled_for=None,
            started_at=datetime.now(timezone.utc),
        )
        await store.record_run_finished(
            rid, finished_at=datetime.now(timezone.utc), status="error", error_text="boom"
        )
        fp = FakeProvider()
        res = await retro.retrospective(_ctx(store, fp))

        assert res["llm_calls"] == 0
        assert res["failed_runs_seen"] == 1
        props = await store.list_proposals(status="pending")
        findings = [p for p in props if p.kind == "finding"]
        assert len(findings) == 1
        assert json.loads(findings[0].evidence_json)["runs"] == [rid]

    async def test_silent_tool_error_becomes_finding_without_llm(self, store: Store, retro) -> None:
        # A tool call that failed inside an agent turn — and that the user
        # never complained about — is a deterministic finding (doc #36
        # Decisions #26). Evidence is the preceding USER turn, never the
        # assistant row carrying the call (TLC-20). The conversation is
        # active, so extraction would normally run; cheap tier unavailable →
        # skipped, proving the finding needs zero LLM calls.
        conv = await store.get_or_create_conversation("discord", "photos")
        uid = await store.append_message(conv.id, "user", "can you zoom in on the label?")
        await store.append_message(
            conv.id,
            "assistant",
            "trying",
            tool_calls=[
                ToolCall(id="a", name="Read", args={"file_path": "/x.jpg"}, status="ok"),
                ToolCall(
                    id="b",
                    name="Bash",
                    args={"command": "cd /tmp && python3 -c 'from PIL import Image'"},
                    status="error",
                ),
                ToolCall(
                    id="c",
                    name="Bash",
                    args={"command": "PYTHONPATH=/x python3 -c 'import PIL'"},
                    status="error",
                ),
                ToolCall(
                    id="d", name="Bash", args={"command": "pip install pillow"}, status="error"
                ),
                ToolCall(id="e", name="vault_list", args={}, status="error"),
            ],
        )
        res = await retro.retrospective(_ctx(store, None, cheap_available=False))

        assert res["extractions_ok"] == 0
        assert res["conversations_skipped"] == 1
        assert res["tool_errors_seen"] == 4
        findings = [p for p in await store.list_proposals(status="pending") if p.kind == "finding"]
        by_payload = {
            (json.loads(p.payload_json)["tool"], json.loads(p.payload_json)["signature"]): p
            for p in findings
        }
        assert set(by_payload) == {("Bash", "python3"), ("Bash", "pip"), ("vault_list", None)}
        for p in findings:
            ev = json.loads(p.evidence_json)
            assert ev["messages"] == [uid]
            assert ev.get("feedback", []) == [] and ev.get("runs", []) == []
            assert p.status == "pending"
        assert "2×" in by_payload[("Bash", "python3")].summary
        assert by_payload[("vault_list", None)].summary.startswith(
            "silent tool error: `vault_list`"
        )

        # Second night, same tool failing again → reinforced, not duplicated.
        await store.append_message(conv.id, "user", "still broken?")
        await store.append_message(
            conv.id,
            "assistant",
            "x",
            tool_calls=[ToolCall(id="f", name="vault_list", args={}, status="error")],
        )
        res2 = await retro.retrospective(_ctx(store, None, cheap_available=False))
        assert res2["proposals_reinforced"] >= 1
        findings2 = [p for p in await store.list_proposals(status="pending") if p.kind == "finding"]
        assert len(findings2) == 3

    async def test_tool_error_without_user_turn_yields_no_finding(
        self, store: Store, retro
    ) -> None:
        # Nothing evidence-eligible to cite → skipped, never faked (TLC-20).
        conv = await store.get_or_create_conversation("discord", "bare")
        await store.append_message(
            conv.id,
            "assistant",
            "x",
            tool_calls=[ToolCall(id="a", name="Bash", args={"command": "false"}, status="error")],
        )
        res = await retro.retrospective(_ctx(store, None, cheap_available=False))
        assert res["tool_errors_seen"] == 1
        assert await store.list_proposals() == []


class TestToolErrorSignature:
    def test_bash_leading_program_after_prefixes(self, retro) -> None:
        sig = retro._tool_error_signature
        assert sig("Bash", {"command": "cd /tmp && python3 -c 'x'"}) == "python3"
        assert sig("Bash", {"command": "PYTHONPATH=/a/b python3 - <<'PY'\nx\nPY"}) == "python3"
        assert sig("Bash", {"command": "sudo env FOO=1 /usr/local/bin/pip install x"}) == "pip"
        assert sig("Bash", {"command": "pip install --quiet pillow 2>&1 | tail -3"}) == "pip"
        assert sig("Bash", {"command": ""}) is None
        assert sig("Bash", {}) is None

    def test_non_bash_tools_have_no_signature(self, retro) -> None:
        assert retro._tool_error_signature("vault_list", {}) is None
        assert retro._tool_error_signature("web_fetch", {"url": "https://x/y.pdf"}) is None


# --- v13 (C): lesson reinforcement + prune generation ---------------------


class TestLessonReinforcement:
    async def test_rederived_lesson_reinforces_live_row(self, store: Store, retro) -> None:
        from ember.identity.lessons import LessonsWriter

        conv = await store.get_or_create_conversation("discord", "t")
        uid = await store.append_message(conv.id, "user", "I always fix things rather than buy new")
        lid = await store.insert_lesson(
            text="prefers repairing over replacing",
            evidence={"messages": [uid]},
            dedup_key=LessonsWriter.dedup_key("prefers repairing over replacing"),
            created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        # The extractor re-derives the same lesson with different casing/punct.
        canned = json.dumps(
            {
                "user_facts": [],
                "friction_events": [],
                "recurring": [
                    {
                        "pattern": "Prefers Repairing Over Replacing.",
                        "occurrences": 3,
                        "proposed_structure": "lesson",
                        "evidence_message_ids": [uid],
                    }
                ],
            }
        )
        fp = FakeProvider()
        fp.queue_text(canned)
        res = await retro.retrospective(_ctx(store, fp))

        assert res["lessons_reinforced"] == 1
        # No duplicate lesson proposal was created for an already-live lesson.
        props = await store.list_proposals(status="pending")
        assert [p for p in props if p.kind == "lesson"] == []
        # The live row's freshness was bumped.
        assert (await store.get_lesson(lid)).reinforced_count == 1


# --- Idempotency (UoW-22) -------------------------------------------------


class TestRerunDedups:
    async def test_second_run_reinforces_not_duplicates(self, store: Store, retro) -> None:
        conv = await store.get_or_create_conversation("discord", "t")
        uid = await store.append_message(conv.id, "user", "always use metric units")
        # Dedup exercised on a recurring→trigger (the profile lane is retired).
        canned = json.dumps(
            {
                "friction_events": [],
                "recurring": [
                    {
                        "pattern": "convert everything to metric",
                        "occurrences": 2,
                        "proposed_structure": "trigger",
                        "evidence_message_ids": [uid],
                    }
                ],
            }
        )
        fp = FakeProvider()
        fp.queue_text(canned)
        fp.queue_text(canned)  # second night, same finding

        res1 = await retro.retrospective(_ctx(store, fp))
        res2 = await retro.retrospective(_ctx(store, fp))

        assert res1["proposals_created"] == 1
        assert res2["proposals_created"] == 0
        assert res2["proposals_reinforced"] == 1
        props = await store.list_proposals()
        assert len(props) == 1
        assert props[0].kind == "trigger"
        assert props[0].reinforced_count == 1


# --- Skip-and-count paths (TLC-13) ----------------------------------------


class TestSkipPaths:
    async def test_no_user_signal_skips_conversation_without_llm(self, store: Store, retro) -> None:
        # A conversation with only agent-authored rows and no feedback has no
        # eligible evidence → no extraction call, no proposals (the TLC-20
        # "all signal is agent-generated → zero proposals" property, at the
        # skill level).
        conv = await store.get_or_create_conversation("discord", "t")
        await store.append_message(conv.id, "assistant", "unprompted note")
        await store.append_message(conv.id, "tool", "tool output")
        fp = FakeProvider()
        res = await retro.retrospective(_ctx(store, fp))

        assert res["active_conversations"] == 1
        assert res["llm_calls"] == 0
        assert await store.list_proposals() == []

    async def test_malformed_output_skips_and_counts(self, store: Store, retro) -> None:
        conv = await store.get_or_create_conversation("discord", "t")
        await store.append_message(conv.id, "user", "hi there")
        fp = FakeProvider()
        fp.queue_text("not json at all {{{")
        res = await retro.retrospective(_ctx(store, fp))

        assert res["llm_calls"] == 1
        assert res["extractions_ok"] == 0
        assert res["conversations_skipped"] == 1
        assert await store.list_proposals() == []

    async def test_cited_ineligible_id_is_dropped(self, store: Store, retro) -> None:
        # The model cites an id NOT in the fed user-message set (here the
        # conversation's own assistant row — a real id, but agent-authored).
        # _route intersects citations with the fed user-message ids, so the
        # finding is left with no evidence and is not persisted. (record_proposal
        # would also reject the assistant id at the gate — defense in depth.)
        conv = await store.get_or_create_conversation("discord", "t")
        await store.append_message(conv.id, "user", "hello")
        aid = await store.append_message(conv.id, "assistant", "hi back")

        canned = json.dumps(
            {
                "friction_events": [],
                "recurring": [
                    {
                        "pattern": "bogus recurring need",
                        "occurrences": 2,
                        "proposed_structure": "trigger",
                        "evidence_message_ids": [aid],
                    }
                ],
            }
        )
        fp = FakeProvider()
        fp.queue_text(canned)
        res = await retro.retrospective(_ctx(store, fp))

        assert res["proposals_created"] == 0
        assert await store.list_proposals() == []

    async def test_no_cheap_tier_skips_extraction(self, store: Store, retro) -> None:
        conv = await store.get_or_create_conversation("discord", "t")
        await store.append_message(conv.id, "user", "something")
        res = await retro.retrospective(_ctx(store, None, cheap_available=False))

        assert res["conversations_skipped"] == 1
        assert res["extractions_ok"] == 0
        assert await store.list_proposals() == []


# --- Graceful degradation -------------------------------------------------


class TestNoLearningContext:
    async def test_missing_learning_context_returns_gracefully(self, store: Store, retro) -> None:
        # Bare scheduler wiring (no store) → ctx.learning is None → no-op.
        ctx = RunContext(
            skill="retrospective",
            run_name="nightly",
            scheduled_for=None,
            profile="",
            soul="",
            tools=None,  # type: ignore[arg-type]
            router=None,  # type: ignore[arg-type]
            notify=None,  # type: ignore[arg-type]
            now=lambda: datetime.now(timezone.utc),
            learning=None,
        )
        res = await retro.retrospective(ctx)
        assert res["status"] == "no_learning_context"


# --- Weekly digest (UoW-23) -----------------------------------------------


def _digest_ctx(store: Store, *, now, notes: list[str]) -> RunContext:
    """A RunContext whose ctx.notify appends to `notes` and whose clock is
    injected (for expiry testing)."""

    async def _notify(msg: str) -> None:
        notes.append(msg)

    return RunContext(
        skill="retrospective",
        run_name="weekly",
        scheduled_for=None,
        profile="",
        soul="",
        tools=None,  # type: ignore[arg-type]
        router=None,  # type: ignore[arg-type]
        notify=_notify,
        now=now,
        learning=StoreLearningContext(store),
    )


async def _seed_pending(store: Store, *, kind: str, summary: str, text: str) -> int:
    conv = await store.get_or_create_conversation("discord", f"k-{summary}")
    uid = await store.append_message(conv.id, "user", text)
    # The digest renders the payload prose (full text), not the short summary
    # label — carry the readable string in the payload so it shows up.
    pid, _ = await store.record_proposal(
        kind=kind, summary=summary, payload={"text": summary}, evidence={"messages": [uid]}
    )
    return pid


class TestWeeklyDigest:
    async def test_empty_queue_posts_one_line(self, store: Store, retro) -> None:
        notes: list[str] = []
        ctx = _digest_ctx(store, now=lambda: datetime(2026, 7, 6, tzinfo=timezone.utc), notes=notes)
        res = await retro.weekly_digest(ctx)
        assert res["posted"] is True
        assert res["pending"] == 0
        assert len(notes) == 1
        assert notes[0].strip().count("\n") == 0  # single line

    async def test_posts_pending_with_ids(self, store: Store, retro) -> None:
        p1 = await _seed_pending(store, kind="profile", summary="fact one", text="a")
        p2 = await _seed_pending(store, kind="finding", summary="friction two", text="b")
        notes: list[str] = []
        ctx = _digest_ctx(store, now=lambda: datetime(2026, 7, 6, tzinfo=timezone.utc), notes=notes)
        res = await retro.weekly_digest(ctx)
        assert res["pending"] == 2
        assert len(notes) == 1
        body = notes[0]
        assert f"#{p1}" in body and f"#{p2}" in body
        assert "fact one" in body and "friction two" in body

    async def test_expires_stale_then_posts_survivors(self, store: Store, retro) -> None:
        old = await _seed_pending(store, kind="lesson", summary="stale", text="old")
        fresh = await _seed_pending(store, kind="lesson", summary="fresh", text="new")
        # Age `old` past the 21-day TTL.
        await store._c.execute(
            "UPDATE proposals SET created_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00.000Z", old),
        )
        await store._c.commit()

        notes: list[str] = []
        now = datetime(2026, 7, 6, 16, 0, 0, tzinfo=timezone.utc)
        ctx = _digest_ctx(store, now=lambda: now, notes=notes)
        res = await retro.weekly_digest(ctx)

        assert old in res["expired_ids"]
        assert res["expired"] == 1
        # `old` is now terminal + silent (not announced), `fresh` still pending.
        assert (await store.get_proposal(old)).status == "expired"
        assert (await store.get_proposal(fresh)).status == "pending"
        body = notes[0]
        assert f"#{fresh}" in body
        assert f"#{old}" not in body

    async def test_generates_prune_candidate_for_stale_lesson(self, store: Store, retro) -> None:
        # v13 (C): a live lesson unreinforced >45d becomes a {op:'prune'} lesson
        # proposal in the digest.
        from ember.identity.lessons import LessonsWriter

        conv = await store.get_or_create_conversation("discord", "t")
        uid = await store.append_message(conv.id, "user", "x")
        lid = await store.insert_lesson(
            text="stale insight",
            evidence={"messages": [uid]},
            dedup_key=LessonsWriter.dedup_key("stale insight"),
            created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),  # >45d before now
        )
        notes: list[str] = []
        now = datetime(2026, 7, 6, 16, 0, 0, tzinfo=timezone.utc)
        ctx = _digest_ctx(store, now=lambda: now, notes=notes)
        res = await retro.weekly_digest(ctx)

        assert res["prune_candidates"] == 1
        props = await store.list_proposals(status="pending")
        prunes = [p for p in props if json.loads(p.payload_json).get("op") == "prune"]
        assert len(prunes) == 1
        assert json.loads(prunes[0].payload_json)["lesson_id"] == lid
        assert "prune" in notes[0].lower()

    async def test_fresh_lesson_not_pruned(self, store: Store, retro) -> None:
        from ember.identity.lessons import LessonsWriter

        conv = await store.get_or_create_conversation("discord", "t")
        uid = await store.append_message(conv.id, "user", "x")
        await store.insert_lesson(
            text="recent insight",
            evidence={"messages": [uid]},
            dedup_key=LessonsWriter.dedup_key("recent insight"),
            created_at=datetime(2026, 7, 5, tzinfo=timezone.utc),  # 1 day old
        )
        notes: list[str] = []
        now = datetime(2026, 7, 6, 16, 0, 0, tzinfo=timezone.utc)
        res = await retro.weekly_digest(_digest_ctx(store, now=lambda: now, notes=notes))
        assert res["prune_candidates"] == 0

    async def test_applied_delta_section_renders(self, store: Store, retro) -> None:
        # v13 (B): an applied change linked to a finished tone run shows a
        # before/after delta vs the previous comparable run.
        base = await store.create_eval_run(
            selector="tone",
            fingerprint="f0",
            identity_sha="id0",
            skills_hash="s",
            provider_route="claude_cli",
            image_version="img",
        )
        await store.finalize_eval_run(base, total_cases=3, passed=1, failed=2, errored=0, skipped=0)
        cur = await store.create_eval_run(
            selector="tone",
            fingerprint="f1",
            identity_sha="id1",
            skills_hash="s",
            provider_route="claude_cli",
            image_version="img",
        )
        await store.finalize_eval_run(cur, total_cases=3, passed=3, failed=0, errored=0, skipped=0)

        conv = await store.get_or_create_conversation("discord", "k-applied")
        uid = await store.append_message(conv.id, "user", "x")
        pid, _ = await store.record_proposal(
            kind="profile",
            summary="likes tea",
            payload={"fact": "likes tea"},
            evidence={"messages": [uid]},
        )
        now = datetime(2026, 7, 6, 16, 0, 0, tzinfo=timezone.utc)
        await store.approve_proposal(pid)
        await store.mark_applied(pid, now=now, applied_eval_run_id=cur)

        notes: list[str] = []
        ctx = _digest_ctx(store, now=lambda: now, notes=notes)
        res = await retro.weekly_digest(ctx)

        assert res["applied_measured"] == 1
        body = notes[0]
        assert "Applied since last digest" in body
        assert f"#{pid}" in body
        assert "pass +2" in body and "fail -2" in body
        assert "identity" in body  # the axis that moved

    async def test_missing_learning_context_no_op(self, store: Store, retro) -> None:
        ctx = RunContext(
            skill="retrospective",
            run_name="weekly",
            scheduled_for=None,
            profile="",
            soul="",
            tools=None,  # type: ignore[arg-type]
            router=None,  # type: ignore[arg-type]
            notify=None,  # type: ignore[arg-type]
            now=lambda: datetime.now(timezone.utc),
            learning=None,
        )
        res = await retro.weekly_digest(ctx)
        assert res["status"] == "no_learning_context"
