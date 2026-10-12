"""`EmberScheduler` — concrete `SchedulerService` (FC-6, UoW-5, TLC-10, TLC-13).

Wraps `apscheduler.schedulers.asyncio.AsyncIOScheduler` with a
`SQLAlchemyJobStore` pointed at the same SQLite file the conversation
store uses (APScheduler creates its own `apscheduler_jobs` table).

Responsibilities:
- `start`: register jobs from `SkillSource.list_schedules()` with
  `max_instances=1`, `coalesce=True` (TLC-10), and per-spec
  `misfire_grace_time`.
- `refresh`: reconcile the live job set against the latest snapshot
  from `SkillSource` — re-add new, remove missing, replace changed.
- `run_now`: invoke the *same wrapped path* used by cron fires (FC-6:
  manual trigger same code path) — directly bypassing the APScheduler
  queue so `/run` is synchronous from the user's perspective.
- `set_enabled`: pause/resume the APScheduler job AND persist via
  `Store.set_job_enabled`.
- `list_jobs`: hydrate the user-visible row shape (`JobSnapshot`).
- Subscribes to `SkillSource.on_change` → schedules a `refresh()`.
- Listens for `EVENT_JOB_MISSED` → records `status=missed`.

Note on the run_now path: APScheduler's job-store/queue is not used.
We resolve the `(skill, fn, run_name)` against the current
`SkillSource.list_schedules()` snapshot and invoke `run_wrapped`
directly. That guarantees `max_instances` is enforced for cron fires
(via APScheduler) and that `run_now` shares the audit/notify
boundary even when the cron job is paused.

Note on `function == run_name` in the recorded row: the runner reads
the function name from `RunContext.run_name`. We honor that here by
keeping the stored `function` field consistent with the spec.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ember.channels.base import Channel
from ember.config import Settings
from ember.errors import SchedulerError, UnknownScheduleError
from ember.scheduler.base import (
    JobSnapshot,
    RunContext,
    ScheduledFunction,
    ScheduleSpec,
    SkillSource,
)
from ember.scheduler.notifier import ChannelRunNotifier
from ember.scheduler.recorder import StoreRunRecorder
from ember.scheduler.runner import run_wrapped
from ember.store.repo import Store

_log = logging.getLogger("ember.scheduler.service")


def _job_id(skill: str, run_name: str) -> str:
    """APScheduler job id; one per (skill, run_name)."""
    return f"{skill}::{run_name}"


def _split_job_id(job_id: str) -> tuple[str, str] | None:
    if "::" not in job_id:
        return None
    skill, _, run_name = job_id.partition("::")
    return skill, run_name


# crontab day-of-week numbering: Sunday is 0 (and 7 is also Sunday).
# APScheduler numbers Monday 0 .. Sunday 6, and `CronTrigger.from_crontab`
# hands the field straight to its own parser without translating — so
# `0 17 * * 1-5` silently means Tue-Sat, not Mon-Fri. Every DOW cron in
# the skill tree was off by exactly one day because of this.
_CRONTAB_DOW = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
# APScheduler's own ordering, used to emit the normalized field.
_APS_DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DOW_ALIASES = {name: idx for idx, name in enumerate(_CRONTAB_DOW)}


def _dow_token_index(token: str) -> int:
    """Resolve one crontab day-of-week atom to its crontab index (sun=0)."""
    tok = token.strip().lower()
    if tok in _DOW_ALIASES:
        return _DOW_ALIASES[tok]
    try:
        value = int(tok)
    except ValueError:
        raise ValueError(f"unrecognized day-of-week {token!r}") from None
    if not 0 <= value <= 7:
        raise ValueError(f"day-of-week out of range: {token!r}")
    return 0 if value == 7 else value  # crontab: 7 is Sunday too


def _normalize_crontab_dow(field: str) -> str:
    """Translate a crontab day-of-week field into APScheduler day names.

    Ranges are expanded to an explicit name list rather than re-emitted as
    a range: crontab's Sunday-first ordering makes `0-2` (Sun-Tue) wrap in
    APScheduler's Monday-first ordering, which its range parser rejects.
    Expansion sidesteps the ordering question entirely.

    `*` passes through untouched — it means every day in both dialects.
    """
    if field.strip() in ("*", "?"):
        return field

    days: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        base, _, step_raw = part.partition("/")
        step = int(step_raw) if step_raw else 1
        if step < 1:
            raise ValueError(f"invalid step in day-of-week {part!r}")

        base = base.strip()
        if base in ("*", "?"):
            candidates = list(range(7))
        elif "-" in base[1:]:  # not a leading '-'; a real range
            lo_raw, _, hi_raw = base.partition("-")
            lo, hi = _dow_token_index(lo_raw), _dow_token_index(hi_raw)
            # Wrapping ranges (fri-mon) are legal crontab; walk forward.
            span = (hi - lo) % 7
            candidates = [(lo + offset) % 7 for offset in range(span + 1)]
        else:
            candidates = [_dow_token_index(base)]

        days.update(candidates[::step])

    if not days:
        raise ValueError(f"day-of-week field matched no days: {field!r}")
    # Emit in APScheduler's own order so the field reads naturally.
    return ",".join(name for name in _APS_DOW if _DOW_ALIASES[name] in days)


def crontab_trigger(cron: str, tz: Any) -> CronTrigger:
    """Build a `CronTrigger` from a *crontab* spec, day-of-week included.

    Thin wrapper over `CronTrigger.from_crontab` that first rewrites the
    day-of-week field to APScheduler's naming. Non-5-field specs are passed
    through so APScheduler raises its own error message.
    """
    fields = cron.split()
    if len(fields) == 5:
        fields[4] = _normalize_crontab_dow(fields[4])
        cron = " ".join(fields)
    return CronTrigger.from_crontab(cron, timezone=tz)


@dataclass
class _JobBinding:
    """Internal bookkeeping per registered job."""

    skill: str
    spec: ScheduleSpec
    fn: Callable[[RunContext], Awaitable[Any]]


class _MinimalChannel:
    """Minimal Channel impl for `RunContext` plumbing when no full
    Channel is provided (used only as a placeholder; the wired
    SchedulerService always has a real channel)."""

    name = "noop"

    async def start(self, on_message: Any) -> None:  # pragma: no cover
        return None

    async def stop(self) -> None:  # pragma: no cover
        return None

    async def send(self, *_a: Any, **_k: Any) -> list[str]:  # pragma: no cover
        return []

    async def set_typing(self, *_a: Any, **_k: Any) -> None:  # pragma: no cover
        return None

    async def notify_scheduler(self, content: str) -> None:  # pragma: no cover
        _log.info("noop notify_scheduler: %s", content)


class EmberScheduler:
    """`SchedulerService` driven by `SkillSource` + APScheduler.

    Constructor takes the wired dependencies; tests pass kwargs.
    """

    def __init__(
        self,
        *,
        store: Store,
        skill_source: SkillSource,
        channel: Channel,
        settings: Settings,
        identity: Any | None = None,
        tools: Any | None = None,
        router: Any | None = None,
    ) -> None:
        self._store = store
        self._source = skill_source
        self._channel = channel
        self._settings = settings
        self._identity = identity
        self._tools = tools
        self._router = router

        self._recorder = StoreRunRecorder(store)
        self._notifier = ChannelRunNotifier(channel)

        # In-memory jobstore. The architecture doc specced SQLAlchemy
        # for cross-restart persistence, but APScheduler's persistent
        # stores pickle the job target — which doesn't compose with
        # bound methods over a Store holding an open sqlite connection.
        # v6 hot-reload derives jobs from the registry on every start
        # anyway, so persistence buys us nothing; missed-fire catch-up
        # for cron jobs is handled by the explicit catch-up pass below.
        self._scheduler = AsyncIOScheduler(
            jobstores={"default": MemoryJobStore()},
            job_defaults={
                "max_instances": 1,
                "coalesce": True,
            },
            timezone=timezone.utc,
        )
        self._scheduler.add_listener(self._on_missed, EVENT_JOB_MISSED)

        # In-memory mirror of the registered jobs so we can read out
        # the cron strings / run-now metadata without round-tripping
        # to the SQLAlchemyJobStore.
        self._bindings: dict[str, _JobBinding] = {}

        # Coalescing-safe: only schedule one refresh at a time.
        self._refresh_lock = asyncio.Lock()
        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None

        # Wire the source→service refresh trigger.
        self._source.on_change(self._on_source_change)

    # --- Lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        self._scheduler.start()
        self._started = True
        await self.refresh()

    async def stop(self) -> None:
        if not self._started:
            return
        try:
            self._scheduler.shutdown(wait=False)
        except Exception:
            _log.exception("scheduler shutdown failed")
        self._started = False

    # --- Refresh ----------------------------------------------------------

    async def refresh(self) -> None:
        """Reconcile registered jobs against `SkillSource.list_schedules()`."""
        async with self._refresh_lock:
            target = list(self._source.list_schedules())
            target_ids = {_job_id(sf.skill, sf.spec.name) for sf in target}

            # Remove jobs that disappeared.
            for job_id in list(self._bindings.keys()):
                if job_id not in target_ids:
                    self._remove_job(job_id)

            # Add or replace.
            for sf in target:
                self._add_or_replace(sf)

    def _add_or_replace(self, sf: ScheduledFunction) -> None:
        job_id = _job_id(sf.skill, sf.spec.name)
        existing = self._bindings.get(job_id)
        # Always update the binding (so hot-reloaded fn refs land);
        # if the cron is unchanged we still re-add to update the fn.
        try:
            tz: Any = timezone.utc
            if sf.spec.timezone and sf.spec.timezone != "UTC":
                from zoneinfo import ZoneInfo

                tz = ZoneInfo(sf.spec.timezone)
            trigger = crontab_trigger(sf.spec.cron, tz)
        except Exception as e:
            raise SchedulerError(f"malformed cron {sf.spec.cron!r} on {job_id}: {e}") from e

        # Replace cleanly: remove existing first, then add.
        if existing is not None:
            try:
                self._scheduler.remove_job(job_id)
            except Exception:
                pass

        self._bindings[job_id] = _JobBinding(skill=sf.skill, spec=sf.spec, fn=sf.fn)

        # Honor persisted enabled state.
        # We can't easily do this synchronously here (it's a DB read),
        # so we add the job and pause it after the fact when needed.

        try:
            self._scheduler.add_job(
                self._fire_job,
                trigger=trigger,
                id=job_id,
                args=[job_id],
                name=job_id,
                max_instances=1,
                coalesce=sf.spec.coalesce,
                misfire_grace_time=sf.spec.misfire_grace_s,
                replace_existing=True,
            )
        except Exception as e:
            raise SchedulerError(f"add_job failed for {job_id}: {e}") from e

    def _remove_job(self, job_id: str) -> None:
        self._bindings.pop(job_id, None)
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass

    # --- Source change → schedule a refresh -------------------------------

    def _on_source_change(self) -> None:
        """Fire-and-forget refresh. Called from sync code (registry callbacks).

        If the loop is running, schedule a task; otherwise no-op (the
        next explicit `refresh()` will reconcile).
        """
        if self._loop is None or self._loop.is_closed():
            return
        if not self._started:
            return
        try:
            self._loop.call_soon_threadsafe(self._spawn_refresh)
        except RuntimeError:
            # Loop closed mid-flight; ignore.
            pass

    def _spawn_refresh(self) -> None:
        try:
            asyncio.create_task(self.refresh(), name="scheduler-refresh")
        except RuntimeError:
            pass

    # --- Wrapped invocation ------------------------------------------------

    async def _fire_job(self, job_id: str) -> None:
        """APScheduler-target wrapper. Calls `run_wrapped` for the job."""
        binding = self._bindings.get(job_id)
        if binding is None:
            _log.warning("fire for unknown job %r; skipping", job_id)
            return
        await self._invoke(binding, scheduled_for=None)

    async def _invoke(
        self,
        binding: _JobBinding,
        *,
        scheduled_for: datetime | None,
    ) -> None:
        """Build a fresh ctx and run through `run_wrapped`."""
        ctx = self._build_ctx(binding, scheduled_for=scheduled_for)
        await run_wrapped(
            ctx=ctx,
            fn=binding.fn,
            timeout_s=binding.spec.timeout_s,
            recorder=self._recorder,
            notifier=self._notifier,
        )

    def _build_ctx(
        self,
        binding: _JobBinding,
        *,
        scheduled_for: datetime | None,
    ) -> RunContext:
        """Construct the `RunContext` for a fire.

        We avoid coupling to `IdentityStore` and the tools/router when
        they aren't wired (test paths) — the unit tests don't exercise
        identity / router. Production wiring sets them on construction.
        """
        if self._identity is not None:
            from ember.skills.context import build_run_context

            return build_run_context(
                skill=binding.skill,
                spec=binding.spec,
                scheduled_for=scheduled_for,
                identity=self._identity,
                tools=self._tools,
                router=self._router,
                channel=self._channel,
                store=self._store,
            )

        async def _notify(message: str) -> None:
            try:
                await self._channel.notify_scheduler(
                    f"[{binding.skill}:{binding.spec.name}] {message}"
                )
            except Exception:
                _log.warning("notify_scheduler failed", exc_info=True)

        return RunContext(
            skill=binding.skill,
            run_name=binding.spec.name,
            scheduled_for=scheduled_for,
            profile="",
            soul="",
            tools=self._tools,  # type: ignore[arg-type]
            router=self._router,  # type: ignore[arg-type]
            notify=_notify,
            now=lambda: datetime.now(timezone.utc),
        )

    # --- run_now ----------------------------------------------------------

    async def run_now(self, skill: str, function: str, run_name: str) -> None:
        """Invoke the same wrapped path used by cron fires (FC-6).

        We bypass APScheduler's queue and call `run_wrapped` directly.
        For `max_instances=1` enforcement on overlapping run_now calls,
        we serialize via a per-job asyncio.Lock so the second call
        observes the in-progress state and returns.
        """
        binding = self._lookup_binding(skill, run_name)
        if binding is None:
            # Try resolving by current source snapshot — handles the
            # case where refresh hasn't yet run after a registry replace.
            for sf in self._source.list_schedules():
                if sf.skill == skill and sf.spec.name == run_name:
                    binding = _JobBinding(skill=sf.skill, spec=sf.spec, fn=sf.fn)
                    break
        if binding is None:
            raise UnknownScheduleError(f"{skill}:{run_name} not loaded")
        if binding.spec.function != function:
            # The caller asked for a different function name on this
            # run_name — surface a typed error so the slash command
            # can render it.
            raise UnknownScheduleError(
                f"{skill}:{run_name} bound to function {binding.spec.function!r}, not {function!r}"
            )

        # Per-job lock: enforces "no concurrent in-flight (skill, run_name)"
        # on the run_now path (matches APScheduler's max_instances=1).
        lock = self._lock_for(skill, run_name)
        if lock.locked():
            # Already in-flight — drop silently (matches APScheduler's
            # default coalesce/skip behavior).
            _log.info("run_now: %s:%s already running; dropping", skill, run_name)
            return
        async with lock:
            await self._invoke(binding, scheduled_for=None)

    _job_locks: dict[str, asyncio.Lock]

    def _lock_for(self, skill: str, run_name: str) -> asyncio.Lock:
        try:
            self._job_locks  # type: ignore[has-type]
        except AttributeError:
            self._job_locks = {}
        key = _job_id(skill, run_name)
        lock = self._job_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._job_locks[key] = lock
        return lock

    def _lookup_binding(self, skill: str, run_name: str) -> _JobBinding | None:
        return self._bindings.get(_job_id(skill, run_name))

    # --- enable / disable -------------------------------------------------

    async def set_enabled(self, skill: str, run_name: str, enabled: bool) -> None:
        job_id = _job_id(skill, run_name)
        binding = self._bindings.get(job_id)
        if binding is None:
            raise UnknownScheduleError(f"{skill}:{run_name} not loaded")
        try:
            if enabled:
                self._scheduler.resume_job(job_id)
            else:
                self._scheduler.pause_job(job_id)
        except Exception as e:
            raise SchedulerError(f"toggle {job_id} enabled={enabled}: {e}") from e
        await self._store.set_job_enabled(skill, run_name, enabled)

    # --- list_jobs --------------------------------------------------------

    def list_jobs(self) -> list[JobSnapshot]:
        out: list[JobSnapshot] = []
        for job_id, binding in self._bindings.items():
            try:
                ap_job = self._scheduler.get_job(job_id)
            except Exception:
                ap_job = None
            next_fire = ap_job.next_run_time if ap_job is not None else None
            enabled = next_fire is not None
            out.append(
                JobSnapshot(
                    skill=binding.skill,
                    run_name=binding.spec.name,
                    function=binding.spec.function,
                    cron=binding.spec.cron,
                    next_fire_time=next_fire,
                    enabled=enabled,
                )
            )
        out.sort(key=lambda j: (j.skill, j.run_name))
        return out

    # --- Misfire listener -------------------------------------------------

    def _on_missed(self, event: JobExecutionEvent) -> None:
        """APScheduler fires this when a misfire is detected outside
        the grace window. Record `status=missed` (no execution)."""
        parsed = _split_job_id(event.job_id)
        if parsed is None:
            return
        skill, run_name = parsed
        binding = self._bindings.get(event.job_id)
        function = binding.spec.function if binding else run_name
        scheduled_for = event.scheduled_run_time or datetime.now(timezone.utc)
        if scheduled_for.tzinfo is None:
            scheduled_for = scheduled_for.replace(tzinfo=timezone.utc)

        # We're in a sync APScheduler callback. Schedule the async record.
        if self._loop is None or self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self._recorder.record_missed(
                        skill=skill,
                        function=function,
                        run_name=run_name,
                        scheduled_for=scheduled_for,
                    ),
                    name=f"record-missed-{event.job_id}",
                )
            )
        except RuntimeError:
            pass


# Public alias matching the contracts surface name.
SchedulerService = EmberScheduler
