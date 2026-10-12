"""`/proposal <approve|reject|edit> <id> [text]` (FC-23, UoW-23).

The human-gated resolution half of the learning loop. Proposals accumulate
silently all week (v12 retrospective); this command is how the user acts on
them — a slash-command family, deliberately NOT the inline y/n prompts that
sank the profile auto-proposer (Architecture decision #4 / TLC-21a). Approval
is the safety property: nothing the retrospective proposes ever mutates
anything without an explicit `/proposal approve`.

- **approve** — move `pending → approved`, then dispatch the payload through
  the kind's existing writer path (`ProposalApplier`). Re-invoking `approve`
  on a proposal already `approved`-but-unapplied (a prior writer failure)
  retries the apply. profile → ProfileWriter; lesson → LessonsWriter;
  skill/trigger → acknowledged, author manually; finding → nothing to apply.
- **reject** — `pending → rejected` (terminal); it drops out of the digest.
- **edit** — replace a pending proposal's text, then approve + apply it.

An unknown or non-pending id gets a typed error that lists the current pending
ids so the user can retry (FC-23 failure mode).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ember.commands.base import CommandContext, CommandOption
from ember.errors import ProposalStateError
from ember.learning.appliers import ProposalApplier

if TYPE_CHECKING:
    from ember.evals.service import EvalService
    from ember.identity.lessons import LessonsWriter
    from ember.identity.proposer import Proposer
    from ember.identity.store import IdentityStore
    from ember.identity.writer import ProfileWriter
    from ember.store.repo import Store


class ProposalCommand:
    """Resolve a learning-loop proposal (approve / reject / edit)."""

    name: str = "proposal"
    description: str = "Approve, reject, or edit a learning-loop proposal (learning loop v12.5)."
    # Discord options are NAMED, not positional, so `action` is the primary
    # dropdown and everything else is optional (mirrors `/eval`). All options
    # carry a default → none is "required", so the adapter's signature synthesis
    # never hits "non-default follows default". `reject-all` needs no id, so the
    # common "clear the junk" case is a single dropdown pick.
    options: list[CommandOption] = [
        CommandOption(
            name="action",
            description="approve | reject | edit | reject-all",
            default="approve",
            choices=["approve", "reject", "edit", "reject-all"],
        ),
        CommandOption(
            name="id",
            description="proposal id — or, for reject: all | a kind | a list like 3,5,7",
            required=False,
            default="",
        ),
        CommandOption(
            name="text",
            description="replacement text (edit only)",
            required=False,
            default="",
        ),
    ]

    def __init__(
        self,
        *,
        identity: "IdentityStore",
        profile_writer: "ProfileWriter",
        lessons_writer: "LessonsWriter",
        proposer: "Proposer | None",
        now: Callable[[], datetime] | None = None,
        eval_service: "EvalService | None" = None,
        notify: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._identity = identity
        self._profile_writer = profile_writer
        self._lessons_writer = lessons_writer
        self._proposer = proposer
        self._now = now or (lambda: datetime.now(UTC))
        # v13: auto-eval on apply. Threaded into the applier so approving a
        # profile/lesson proposal fires a scoped eval whose delta lands in the
        # next weekly digest (UoW-24).
        self._eval_service = eval_service
        self._notify = notify

    async def run(self, ctx: CommandContext) -> str:
        store = ctx.store
        action = (ctx.args.get("action") or "approve").strip().lower().replace("_", "-")
        raw_id = (ctx.args.get("id") or "").strip()
        text = (ctx.args.get("text") or "").strip()

        # Dedicated one-pick bulk clear — no id needed.
        if action == "reject-all":
            ids = [p.id for p in await store.list_proposals(status="pending")]
            return await self._bulk_reject(store, ids, "all")

        # Bulk reject via the id field: `all`, a kind name, or a comma/space list
        # of ids. Handled before the single-id int parse so a low-signal queue
        # can be cleared in one command. Approve/edit stay single-id —
        # bulk-approve would undercut the approval safety property (TLC-21a).
        if action == "reject":
            bulk_ids = await self._resolve_bulk_ids(store, raw_id)
            if bulk_ids is not None:
                return await self._bulk_reject(store, bulk_ids, raw_id)

        if not raw_id:
            return await self._pending_hint(
                store,
                f"`{action}` needs a proposal id — e.g. `id:6`. "
                "(To clear everything, use `action:reject-all`.)",
            )
        try:
            pid = int(raw_id)
        except (TypeError, ValueError):
            return await self._pending_hint(store, f"invalid proposal id {raw_id!r}.")

        proposal = await store.get_proposal(pid)
        if proposal is None:
            return await self._pending_hint(store, f"proposal #{pid} does not exist.")

        if action == "reject":
            if proposal.status != "pending":
                return await self._pending_hint(
                    store, f"proposal #{pid} is {proposal.status}, not pending."
                )
            try:
                await store.reject_proposal(pid, now=self._now())
            except ProposalStateError as e:
                return await self._pending_hint(store, str(e))
            return f"🗑️ proposal #{pid} rejected."

        if action == "edit":
            if not text:
                return "edit needs replacement text: `/proposal edit <id> <text>`."
            if proposal.status != "pending":
                return await self._pending_hint(
                    store, f"proposal #{pid} is {proposal.status}, not pending."
                )
            if proposal.kind == "finding":
                return (
                    "findings are informational — nothing to edit/apply. "
                    "`/proposal reject` to dismiss."
                )
            try:
                await store.edit_proposal(
                    pid,
                    payload=self._edit_payload(proposal.kind, text),
                    summary=f"edited ({proposal.kind}): {text[:80]}",
                )
            except ProposalStateError as e:
                return await self._pending_hint(store, str(e))
            proposal = await store.get_proposal(pid)
            assert proposal is not None  # just edited it
            # fall through into the approve+apply path

        elif action != "approve":
            return "unknown action. Use `approve`, `reject`, or `edit`."

        # --- approve + apply (also reached after edit) --------------------
        if proposal.kind == "finding":
            return (
                f"proposal #{pid} is a finding — informational, nothing to apply. "
                "`/proposal reject` to dismiss."
            )

        if proposal.status == "pending":
            try:
                proposal = await store.approve_proposal(pid)
            except ProposalStateError as e:
                return await self._pending_hint(store, str(e))
        elif proposal.status == "approved":
            # Retry a prior approve whose writer path had failed.
            pass
        else:
            return await self._pending_hint(
                store, f"proposal #{pid} is {proposal.status}, not pending."
            )

        applier = ProposalApplier(
            store=store,
            identity=self._identity,
            profile_writer=self._profile_writer,
            lessons_writer=self._lessons_writer,
            proposer=self._proposer,
            now=self._now,
            eval_service=self._eval_service,
            notify=self._notify,
        )
        outcome = await applier.apply(proposal)
        return outcome.message

    # --- helpers ----------------------------------------------------------

    @staticmethod
    def _edit_payload(kind: str, text: str) -> dict[str, Any]:
        """Rebuild a proposal payload around user-supplied text, preserving the
        keys the applier reads for each kind."""
        if kind == "profile":
            return {"type": "edited", "fact": text}
        if kind == "lesson":
            return {"type": "edited", "text": text}
        return {"type": "edited", "pattern": text}  # skill / trigger

    _KINDS = ("profile", "skill", "trigger", "lesson", "finding")

    async def _resolve_bulk_ids(self, store: "Store", raw_id: str) -> list[int] | None:
        """Interpret a bulk-reject selector, returning the target pending ids —
        or None if `raw_id` is a single id (falls through to the single path).

        Selectors: `all` → every pending; a kind name → every pending of that
        kind; a comma/space-separated list of ints → those. A single int → None.
        """
        token = raw_id.strip().lower()
        if token == "all":
            return [p.id for p in await store.list_proposals(status="pending")]
        if token in self._KINDS:
            return [p.id for p in await store.list_proposals(status="pending") if p.kind == token]
        parts = [p for p in raw_id.replace(",", " ").split() if p]
        if len(parts) > 1:
            ids: list[int] = []
            for p in parts:
                try:
                    ids.append(int(p))
                except ValueError:
                    continue
            return ids
        return None  # single id → single-reject path

    async def _bulk_reject(self, store: "Store", ids: list[int], raw_id: str) -> str:
        if not ids:
            return await self._pending_hint(store, f"nothing matched {raw_id!r} to reject.")
        rejected = await store.reject_proposals(ids, now=self._now())
        if not rejected:
            return await self._pending_hint(store, f"no pending proposals matched {raw_id!r}.")
        shown = ", ".join(f"#{i}" for i in rejected[:40])
        more = " …" if len(rejected) > 40 else ""
        return f"🗑️ rejected {len(rejected)} proposal(s): {shown}{more}"

    async def _pending_hint(self, store: "Store", prefix: str) -> str:
        pending = await store.list_proposals(status="pending")
        if not pending:
            return f"❌ {prefix} There are no pending proposals."
        ids = ", ".join(f"#{p.id}" for p in pending[:30])
        more = " …" if len(pending) > 30 else ""
        return f"❌ {prefix}\nPending proposals: {ids}{more}"
