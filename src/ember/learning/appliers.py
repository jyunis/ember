"""Proposal appliers (FC-23, applier half).

Dispatch an APPROVED proposal through the EXISTING writer path for its kind.
The applier owns ZERO write logic of its own — it translates the stored
payload into each writer's concrete input and calls that writer, so every
mutation inherits that path's contracts (announce, git commit, revertibility,
no-op handling — TLC-2/9). It never touches soul.md or src/ (TLC-19).

Payload → writer-input translation (the v12.5 seam, resolved 2026-07-06):
- **profile** → re-render the FULL profile.md at approve-time from the current
  file + the stored fact (`Proposer.render_updated_profile`), then
  `ProfileWriter.commit`. Rendering against the *current* file is what makes a
  weeks-old pending proposal merge into intervening edits instead of clobbering
  them.
- **lesson** → deterministic format + append via `LessonsWriter` (date + text +
  evidence + reinforcement count, 8 KB cap). No LLM.
- **skill / trigger** → deferred: v12.5 has no synthesizer that turns a bare
  recurring-pattern string into a working TOML/py. Approval acknowledges the
  idea (drops it from the pending digest) and points at the manual UoW-10/11
  authoring flow; nothing is written.
- **finding** → informational, no writer path; not appliable (the command
  blocks `approve` on findings; this is a defensive fallback).

On writer-path failure the proposal stays `approved`-but-unapplied with the
error surfaced (the digest re-lists it); `mark_applied` runs only after the
writer actually commits (UoW-23 atomicity).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ember.evals.base import RunResult
    from ember.evals.service import EvalService
    from ember.identity.lessons import LessonsWriter
    from ember.identity.proposer import Proposer
    from ember.identity.store import IdentityStore
    from ember.identity.writer import ProfileWriter
    from ember.store.repo import Store
    from ember.store.types import Proposal

_log = logging.getLogger("ember.learning.appliers")

# v13 (UoW-24 / Decision #24): an applied proposal of these kinds auto-fires an
# eval scoped to the case kind most sensitive to that change. Identity edits
# (profile/lesson) move tone; a trigger moves routing. tool_flow is untouched by
# identity edits, so it is never auto-run. (Trigger application is still deferred
# — Decision #20 — so the trigger branch is wired-but-dormant until a trigger
# synthesizer lands.)
_AUTOEVAL_SELECTOR: dict[str, str] = {
    "profile": "tone",
    "lesson": "tone",
    "trigger": "routing",
}


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    """Result of applying one approved proposal.

    `applied` is True iff the writer committed AND the proposal moved to
    `applied`. `status` is the proposal's status after this call (`applied`, or
    `approved` when a writer failed / a skill-trigger idea was acknowledged).
    `message` is the user-facing announcement (TLC-2)."""

    applied: bool
    status: str
    message: str
    git_sha: str | None = None
    error: str | None = None


class ProposalApplier:
    """Routes an approved proposal to its kind's existing writer path."""

    def __init__(
        self,
        *,
        store: "Store",
        identity: "IdentityStore",
        profile_writer: "ProfileWriter",
        lessons_writer: "LessonsWriter",
        proposer: "Proposer | None",
        now: Callable[[], datetime],
        eval_service: "EvalService | None" = None,
        notify: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._identity = identity
        self._profile_writer = profile_writer
        self._lessons_writer = lessons_writer
        self._proposer = proposer
        self._now = now
        # v13: auto-eval on apply. Optional so the v12.5 applier tests (which
        # construct ProposalApplier without an eval service) keep passing —
        # absent service ⇒ no auto-eval, apply is otherwise identical.
        self._eval_service = eval_service
        self._notify = notify

    async def apply(self, proposal: "Proposal") -> ApplyOutcome:
        """Dispatch by kind, then (v13) auto-eval the change if it was applied.
        The proposal must already be `approved` (the command approves first,
        then applies)."""
        outcome = await self._dispatch(proposal)
        if outcome.applied:
            outcome = self._maybe_autoeval(proposal, outcome)
        return outcome

    async def _dispatch(self, proposal: "Proposal") -> ApplyOutcome:
        if proposal.kind == "profile":
            return await self._apply_profile(proposal)
        if proposal.kind == "lesson":
            return await self._apply_lesson(proposal)
        if proposal.kind in ("skill", "trigger"):
            return self._defer_manual(proposal)
        return ApplyOutcome(
            applied=False,
            status=proposal.status,
            message=(
                f"proposals of kind {proposal.kind!r} are informational and have no "
                "writer path — reject to dismiss."
            ),
            error="not_appliable",
        )

    # --- v13: auto-eval on apply (UoW-24) ---------------------------------

    def _maybe_autoeval(self, proposal: "Proposal", outcome: ApplyOutcome) -> ApplyOutcome:
        """Enqueue a kind-scoped eval on the shared serial queue and link its
        run id to the proposal at creation. Fire-and-forget: the run drains on
        the EvalService worker and posts its own completion notice; the
        before/after delta lands in the next weekly digest (UoW-24 / B). No-op
        (returns the outcome unchanged) when no eval service is wired or the
        kind isn't eval-relevant."""
        selector = _AUTOEVAL_SELECTOR.get(proposal.kind)
        if selector is None or self._eval_service is None:
            return outcome
        pid = proposal.id
        result = self._eval_service.enqueue(
            selector,
            label=f"auto-eval proposal #{pid} ({proposal.kind}→{selector})",
            on_run_created=lambda run_id: self._store.link_applied_eval_run(pid, run_id),
            on_complete=lambda run: self._autoeval_complete(pid, selector, run),
        )
        if not result.accepted:
            _log.info("auto-eval for proposal #%s skipped: no %s cases", pid, selector)
            return outcome
        queued = f" (queued behind {result.depth})" if result.depth else ""
        return replace(
            outcome,
            message=(
                f"{outcome.message}\n📈 auto-eval `{selector}` measuring the change"
                f"{queued}; before/after delta lands in the next weekly digest."
            ),
        )

    async def _autoeval_complete(
        self, proposal_id: int, selector: str, result: "RunResult"
    ) -> None:
        """Post a short completion notice for an auto-eval run (the full delta is
        the digest's job). Silent if no notifier is wired."""
        if self._notify is None:
            return
        p = sum(1 for o in result.outcomes if o.status == "pass")
        f = sum(1 for o in result.outcomes if o.status == "fail")
        e = sum(1 for o in result.outcomes if o.status == "error")
        try:
            await self._notify(
                f"📈 auto-eval for proposal #{proposal_id} (`{selector}`) finished — "
                f"run #{result.run_id}: {p}✅ {f}❌ {e}⚠️ trials. "
                "Before/after delta in the next weekly digest."
            )
        except Exception:
            _log.exception("auto-eval completion notice failed for proposal #%s", proposal_id)

    # --- profile (seam (a): re-render at approve-time) --------------------

    async def _apply_profile(self, proposal: "Proposal") -> ApplyOutcome:
        fact = self._payload_text(proposal, ("fact", "pattern", "text"))
        if not fact:
            return ApplyOutcome(
                applied=False,
                status="approved",
                message=f"proposal #{proposal.id} has no profile fact to apply.",
                error="empty_payload",
            )
        if self._proposer is None:
            return ApplyOutcome(
                applied=False,
                status="approved",
                message=(
                    f"proposal #{proposal.id} left approved: no cheap-tier renderer "
                    "configured to build the profile update. Retry once available."
                ),
                error="no_renderer",
            )
        try:
            current = self._identity.read_profile()
        except FileNotFoundError:
            current = ""
        proposed = await self._proposer.render_updated_profile(current_profile=current, fact=fact)
        if proposed is None:
            return ApplyOutcome(
                applied=False,
                status="approved",
                message=(
                    f"proposal #{proposal.id} left approved: couldn't render a profile "
                    "update (cheap tier unavailable or empty output). Retry later."
                ),
                error="render_failed",
            )
        result = await self._profile_writer.commit(proposed, proposal_id=proposal.id)
        if not result.committed:
            if result.error is None:
                # No-op: the rendered profile matched the file byte-for-byte —
                # the fact is already reflected. Still a successful resolution.
                applied = await self._store.mark_applied(proposal.id, now=self._now())
                return ApplyOutcome(
                    applied=True,
                    status=applied.status,
                    message=(
                        f"✅ proposal #{proposal.id} applied — profile already reflected "
                        "it, no change needed."
                    ),
                )
            return ApplyOutcome(
                applied=False,
                status="approved",
                message=(
                    f"⚠️ proposal #{proposal.id} left approved: profile commit failed "
                    f"({result.error})."
                ),
                error=result.error,
            )
        applied = await self._store.mark_applied(proposal.id, now=self._now())
        sha = result.git_commit_sha or ""
        return ApplyOutcome(
            applied=True,
            status=applied.status,
            git_sha=result.git_commit_sha,
            message=(
                f"✅ profile updated & committed `{sha[:8]}` (proposal #{proposal.id}): "
                f"{proposal.summary}"
            ),
        )

    # --- lesson (seam (b): deterministic append) --------------------------

    async def _apply_lesson(self, proposal: "Proposal") -> ApplyOutcome:
        # v13 (C): a lesson proposal carrying {op:'prune', lesson_id} soft-deletes
        # a stale lesson row instead of adding one. The digest generates these
        # for rows unreinforced >45d; approving one is a normal mutation.
        payload = self._payload_dict(proposal)
        if payload.get("op") == "prune":
            return await self._apply_lesson_prune(proposal, payload)

        text = self._payload_text(proposal, ("text", "pattern", "fact"))
        if not text:
            return ApplyOutcome(
                applied=False,
                status="approved",
                message=f"proposal #{proposal.id} has no lesson text to apply.",
                error="empty_payload",
            )
        evidence = self._evidence(proposal)
        # v13: lessons are rows now. apply_lesson inserts (or reinforces a live
        # match) then re-renders + commits lessons.md; the 8 KB cap is enforced
        # over the render (TLC-21b).
        result = await self._lessons_writer.apply_lesson(
            text=text,
            evidence=evidence,
            proposal_id=proposal.id,
            reinforced_count=proposal.reinforced_count,
            now=self._now(),
        )
        if not result.committed:
            # Cap-exceeded (pruning hint) or git failure → stays approved.
            return ApplyOutcome(
                applied=False,
                status="approved",
                message=f"⚠️ proposal #{proposal.id} not applied: {result.error}",
                error=result.error,
            )
        applied = await self._store.mark_applied(proposal.id, now=self._now())
        sha = result.git_commit_sha or ""
        verb = "reinforced an existing lesson" if result.reinforced else "lesson recorded"
        return ApplyOutcome(
            applied=True,
            status=applied.status,
            git_sha=result.git_commit_sha,
            message=(f"✅ {verb} & committed `{sha[:8]}` (proposal #{proposal.id})."),
        )

    async def _apply_lesson_prune(
        self, proposal: "Proposal", payload: dict[str, Any]
    ) -> ApplyOutcome:
        """Soft-delete a stale lesson row + re-render lessons.md + commit (UoW-25
        / C). A no-op (row already gone / bad id) leaves the proposal approved."""
        lesson_id = payload.get("lesson_id")
        if not isinstance(lesson_id, int) or isinstance(lesson_id, bool):
            return ApplyOutcome(
                applied=False,
                status="approved",
                message=f"proposal #{proposal.id} prune has no lesson_id.",
                error="empty_payload",
            )
        result = await self._lessons_writer.prune_lesson(
            lesson_id=lesson_id, proposal_id=proposal.id, now=self._now()
        )
        if not result.committed:
            return ApplyOutcome(
                applied=False,
                status="approved",
                message=f"⚠️ proposal #{proposal.id} not applied: {result.error}",
                error=result.error,
            )
        applied = await self._store.mark_applied(proposal.id, now=self._now())
        sha = result.git_commit_sha or ""
        return ApplyOutcome(
            applied=True,
            status=applied.status,
            git_sha=result.git_commit_sha,
            message=(
                f"✅ pruned stale lesson #{lesson_id} & committed `{sha[:8]}` "
                f"(proposal #{proposal.id})."
            ),
        )

    # --- skill / trigger (deferred to manual authoring) -------------------

    def _defer_manual(self, proposal: "Proposal") -> ApplyOutcome:
        pattern = self._payload_text(proposal, ("pattern", "text", "fact")) or proposal.summary
        where = (
            "a declarative TOML trigger (UoW-10)"
            if proposal.kind == "trigger"
            else "an imperative `_drafts/` skill (UoW-11)"
        )
        return ApplyOutcome(
            applied=False,
            status="approved",
            message=(
                f"📌 proposal #{proposal.id} approved as a {proposal.kind} idea — but "
                f"{proposal.kind}s aren't auto-applied in v12.5. Author it as {where} "
                f"(e.g. via `skill_author`); it goes live through the normal path.\n"
                f"Pattern: {pattern}"
            ),
        )

    # --- helpers ----------------------------------------------------------

    @staticmethod
    def _payload_dict(proposal: "Proposal") -> dict[str, Any]:
        """The proposal payload as a dict (empty on any parse failure)."""
        try:
            payload: Any = json.loads(proposal.payload_json)
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _payload_text(proposal: "Proposal", keys: tuple[str, ...]) -> str | None:
        """First non-empty string among `keys` in the proposal payload."""
        try:
            payload: Any = json.loads(proposal.payload_json)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        for k in keys:
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    @staticmethod
    def _evidence(proposal: "Proposal") -> dict[str, list[int]]:
        try:
            ev = json.loads(proposal.evidence_json)
        except (TypeError, ValueError):
            return {}
        return ev if isinstance(ev, dict) else {}
