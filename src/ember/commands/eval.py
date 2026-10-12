"""`/eval [run|report] [selector]` slash command (FC-12, UoW-21).

- `run [selector]`: validate + load cases, then run them in the background
  (a real run spawns `claude -p` subprocesses and can take minutes), posting a
  single summary to the scheduler channel when done (UoW-21 "post one summary
  message"). `selector` filters to a case id OR a kind (routing|tone|tool_flow).
- `report [selector]`: render the latest finished run and its delta vs the
  previous run of the same case set.

Cases are read-only here (agent-forbidden write set, TLC-19). Only one run may
be in flight at a time — the eval bridge binds a fixed port.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from ember.commands.base import CommandContext, CommandOption

if TYPE_CHECKING:
    from ember.config import Settings
    from ember.evals.base import RunResult
    from ember.evals.service import EvalService
    from ember.store.repo import EvalRun, Store

_log = logging.getLogger("ember.commands.eval")


def _case_verdict(statuses: list[str]) -> str:
    """Roll trial statuses up to a case-level verdict (doc #36: pass =
    majority). `error` dominates a tie so a flaky/unscoreable case never reads
    green; otherwise majority pass → pass, else fail."""
    n = len(statuses)
    p = statuses.count("pass")
    e = statuses.count("error")
    if p * 2 > n:
        return "pass"
    if e * 2 >= n:
        return "error"
    return "fail"


class EvalCommand:
    """Run eval cases / report the latest run."""

    name: str = "eval"
    description: str = "Run eval cases or report the latest run (learning loop Layer 2)."
    options: list[CommandOption] = [
        CommandOption(
            name="action",
            description="run | report",
            default="run",
            choices=["run", "report"],
        ),
        CommandOption(
            name="selector",
            description="case id or kind (routing|tone|tool_flow); blank = all",
            required=False,
            default="",
        ),
    ]

    def __init__(
        self,
        *,
        store: "Store",
        service: "EvalService",
        settings: "Settings",
    ) -> None:
        self._store = store
        self._service = service
        self._settings = settings

    async def run(self, ctx: CommandContext) -> str:
        action = (ctx.args.get("action") or "run").strip()
        selector = (ctx.args.get("selector") or "").strip() or None
        if action == "report":
            return await self._report(selector)
        if action == "run":
            return await self._start_run(selector)
        return "Unknown action. Use `run` or `report`."

    # --- run --------------------------------------------------------------

    async def _start_run(self, selector: str | None) -> str:
        # v13: submit onto the shared serial queue (EvalService). A run already
        # in flight no longer rejects — this one queues behind it and drains in
        # order (Decision #25); its summary posts to the scheduler channel.
        label = f"/eval run {selector}" if selector else "/eval run"
        result = self._service.enqueue(selector, label=label, on_complete=self._post_summary)
        if not result.accepted:
            lines = ["No cases to run."]
            lines += [f"- skipped: {s}" for s in result.skipped]
            return "\n".join(lines)

        sel = f" (selector `{selector}`)" if selector else ""
        skip_note = f" — {len(result.skipped)} case(s) skipped at parse" if result.skipped else ""
        queued = f" — queued behind {result.depth} run(s)" if result.depth else ""
        return (
            f"🧪 eval run started: {result.count} case(s){sel}{skip_note}{queued}. "
            f"Route `{self._settings.eval_route}`; results will post to the scheduler channel."
        )

    async def _post_summary(self, result: "RunResult") -> None:
        await self._service._notify(self._summary(result))

    # --- rendering --------------------------------------------------------

    def _summary(self, result: "RunResult") -> str:
        by_case: dict[str, list[str]] = defaultdict(list)
        for o in result.outcomes:
            by_case[o.case_id].append(o.status)
        verdicts = {cid: _case_verdict(st) for cid, st in by_case.items()}
        passed = sum(1 for v in verdicts.values() if v == "pass")
        failed = sum(1 for v in verdicts.values() if v == "fail")
        errored = sum(1 for v in verdicts.values() if v == "error")

        tok_in = sum(o.usage.input_tokens for o in result.outcomes if o.usage)
        tok_out = sum(o.usage.output_tokens for o in result.outcomes if o.usage)
        cost = sum((o.usage.cost_usd or 0.0) for o in result.outcomes if o.usage)

        fp = result.fingerprint
        icon = {"pass": "✅", "fail": "❌", "error": "⚠️"}
        lines = [
            f"🧪 **eval run #{result.run_id}** — {passed}✅ {failed}❌ {errored}⚠️ "
            f"({len(verdicts)} cases)",
            f"fingerprint `{fp.composite}` · route `{fp.provider_route}` · "
            f"identity `{(fp.identity_sha or '—')[:8]}` · skills `{(fp.skills_hash or '—')[:8]}` · "
            f"image `{fp.image_version or '—'}`",
            f"tokens in {tok_in:,} / out {tok_out:,} · metered ${cost:.4f}",
        ]
        for cid in sorted(verdicts):
            lines.append(f"{icon[verdicts[cid]]} `{cid}`")
        if result.skipped:
            lines.append(f"skipped ({len(result.skipped)}):")
            lines += [f"  - {s}" for s in result.skipped]
        return "\n".join(lines)

    async def _report(self, selector: str | None) -> str:
        run = await self._store.latest_eval_run()
        if run is None:
            return "No finished eval runs yet. Run `/eval action:run` first."
        rows = await self._store.case_results_for_run(run.id)
        by_case: dict[str, list[str]] = defaultdict(list)
        scores: dict[str, list[int]] = defaultdict(list)
        for r in rows:
            by_case[r.case_id].append(r.status)
            if r.score is not None:
                scores[r.case_id].append(r.score)
        verdicts = {cid: _case_verdict(st) for cid, st in by_case.items()}
        icon = {"pass": "✅", "fail": "❌", "error": "⚠️"}

        lines = [
            f"🧪 **eval report — run #{run.id}** "
            f"({run.finished_at.strftime('%Y-%m-%d %H:%M') if run.finished_at else 'unfinished'})",
            f"fingerprint `{run.fingerprint}` · route `{run.provider_route}` · "
            f"identity `{(run.identity_sha or '—')[:8]}` · skills `{(run.skills_hash or '—')[:8]}` · "
            f"image `{run.image_version or '—'}`",
            f"totals: {run.passed}✅ {run.failed}❌ {run.errored}⚠️  (skipped {run.skipped})",
            "",
        ]
        for cid in sorted(verdicts):
            sc = scores.get(cid)
            sc_note = f" (score {sum(sc) / len(sc):.1f})" if sc else ""
            lines.append(f"{icon[verdicts[cid]]} `{cid}`{sc_note}")

        prev = await self._store.previous_eval_run(run.id, selector=run.selector)
        lines.append("")
        if prev is None:
            lines.append("_no prior run of the same case set to diff against._")
        else:
            lines.append(self._delta(prev, run))
        return "\n".join(lines)

    @staticmethod
    def _delta(prev: "EvalRun", cur: "EvalRun") -> str:
        def d(a: int, b: int) -> str:
            n = b - a
            return f"{n:+d}" if n else "±0"

        changed = []
        if prev.identity_sha != cur.identity_sha:
            changed.append("identity")
        if prev.skills_hash != cur.skills_hash:
            changed.append("skills")
        if prev.provider_route != cur.provider_route:
            changed.append("route")
        if prev.image_version != cur.image_version:
            changed.append("image")
        what = ", ".join(changed) if changed else "nothing (same fingerprint axes)"
        return (
            f"**Δ vs run #{prev.id}**: "
            f"pass {d(prev.passed, cur.passed)} · fail {d(prev.failed, cur.failed)} · "
            f"error {d(prev.errored, cur.errored)}  — changed: {what}"
        )
