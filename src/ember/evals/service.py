"""EvalService — the single serial eval runner (FC-25, v13).

v13 unifies two submitters onto one queue: `/eval run` (UoW-21) and the
auto-eval fired when a proposal is applied (UoW-24). The recording bridge binds
one fixed port and only one eval may execute at a time, so concurrent submits
(a manual run racing an apply, or two applies in a burst) would collide on the
port. This service owns:

- **The persistent capture driver + eval bridge** (built once, reused across
  jobs — rebinding per run leaked the port in v11; commit a943625).
- **One serial job queue drained by a single worker task.** Every submit is
  measured in FIFO order; nothing is dropped under bursts (Decision #25). The
  single worker IS the single-in-flight invariant — no locking needed.

Submitters hand in an already-loaded case set plus two optional async hooks:
`on_run_created(run_id)` (fired the moment the `eval_runs` row exists, so the
applier can write `proposals.applied_eval_run_id` at creation — the link then
survives a crashed run) and `on_complete(result)` (fired after the run
finishes, e.g. to post a summary). Job errors are logged and routed to
`on_error` (default: the service notifier), never crashing the worker.

The service writes only the eval tables + (via the applier's hook) the closure
link; it never mutates identity or eval cases (TLC-19 holds — it only reads +
records, FC-21/FC-25).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ember.evals.cases import load_cases
from ember.evals.drivers import build_driver
from ember.evals.judge import ToneJudge
from ember.evals.recording import RecordingRegistry
from ember.evals.runner import Runner

if TYPE_CHECKING:
    from ember.config import Settings
    from ember.evals.base import EvalCase, RunResult
    from ember.evals.drivers import CaptureDriver
    from ember.identity.store import IdentityStore
    from ember.providers.routing import ProviderRouter
    from ember.skills.base import SkillRegistry
    from ember.store.repo import Store
    from ember.tools.registry import Registry

_log = logging.getLogger("ember.evals.service")

OnRunCreated = Callable[[int], Awaitable[None]]
OnComplete = Callable[["RunResult"], Awaitable[None]]
OnError = Callable[[Exception], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class EvalJob:
    """One queued eval run. `cases`/`skipped` are pre-loaded by the submitter so
    the ack (case count, parse-skips) is synchronous; the run itself drains on
    the worker."""

    selector: str | None
    cases: list["EvalCase"]
    skipped: list[str]
    label: str
    on_run_created: OnRunCreated | None
    on_complete: OnComplete | None
    on_error: OnError | None


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    """Outcome of `EvalService.enqueue`. `accepted` is False when the selector
    matched no runnable cases (nothing queued). `depth` is the number of jobs
    ahead of this one (0 = starts immediately)."""

    accepted: bool
    count: int
    skipped: list[str]
    depth: int


class EvalService:
    """Owns the eval bridge + a single serial job queue (FC-25)."""

    def __init__(
        self,
        *,
        store: "Store",
        router: "ProviderRouter",
        identity: "IdentityStore",
        skill_registry: "SkillRegistry",
        tool_registry: "Registry",
        settings: "Settings",
        notify: Callable[[str], Awaitable[None]],
    ) -> None:
        self._store = store
        self._router = router
        self._identity = identity
        self._skill_registry = skill_registry
        self._tool_registry = tool_registry
        self._settings = settings
        self._notify = notify
        # Persistent driver + its eval bridge, built once on first run.
        self._driver: "CaptureDriver | None" = None
        self._route: str | None = None
        # Serial queue + its single worker (the single-in-flight invariant).
        self._queue: asyncio.Queue[EvalJob] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._busy = False

    # --- Submission -------------------------------------------------------

    def enqueue(
        self,
        selector: str | None,
        *,
        label: str,
        on_run_created: OnRunCreated | None = None,
        on_complete: OnComplete | None = None,
        on_error: OnError | None = None,
    ) -> EnqueueResult:
        """Load the selector's cases and queue a job. Synchronous: returns the
        case count + parse-skips + queue depth so the caller can ack. A selector
        that matches no cases is NOT queued (`accepted=False`)."""
        known = frozenset(self._tool_registry.names())
        cases, skipped = load_cases(self._settings.evals_cases_dir, known, selector=selector)
        if not cases:
            return EnqueueResult(accepted=False, count=0, skipped=skipped, depth=0)
        depth = self._queue.qsize() + (1 if self._busy else 0)
        job = EvalJob(
            selector=selector,
            cases=cases,
            skipped=skipped,
            label=label,
            on_run_created=on_run_created,
            on_complete=on_complete,
            on_error=on_error,
        )
        self._queue.put_nowait(job)
        self._ensure_worker()
        return EnqueueResult(accepted=True, count=len(cases), skipped=skipped, depth=depth)

    @property
    def busy(self) -> bool:
        """True while a job is executing or queued — the single-in-flight
        guarantee means at most one runs at a time regardless."""
        return self._busy or not self._queue.empty()

    # --- Worker -----------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        """Single worker: pull jobs FIFO and run them one at a time. Exits when
        the queue is empty; re-created on the next enqueue."""
        while True:
            try:
                job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._busy = True
            try:
                await self._run_job(job)
            except Exception as e:  # a broken job never kills the worker
                _log.exception("eval job %r failed", job.label)
                try:
                    if job.on_error is not None:
                        await job.on_error(e)
                    else:
                        await self._notify(f"❌ eval run failed: {type(e).__name__}: {e}")
                except Exception:
                    _log.exception("eval job error handler also failed")
            finally:
                self._busy = False
                self._queue.task_done()

    async def _run_job(self, job: EvalJob) -> None:
        runner = self._build_runner()
        result = await runner.run(
            job.cases,
            selector=job.selector,
            skipped=job.skipped,
            on_run_created=job.on_run_created,
        )
        if job.on_complete is not None:
            await job.on_complete(result)

    def _build_runner(self) -> Runner:
        # Build the driver (and its persistent eval bridge over a stable
        # RecordingRegistry) once; the runner reconfigures the registry per
        # case, so one long-lived bridge serves every run (no port rebind).
        if self._driver is None:
            recording = RecordingRegistry(self._tool_registry)
            resolved = self._router.resolve(self._settings.eval_route)
            self._driver = build_driver(
                settings=self._settings,
                recording=recording,
                provider=resolved.provider,
                model=resolved.model,
            )
            self._route = resolved.route
        assert self._route is not None
        return Runner(
            store=self._store,
            driver=self._driver,
            judge=ToneJudge(self._router),
            identity=self._identity,
            skill_registry=self._skill_registry,
            tool_registry=self._tool_registry,
            settings=self._settings,
            route=self._route,
        )
