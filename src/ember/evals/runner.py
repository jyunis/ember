"""Eval runner (UoW-21 / FC-21).

Deterministic orchestration around bounded LLM calls (TLC-13 spirit): the
structure of a run — which cases, how many trials, how a verdict is computed,
what gets written — is fixed Python; only the model's reply / routing choice is
nondeterministic, and that is captured and asserted, never used as control flow.

Per case×trial one `eval_case_results` row (the commit point). The runner
writes ONLY `eval_runs` / `eval_case_results` (FC-21); usage lands there,
satisfying TLC-5 without touching `messages`. A trial that raises (provider
error, subprocess failure) is recorded `status=error` and the run continues.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ember.evals.base import (
    EvalCase,
    RoutingCase,
    RunResult,
    ToneCase,
    ToolFlowCase,
    TrialOutcome,
)
from ember.evals.drivers import CaptureDriver, sum_usage
from ember.evals.fingerprint import compute_fingerprint
from ember.identity.store import IdentityStore
from ember.skills.router_prompt import build_skills_block, build_tools_catalog_block

if TYPE_CHECKING:
    from ember.config import Settings
    from ember.evals.base import Judge
    from ember.skills.base import SkillRegistry
    from ember.store.repo import Store
    from ember.tools.registry import Registry

_log = logging.getLogger("ember.evals.runner")

Progress = Callable[[str], Awaitable[None]] | None


class Runner:
    """Executes cases against the live config and persists results."""

    def __init__(
        self,
        *,
        store: "Store",
        driver: CaptureDriver,
        judge: "Judge",
        identity: IdentityStore,
        skill_registry: "SkillRegistry",
        tool_registry: "Registry",
        settings: "Settings",
        route: str,
    ) -> None:
        self._store = store
        self._driver = driver
        self._judge = judge
        self._identity = identity
        self._skill_registry = skill_registry
        self._tool_registry = tool_registry
        self._settings = settings
        self._route = route

    # --- Router-mode system assembly (mirrors agent._assemble_system_block) --

    def _router_system(self) -> str:
        """Soul+profile + `## Available skills` + `## Your tools`, joined the
        same way the live router turn assembles them (FC-18). A faithful
        reconstruction — the eval isn't inside an Agent instance — so routing
        is measured against the prompt production actually uses."""
        sep = IdentityStore.SECTION_SEPARATOR
        sections: list[str] = []
        base = self._identity.system_prompt()
        if base:
            sections.append(base)
        skills_block = build_skills_block(self._skill_registry)
        if skills_block:
            sections.append(skills_block)
        catalog_block = build_tools_catalog_block(self._tool_registry)
        if catalog_block:
            sections.append(catalog_block)
        return sep.join(sections)

    # --- Public entry -----------------------------------------------------

    async def run(
        self,
        cases: list[EvalCase],
        *,
        selector: str | None,
        skipped: list[str],
        progress: Progress = None,
        on_run_created: Callable[[int], Awaitable[None]] | None = None,
    ) -> RunResult:
        fp = compute_fingerprint(
            identity_dir=self._settings.identity_dir,
            skills_dir=self._settings.skills_dir,
            provider_route=self._route,
        )
        run_id = await self._store.create_eval_run(
            selector=selector,
            fingerprint=fp.composite,
            identity_sha=fp.identity_sha,
            skills_hash=fp.skills_hash,
            provider_route=fp.provider_route,
            image_version=fp.image_version,
        )
        # v13: link the run to the triggering proposal AT CREATION, so the
        # closure link (proposals.applied_eval_run_id) survives even if the run
        # crashes mid-flight (UoW-24).
        if on_run_created is not None:
            await on_run_created(run_id)

        # The driver (and its eval bridge) is owned by the caller and persists
        # across runs — `start()` is idempotent, and NOT stopping it here is
        # what keeps the eval bridge from leaking its port between back-to-back
        # runs. We always `finalize_eval_run`, even on a mid-run failure, so a
        # crash never leaves a dangling `finished_at IS NULL` row (UoW-21).
        outcomes: list[TrialOutcome] = []
        try:
            await self._driver.start()
            system = self._router_system()
            for case in cases:
                if progress is not None:
                    await progress(f"running `{case.id}` ({case.kind}, {case.trials}×)…")
                for trial in range(1, case.trials + 1):
                    outcome = await self._run_trial(case, trial, system)
                    outcomes.append(outcome)
                    await self._store.record_case_result(
                        run_id,
                        case_id=outcome.case_id,
                        kind=outcome.kind,
                        trial=outcome.trial,
                        status=outcome.status,
                        score=outcome.score,
                        judge_reason=outcome.judge_reason,
                        detail=outcome.detail,
                        usage=outcome.usage,
                    )
        finally:
            passed = sum(1 for o in outcomes if o.status == "pass")
            failed = sum(1 for o in outcomes if o.status == "fail")
            errored = sum(1 for o in outcomes if o.status == "error")
            await self._store.finalize_eval_run(
                run_id,
                total_cases=len(cases),
                passed=passed,
                failed=failed,
                errored=errored,
                skipped=len(skipped),
            )
        return RunResult(run_id=run_id, fingerprint=fp, outcomes=outcomes, skipped=skipped)

    # --- Per-trial dispatch ----------------------------------------------

    async def _run_trial(self, case: EvalCase, trial: int, system: str) -> TrialOutcome:
        try:
            if isinstance(case, RoutingCase):
                return await self._run_routing(case, trial, system)
            if isinstance(case, ToneCase):
                return await self._run_tone(case, trial, system)
            if isinstance(case, ToolFlowCase):
                return await self._run_tool_flow(case, trial, system)
        except Exception as e:  # a broken trial is `error`, never a crashed run
            _log.warning("eval trial %s/%d raised: %s", case.id, trial, e, exc_info=True)
            return TrialOutcome(
                case_id=case.id,
                kind=case.kind,
                trial=trial,
                status="error",
                detail=f"trial raised: {type(e).__name__}: {e}",
            )
        # Unreachable — every case kind is handled above.
        return TrialOutcome(
            case_id=case.id, kind=case.kind, trial=trial, status="error", detail="unknown case kind"
        )

    async def _run_routing(self, case: RoutingCase, trial: int, system: str) -> TrialOutcome:
        self._driver.recording.reconfigure(None)
        self._driver.recording.reset()
        _text, usage = await self._driver.run_turn(
            system=system, prompt=case.prompt, advertise_tools=True
        )
        calls = self._driver.recording.calls
        got = calls[0].name if calls else None
        status = "pass" if got == case.expect_tool else "fail"
        seq = ", ".join(c.name for c in calls) or "(no tool call)"
        detail = f"expect first tool {case.expect_tool!r}; got [{seq}]"
        return TrialOutcome(
            case_id=case.id, kind="routing", trial=trial, status=status, detail=detail, usage=usage
        )

    async def _run_tone(self, case: ToneCase, trial: int, system: str) -> TrialOutcome:
        # No tools advertised — a tone reply is a plain completion (and a
        # no-tools turn can have no side effect regardless of driver).
        reply, comp_usage = await self._driver.run_turn(
            system=system, prompt=case.prompt, advertise_tools=False
        )
        rubric = self._identity.read_soul()
        verdict = await self._judge.judge(reply=reply, rubric=rubric, criteria=case.criteria)
        usage = sum_usage(comp_usage, verdict.usage)
        if verdict.verdict is None:  # judge failed schema → error, never coerced
            return TrialOutcome(
                case_id=case.id,
                kind="tone",
                trial=trial,
                status="error",
                detail=f"judge: {verdict.error}",
                usage=usage,
            )
        v = verdict.verdict
        status = "pass" if v.passed else "fail"
        return TrialOutcome(
            case_id=case.id,
            kind="tone",
            trial=trial,
            status=status,
            score=v.score,
            judge_reason=v.reason,
            detail=None,
            usage=usage,
        )

    async def _run_tool_flow(self, case: ToolFlowCase, trial: int, system: str) -> TrialOutcome:
        self._driver.recording.reconfigure(case.scripted)
        self._driver.recording.reset()
        reply, usage = await self._driver.run_turn(
            system=system, prompt=case.prompt, advertise_tools=True
        )
        got_seq = tuple(c.name for c in self._driver.recording.calls)
        seq_ok = got_seq == case.expect_tools
        detail = f"expect tools {list(case.expect_tools)}; got {list(got_seq)}"

        if not seq_ok:
            return TrialOutcome(
                case_id=case.id,
                kind="tool_flow",
                trial=trial,
                status="fail",
                detail=detail,
                usage=usage,
            )
        # Sequence matched. Optionally judge the final reply too.
        if case.judge_criteria:
            rubric = self._identity.read_soul()
            verdict = await self._judge.judge(
                reply=reply, rubric=rubric, criteria=case.judge_criteria
            )
            usage = sum_usage(usage, verdict.usage)
            if verdict.verdict is None:
                return TrialOutcome(
                    case_id=case.id,
                    kind="tool_flow",
                    trial=trial,
                    status="error",
                    detail=f"{detail}; judge: {verdict.error}",
                    usage=usage,
                )
            if not verdict.verdict.passed:
                return TrialOutcome(
                    case_id=case.id,
                    kind="tool_flow",
                    trial=trial,
                    status="fail",
                    score=verdict.verdict.score,
                    judge_reason=verdict.verdict.reason,
                    detail=detail,
                    usage=usage,
                )
            return TrialOutcome(
                case_id=case.id,
                kind="tool_flow",
                trial=trial,
                status="pass",
                score=verdict.verdict.score,
                judge_reason=verdict.verdict.reason,
                detail=detail,
                usage=usage,
            )
        return TrialOutcome(
            case_id=case.id,
            kind="tool_flow",
            trial=trial,
            status="pass",
            detail=detail,
            usage=usage,
        )
