"""Proposal appliers + `/proposal` command (v12.5, FC-23 applier half, UoW-23).

Single-author (implementer) coverage of the apply path — the barrier tests
(`test_proposal_transitions.py`, `test_lessons_writer.py`) already pin the
behavior-sensitive state machine, cap, and injection; this file wires those
pieces through `ProposalApplier` and `ProposalCommand` end-to-end:

- approve a `profile` proposal → profile.md committed (git body references the
  proposal id), proposal → applied, announced;
- approve a `lesson` proposal → lessons.md created, committed, present in the
  next `system_prompt()`;
- approve a `lesson` that would exceed 8 KB → refused with a pruning hint, stays
  approved, lessons.md untouched;
- `reject` → terminal, gone from the pending queue;
- `finding` approve → informational (nothing applied), stays pending;
- `skill`/`trigger` approve → acknowledged as a manual-authoring idea (approved,
  not applied);
- `edit` → replaces payload then applies;
- unknown / non-pending id → typed hint listing pending ids.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ember.commands.base import CommandContext
from ember.commands.proposal import ProposalCommand
from ember.commands.registry import CommandRegistry
from ember.config import Settings
from ember.identity.lessons import LessonsWriter
from ember.identity.proposer import Proposer
from ember.identity.store import IdentityStore
from ember.identity.writer import ProfileWriter
from ember.learning.appliers import ProposalApplier
from ember.providers.base import CompletionResult, Usage
from ember.providers.routing import ProviderRouter
from ember.store.repo import Store
from tests.fakes.fake_provider import FakeProvider

_FIXED_NOW = datetime(2026, 7, 6, 16, 0, 0, tzinfo=UTC)


def _now() -> datetime:
    return _FIXED_NOW


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


def _router_with_cheap() -> tuple[ProviderRouter, FakeProvider]:
    primary = FakeProvider()
    primary.name = "claude_cli"
    cheap = FakeProvider()
    cheap.name = "cheap"
    router = ProviderRouter({"claude_cli": primary, "cheap": cheap}, cheap_tier_route="cheap")
    return router, cheap


def _queue_profile_render(cheap: FakeProvider, text: str) -> None:
    cheap.queue_response(CompletionResult(text=text, usage=Usage(1, 1, subscription=True)))


@pytest.fixture
async def wired(tmp_path: Path):
    store = Store(tmp_path / "conversations.db")
    await store.open()
    identity = IdentityStore(tmp_path / "identity")
    await identity.ensure_initialized()
    router, cheap = _router_with_cheap()
    applier = ProposalApplier(
        store=store,
        identity=identity,
        profile_writer=ProfileWriter(identity=identity),
        lessons_writer=LessonsWriter(identity=identity, store=store),
        proposer=Proposer(router=router),
        now=_now,
    )
    try:
        yield store, identity, applier, cheap
    finally:
        await store.close()


async def _seed(store: Store, *, kind: str, payload: dict, summary: str = "s") -> int:
    conv = await store.get_or_create_conversation("discord", f"k-{summary}-{kind}")
    uid = await store.append_message(conv.id, "user", "user signal")
    pid, _ = await store.record_proposal(
        kind=kind, summary=summary, payload=payload, evidence={"messages": [uid]}
    )
    return pid


def _git_log(identity: IdentityStore) -> str:
    import subprocess

    return subprocess.run(
        ["git", "log", "--format=%B"],
        cwd=identity._dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


# --- Command option ordering (Discord signature synthesis) --------------------


def test_required_options_precede_defaulted() -> None:
    # The Discord adapter synthesizes a function signature from `options` in
    # order; a required (no-default) option after a defaulted one raises
    # "non-default argument follows default argument" at slash registration.
    seen_default = False
    for opt in ProposalCommand.options:
        has_default = not opt.required
        if seen_default:
            assert has_default, f"required option {opt.name!r} follows a defaulted one"
        seen_default = seen_default or has_default


# --- ProposalApplier: profile -------------------------------------------------


class TestApplyProfile:
    async def test_profile_render_commit_and_mark_applied(self, wired) -> None:
        store, identity, applier, cheap = wired
        pid = await _seed(
            store,
            kind="profile",
            payload={"type": "user_fact", "fact": "The user owns an RV"},
            summary="user fact: RV",
        )
        rendered = identity.read_profile() + "\n## Sources / interests\n- Owns an RV\n"
        _queue_profile_render(cheap, rendered)

        approved = await store.approve_proposal(pid)
        outcome = await applier.apply(approved)

        assert outcome.applied is True
        assert outcome.status == "applied"
        assert "✅" in outcome.message
        # profile.md now carries the rendered text and git references the proposal.
        assert "Owns an RV" in identity.read_profile()
        assert f"proposal #{pid}" in _git_log(identity)
        assert f"proposal_id: {pid}" in _git_log(identity)
        # Terminal in the store.
        assert (await store.get_proposal(pid)).status == "applied"

    async def test_render_noop_still_marks_applied(self, wired) -> None:
        # Extractor returns the CURRENT profile unchanged → no commit, but the
        # fact is already reflected, so the proposal resolves as applied.
        store, identity, applier, cheap = wired
        pid = await _seed(
            store, kind="profile", payload={"type": "user_fact", "fact": "x"}, summary="noop"
        )
        _queue_profile_render(cheap, identity.read_profile())
        approved = await store.approve_proposal(pid)
        outcome = await applier.apply(approved)
        assert outcome.applied is True
        assert (await store.get_proposal(pid)).status == "applied"

    async def test_render_failure_leaves_approved(self, wired) -> None:
        # Cheap tier returns empty → render_updated_profile None → stays approved.
        store, identity, applier, cheap = wired
        pid = await _seed(
            store, kind="profile", payload={"type": "user_fact", "fact": "y"}, summary="fail"
        )
        _queue_profile_render(cheap, "")
        approved = await store.approve_proposal(pid)
        outcome = await applier.apply(approved)
        assert outcome.applied is False
        assert outcome.status == "approved"
        assert (await store.get_proposal(pid)).status == "approved"


# --- ProposalApplier: lesson --------------------------------------------------


class TestApplyLesson:
    async def test_lesson_appends_and_injects(self, wired) -> None:
        store, identity, applier, _cheap = wired
        pid = await _seed(
            store,
            kind="lesson",
            payload={"type": "recurring", "pattern": "prefers metric units"},
            summary="lesson: units",
        )
        approved = await store.approve_proposal(pid)
        outcome = await applier.apply(approved)

        assert outcome.applied is True
        lessons = identity.read_lessons()
        assert "prefers metric units" in lessons
        assert "2026-07-06" in lessons  # injected clock date
        # Present in the NEXT turn's system block, after profile (FC-24).
        sp = identity.system_prompt()
        assert "prefers metric units" in sp
        assert sp.index(identity.read_profile()) < sp.index(lessons)
        assert f"proposal #{pid}" in _git_log(identity)

    async def test_oversized_lesson_refused_stays_approved(self, wired) -> None:
        store, identity, applier, _cheap = wired
        # v13: the cap is enforced over the RENDER of the live lessons TABLE, not
        # over the file. Seed the table near the 8 KB cap (distinct dedup keys so
        # each is its own live row), then materialize the file from it.
        writer = applier._lessons_writer
        for i in range(15):
            txt = f"padding lesson number {i} " + "x" * 450
            await store.insert_lesson(
                text=txt,
                evidence={},
                dedup_key=writer.dedup_key(txt),
                created_at=_now(),
            )
        identity.write_lessons(writer.render(await store.live_lessons()))
        before = identity.read_lessons()
        assert len(before.encode()) < LessonsWriter.LESSONS_CAP_BYTES

        pid = await _seed(
            store,
            kind="lesson",
            payload={"type": "edited", "text": "z" * 2000},
            summary="huge",
        )
        approved = await store.approve_proposal(pid)
        outcome = await applier.apply(approved)

        assert outcome.applied is False
        assert outcome.status == "approved"
        assert outcome.error is not None and "prune" in outcome.error.lower()
        # Nothing inserted, file byte-unchanged.
        assert len(await store.live_lessons()) == 15
        assert identity.read_lessons() == before
        assert (await store.get_proposal(pid)).status == "approved"

    async def test_prune_lesson_soft_deletes_and_commits(self, wired) -> None:
        # v13 (C): a {op:'prune'} lesson proposal soft-deletes the row + rerenders.
        store, identity, applier, _cheap = wired
        conv = await store.get_or_create_conversation("discord", "kprune")
        uid = await store.append_message(conv.id, "user", "x")
        lid = await store.insert_lesson(
            text="an old lesson",
            evidence={"messages": [uid]},
            dedup_key=LessonsWriter.dedup_key("an old lesson"),
            created_at=_now(),
        )
        identity.write_lessons(applier._lessons_writer.render(await store.live_lessons()))
        assert "an old lesson" in identity.read_lessons()

        pid, _ = await store.record_proposal(
            kind="lesson",
            summary="🗑️ prune stale lesson: an old lesson",
            payload={"op": "prune", "lesson_id": lid, "lesson_text": "an old lesson"},
            evidence={"messages": [uid]},
        )
        approved = await store.approve_proposal(pid)
        outcome = await applier.apply(approved)

        assert outcome.applied is True
        assert "pruned" in outcome.message.lower()
        assert (await store.get_lesson(lid)).removed_at is not None
        assert "an old lesson" not in identity.read_lessons()
        assert (await store.get_proposal(pid)).status == "applied"
        assert f"proposal #{pid}" in _git_log(identity)

    async def test_prune_missing_lesson_stays_approved(self, wired) -> None:
        store, _identity, applier, _cheap = wired
        conv = await store.get_or_create_conversation("discord", "kprune2")
        uid = await store.append_message(conv.id, "user", "x")
        pid, _ = await store.record_proposal(
            kind="lesson",
            summary="prune ghost",
            payload={"op": "prune", "lesson_id": 999999, "lesson_text": "ghost"},
            evidence={"messages": [uid]},
        )
        approved = await store.approve_proposal(pid)
        outcome = await applier.apply(approved)
        assert outcome.applied is False
        assert (await store.get_proposal(pid)).status == "approved"


# --- ProposalApplier: v13 auto-eval on apply (UoW-24) ------------------------


class _FakeEvalService:
    """Records enqueue() calls and lets a test fire the run-created/complete
    hooks by hand — the applier should submit a kind-scoped job and link its
    run id at creation."""

    def __init__(self, *, accepted: bool = True) -> None:
        from ember.evals.service import EnqueueResult

        self.calls: list = []
        self._result = EnqueueResult(accepted=accepted, count=3, skipped=[], depth=0)

    def enqueue(self, selector, *, label, on_run_created=None, on_complete=None, on_error=None):
        from types import SimpleNamespace

        self.calls.append(
            SimpleNamespace(
                selector=selector,
                label=label,
                on_run_created=on_run_created,
                on_complete=on_complete,
            )
        )
        return self._result


class TestAutoEvalOnApply:
    @pytest.fixture
    async def wired_autoeval(self, tmp_path: Path):
        store = Store(tmp_path / "conversations.db")
        await store.open()
        identity = IdentityStore(tmp_path / "identity")
        await identity.ensure_initialized()
        router, cheap = _router_with_cheap()
        fake = _FakeEvalService()
        notes: list[str] = []

        async def _notify(msg: str) -> None:
            notes.append(msg)

        applier = ProposalApplier(
            store=store,
            identity=identity,
            profile_writer=ProfileWriter(identity=identity),
            lessons_writer=LessonsWriter(identity=identity, store=store),
            proposer=Proposer(router=router),
            now=_now,
            eval_service=fake,
            notify=_notify,
        )
        try:
            yield store, identity, applier, cheap, fake, notes
        finally:
            await store.close()

    async def test_profile_apply_fires_tone_eval_and_links_run(self, wired_autoeval) -> None:
        store, identity, applier, cheap, fake, notes = wired_autoeval
        pid = await _seed(
            store, kind="profile", payload={"type": "user_fact", "fact": "likes tea"}, summary="t"
        )
        _queue_profile_render(cheap, identity.read_profile() + "\n- likes tea\n")
        approved = await store.approve_proposal(pid)
        outcome = await applier.apply(approved)

        assert outcome.applied is True
        assert len(fake.calls) == 1
        assert fake.calls[0].selector == "tone"  # profile → tone (seam ④)
        assert "auto-eval" in outcome.message.lower()
        # The run id is linked to the proposal AT CREATION (UoW-24). Use a real
        # eval_runs row id (applied_eval_run_id is a FK).
        run_id = await store.create_eval_run(
            selector="tone",
            fingerprint="fp",
            identity_sha=None,
            skills_hash=None,
            provider_route="r",
            image_version=None,
        )
        await fake.calls[0].on_run_created(run_id)
        assert (await store.get_proposal(pid)).applied_eval_run_id == run_id

    async def test_lesson_apply_fires_tone_eval(self, wired_autoeval) -> None:
        store, _identity, applier, _cheap, fake, _notes = wired_autoeval
        pid = await _seed(
            store, kind="lesson", payload={"type": "edited", "text": "be concise"}, summary="l"
        )
        approved = await store.approve_proposal(pid)
        outcome = await applier.apply(approved)
        assert outcome.applied is True
        assert [c.selector for c in fake.calls] == ["tone"]

    async def test_completion_notice_posted(self, wired_autoeval) -> None:
        from ember.evals.base import Fingerprint, RunResult

        store, _identity, applier, _cheap, fake, notes = wired_autoeval
        pid = await _seed(
            store, kind="lesson", payload={"type": "edited", "text": "cite sources"}, summary="l2"
        )
        approved = await store.approve_proposal(pid)
        await applier.apply(approved)
        # Fire the completion hook the service would call when the run drains.
        fp = Fingerprint(
            composite="x",
            identity_sha=None,
            skills_hash=None,
            provider_route="r",
            image_version=None,
        )
        await fake.calls[0].on_complete(RunResult(run_id=5, fingerprint=fp, outcomes=[]))
        assert any("proposal #%d" % pid in n and "📈" in n for n in notes)

    async def test_finding_apply_does_not_eval(self, wired_autoeval) -> None:
        store, _identity, applier, _cheap, fake, _notes = wired_autoeval
        pid = await _seed(
            store, kind="finding", payload={"type": "friction", "summary": "z"}, summary="f"
        )
        approved = await store.approve_proposal(pid)
        await applier.apply(approved)
        assert fake.calls == []  # findings aren't applied → no eval


# --- ProposalApplier: skill / trigger / finding ------------------------------


class TestApplyNonWriterKinds:
    async def test_skill_deferred_manual(self, wired) -> None:
        store, _identity, applier, _cheap = wired
        pid = await _seed(
            store,
            kind="skill",
            payload={"type": "recurring", "pattern": "summarize my inbox"},
            summary="skill idea",
        )
        approved = await store.approve_proposal(pid)
        outcome = await applier.apply(approved)
        assert outcome.applied is False
        assert outcome.status == "approved"
        assert "author" in outcome.message.lower()
        assert "summarize my inbox" in outcome.message

    async def test_finding_not_appliable(self, wired) -> None:
        store, _identity, applier, _cheap = wired
        pid = await _seed(
            store, kind="finding", payload={"type": "friction", "summary": "z"}, summary="f"
        )
        approved = await store.approve_proposal(pid)
        outcome = await applier.apply(approved)
        assert outcome.applied is False
        assert outcome.error == "not_appliable"


# --- ProposalCommand dispatch -------------------------------------------------


class TestProposalCommand:
    @pytest.fixture
    async def cmd_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        settings = _make_settings(monkeypatch, tmp_path)
        store = Store(tmp_path / "conversations.db")
        await store.open()
        identity = IdentityStore(tmp_path / "identity")
        await identity.ensure_initialized()
        router, cheap = _router_with_cheap()
        cmd = ProposalCommand(
            identity=identity,
            profile_writer=ProfileWriter(identity=identity),
            lessons_writer=LessonsWriter(identity=identity, store=store),
            proposer=Proposer(router=router),
            now=_now,
        )
        registry = CommandRegistry()
        try:
            yield store, identity, cmd, cheap, settings, registry
        finally:
            await store.close()

    def _ctx(self, store, settings, registry, args) -> CommandContext:
        return CommandContext(
            conversation_key="t",
            user_id="1000",
            store=store,
            settings=settings,
            registry=registry,
            args=args,
        )

    async def test_approve_lesson_end_to_end(self, cmd_env) -> None:
        store, identity, cmd, _cheap, settings, registry = cmd_env
        pid = await _seed(
            store, kind="lesson", payload={"type": "edited", "text": "be terse"}, summary="terse"
        )
        reply = await cmd.run(
            self._ctx(store, settings, registry, {"action": "approve", "id": str(pid)})
        )
        assert "✅" in reply
        assert "be terse" in identity.read_lessons()
        assert (await store.get_proposal(pid)).status == "applied"

    async def test_reject_terminal(self, cmd_env) -> None:
        store, _identity, cmd, _cheap, settings, registry = cmd_env
        pid = await _seed(
            store, kind="lesson", payload={"type": "edited", "text": "x"}, summary="rej"
        )
        reply = await cmd.run(
            self._ctx(store, settings, registry, {"action": "reject", "id": str(pid)})
        )
        assert "rejected" in reply.lower()
        assert (await store.get_proposal(pid)).status == "rejected"
        assert await store.list_proposals(status="pending") == []

    async def test_edit_then_apply(self, cmd_env) -> None:
        store, identity, cmd, _cheap, settings, registry = cmd_env
        pid = await _seed(
            store, kind="lesson", payload={"type": "recurring", "pattern": "old"}, summary="e"
        )
        reply = await cmd.run(
            self._ctx(
                store,
                settings,
                registry,
                {"action": "edit", "id": str(pid), "text": "corrected lesson body"},
            )
        )
        assert "✅" in reply
        assert "corrected lesson body" in identity.read_lessons()
        assert (await store.get_proposal(pid)).status == "applied"

    async def test_unknown_id_lists_pending(self, cmd_env) -> None:
        store, _identity, cmd, _cheap, settings, registry = cmd_env
        pid = await _seed(
            store, kind="lesson", payload={"type": "edited", "text": "x"}, summary="pend"
        )
        reply = await cmd.run(
            self._ctx(store, settings, registry, {"action": "approve", "id": "999999"})
        )
        assert "❌" in reply
        assert f"#{pid}" in reply  # lists the real pending id

    async def test_reject_all_action_needs_no_id(self, cmd_env) -> None:
        # The ergonomic one-pick clear: action:reject-all, no id field.
        store, _identity, cmd, _cheap, settings, registry = cmd_env
        ids = [
            await _seed(store, kind="profile", payload={"fact": f"f{i}"}, summary=f"s{i}")
            for i in range(3)
        ]
        reply = await cmd.run(self._ctx(store, settings, registry, {"action": "reject-all"}))
        assert "rejected 3" in reply.lower()
        assert await store.list_proposals(status="pending") == []
        for pid in ids:
            assert (await store.get_proposal(pid)).status == "rejected"

    async def test_bulk_reject_all_via_id_field(self, cmd_env) -> None:
        store, _identity, cmd, _cheap, settings, registry = cmd_env
        ids = [
            await _seed(store, kind="profile", payload={"fact": f"f{i}"}, summary=f"s{i}")
            for i in range(3)
        ]
        reply = await cmd.run(
            self._ctx(store, settings, registry, {"action": "reject", "id": "all"})
        )
        assert "rejected 3" in reply.lower()
        assert await store.list_proposals(status="pending") == []
        for pid in ids:
            assert (await store.get_proposal(pid)).status == "rejected"

    async def test_approve_without_id_hints(self, cmd_env) -> None:
        # id is optional in the slash UI now; approve without one → typed hint.
        store, _identity, cmd, _cheap, settings, registry = cmd_env
        pid = await _seed(store, kind="lesson", payload={"text": "x"}, summary="p")
        reply = await cmd.run(self._ctx(store, settings, registry, {"action": "approve", "id": ""}))
        assert "❌" in reply and "id" in reply.lower()
        assert (await store.get_proposal(pid)).status == "pending"  # untouched

    async def test_bulk_reject_by_kind(self, cmd_env) -> None:
        store, _identity, cmd, _cheap, settings, registry = cmd_env
        prof = await _seed(store, kind="profile", payload={"fact": "p"}, summary="prof")
        find = await _seed(store, kind="finding", payload={"summary": "x"}, summary="find")
        reply = await cmd.run(
            self._ctx(store, settings, registry, {"action": "reject", "id": "profile"})
        )
        assert "rejected 1" in reply.lower()
        assert (await store.get_proposal(prof)).status == "rejected"
        assert (await store.get_proposal(find)).status == "pending"  # other kind untouched

    async def test_bulk_reject_id_list(self, cmd_env) -> None:
        store, _identity, cmd, _cheap, settings, registry = cmd_env
        a = await _seed(store, kind="lesson", payload={"text": "a"}, summary="a")
        b = await _seed(store, kind="lesson", payload={"text": "b"}, summary="b")
        keep = await _seed(store, kind="lesson", payload={"text": "c"}, summary="c")
        reply = await cmd.run(
            self._ctx(store, settings, registry, {"action": "reject", "id": f"{a}, {b}"})
        )
        assert "rejected 2" in reply.lower()
        assert (await store.get_proposal(keep)).status == "pending"

    async def test_double_approve_non_pending_hint(self, cmd_env) -> None:
        # Reject first (terminal), then approving it must not mutate — typed hint.
        store, _identity, cmd, _cheap, settings, registry = cmd_env
        pid = await _seed(
            store, kind="lesson", payload={"type": "edited", "text": "x"}, summary="t"
        )
        await cmd.run(self._ctx(store, settings, registry, {"action": "reject", "id": str(pid)}))
        reply = await cmd.run(
            self._ctx(store, settings, registry, {"action": "approve", "id": str(pid)})
        )
        assert "❌" in reply
        assert (await store.get_proposal(pid)).status == "rejected"
