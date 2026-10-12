"""Scheduler runner — UoW-5 wrapping (FC-6).

Single entrypoint `run_wrapped` that wraps every scheduled fire (cron or
manual) in the audit + notify boundary:

- Record `started_at` before `fn(ctx)` runs.
- `await asyncio.wait_for(fn(ctx), timeout=timeout_s)`.
- On success: `status=ok`, `result_json` carries the JSON-serialized
  return. No notifier call (TLC-2: ok rows are visible via `/runs`,
  notification is reserved for failures).
- On `asyncio.TimeoutError`: `status=timeout`, notify the channel.
- On any other Exception: `status=error`, `error_text=traceback`,
  notify the channel.

The runner never propagates user-function failures — the wrapper itself
is the recovery boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from ember.scheduler.base import RunContext, RunNotifier, RunRecorder

_log = logging.getLogger("ember.scheduler.runner")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize_result(value: Any) -> str:
    """JSON-encode the function return for `result_json`.

    Tolerates datetimes etc. via `default=str`. If the value isn't
    JSON-encodable at all (e.g. a custom object whose `default=str`
    repr loops), fall back to `str(value)` and log a warning.
    """
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError) as e:
        _log.warning("run result not JSON-encodable; using str(): %s", e)
        return json.dumps(str(value))


async def run_wrapped(
    *,
    ctx: RunContext,
    fn: Callable[[RunContext], Awaitable[Any]],
    timeout_s: int,
    recorder: RunRecorder,
    notifier: RunNotifier,
) -> None:
    """Run `fn(ctx)` with audit + notify wrapping (UoW-5).

    Public entrypoint shared by APScheduler-fired runs and `run_now`
    (FC-6: same code path for cron and manual).
    """
    started_at = _now()
    run_id = await recorder.record_started(
        skill=ctx.skill,
        function=ctx.run_name,  # the spec function-name is forwarded via ctx.run_name's pair; see below
        run_name=ctx.run_name,
        scheduled_for=ctx.scheduled_for,
        started_at=started_at,
    )

    # NOTE on `function` field: the recorder takes both `function` and
    # `run_name`. We forward `ctx.run_name` for both unless the caller
    # constructed the ctx with a separate function attribute. The
    # current `RunContext` only carries `run_name`; the caller (the
    # scheduler service) pre-resolves the function name and threads it
    # through by setting both into the ctx via the scheduler. For the
    # standalone runner, `function == run_name` is the safe default.

    try:
        result = await asyncio.wait_for(fn(ctx), timeout=timeout_s)
    except asyncio.TimeoutError:
        finished_at = _now()
        await recorder.record_finished(
            run_id,
            finished_at=finished_at,
            status="timeout",
            result_json=None,
            error_text=None,
        )
        try:
            await notifier.notify(f"⏱️ {ctx.skill}:{ctx.run_name} timed out after {timeout_s}s")
        except Exception:
            _log.exception("notifier raised on timeout for %s:%s", ctx.skill, ctx.run_name)
        return
    except Exception as e:
        tb = traceback.format_exc()
        finished_at = _now()
        await recorder.record_finished(
            run_id,
            finished_at=finished_at,
            status="error",
            result_json=None,
            error_text=tb,
        )
        try:
            await notifier.notify(f"❌ {ctx.skill}:{ctx.run_name} failed: {type(e).__name__}: {e}")
        except Exception:
            _log.exception("notifier raised on error for %s:%s", ctx.skill, ctx.run_name)
        return

    finished_at = _now()
    await recorder.record_finished(
        run_id,
        finished_at=finished_at,
        status="ok",
        result_json=_serialize_result(result),
        error_text=None,
    )
