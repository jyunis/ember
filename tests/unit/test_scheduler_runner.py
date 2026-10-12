"""Scheduler runner — UoW-5 wrapping.

The scheduler service wraps every fire (and every `run_now` invocation)
through a single `run_wrapped` entrypoint so cron-fired and manually-
fired runs share recording + notification semantics (FC-6: "manual
trigger same code path").

Per UoW-5:
- Wrapper records `started_at` before the function runs.
- `await asyncio.wait_for(fn(ctx), timeout)`.
- On success: status=ok, result_json carries the serialized return.
- On asyncio.TimeoutError: status=timeout, notifier called.
- On any other exception: status=error, error_text carries traceback,
  notifier called.
- Successful runs do NOT call the notifier (TLC-2 visibility is satisfied
  by the row + the `/runs` command; only failures interrupt the user).
- `finished_at >= started_at`.

The runner takes a `RunContext`, a `Callable`, a `timeout_s`, the
recorder, and the notifier. Public function: `run_wrapped`. Imports
from `ember.scheduler.runner` per the file inventory the implementer
will follow.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any


from ember.scheduler.base import RunContext
from tests.fakes.fake_run_notifier import FakeRunNotifier
from tests.fakes.fake_run_recorder import FakeRunRecorder

# `runner.run_wrapped` is implemented by the implementer agent. Tests
# import lazily inside helpers so collection still works while the
# module is missing — pytest reports an ImportError at run time.


def _import_run_wrapped() -> Callable[..., Awaitable[Any]]:
    from ember.scheduler.runner import run_wrapped

    return run_wrapped


def _make_ctx(skill: str = "demo", run_name: str = "tick") -> RunContext:
    """Construct a minimal RunContext.

    The runner does not interpret most of these fields — they are
    forwarded into the user function. We pass typed sentinels so the
    tester can verify the runner does not mutate them.
    """
    return RunContext(
        skill=skill,
        run_name=run_name,
        scheduled_for=None,
        profile="profile-text",
        soul="soul-text",
        tools=None,  # type: ignore[arg-type]
        router=None,  # type: ignore[arg-type]
        notify=_fake_notify,
        now=datetime.utcnow,
    )


async def _fake_notify(_message: str) -> None:
    return None


# --- Successful run -------------------------------------------------------


class TestSuccessfulRun:
    async def test_status_ok_and_no_notifier_call(self) -> None:
        recorder = FakeRunRecorder()
        notifier = FakeRunNotifier()
        ctx = _make_ctx()

        async def fn(ctx_in: RunContext) -> dict[str, int]:
            return {"count": 3}

        run_wrapped = _import_run_wrapped()
        await run_wrapped(
            ctx=ctx,
            fn=fn,
            timeout_s=5,
            recorder=recorder,
            notifier=notifier,
        )

        assert len(recorder.started_calls) == 1
        assert recorder.started_calls[0].skill == "demo"
        assert recorder.started_calls[0].run_name == "tick"

        assert len(recorder.finished_calls) == 1
        finished = recorder.finished_calls[0]
        assert finished.status == "ok"
        assert finished.error_text is None
        assert finished.result_json is not None
        # The result is JSON-serialized; the runner is expected to call
        # json.dumps on the function's return.
        parsed = json.loads(finished.result_json)
        assert parsed == {"count": 3}

        # No notifier call on success.
        assert notifier.messages == []

    async def test_finished_at_geq_started_at(self) -> None:
        recorder = FakeRunRecorder()
        notifier = FakeRunNotifier()
        ctx = _make_ctx()

        async def fn(ctx_in: RunContext) -> str:
            await asyncio.sleep(0.01)
            return "done"

        run_wrapped = _import_run_wrapped()
        await run_wrapped(ctx=ctx, fn=fn, timeout_s=5, recorder=recorder, notifier=notifier)

        started = recorder.started_calls[0].started_at
        finished = recorder.finished_calls[0].finished_at
        assert finished >= started

    async def test_function_receives_run_context(self) -> None:
        recorder = FakeRunRecorder()
        notifier = FakeRunNotifier()
        ctx = _make_ctx(skill="payments", run_name="hourly")

        captured: dict[str, RunContext] = {}

        async def fn(ctx_in: RunContext) -> str:
            captured["ctx"] = ctx_in
            return "ok"

        run_wrapped = _import_run_wrapped()
        await run_wrapped(ctx=ctx, fn=fn, timeout_s=5, recorder=recorder, notifier=notifier)

        assert captured["ctx"] is ctx


# --- Timeout --------------------------------------------------------------


class TestTimeout:
    async def test_status_timeout_when_function_exceeds_budget(self) -> None:
        recorder = FakeRunRecorder()
        notifier = FakeRunNotifier()
        ctx = _make_ctx(skill="slow_skill", run_name="slow_run")

        async def fn(ctx_in: RunContext) -> str:
            # Sleep well past the timeout so wait_for cancels.
            await asyncio.sleep(5)
            return "never"

        run_wrapped = _import_run_wrapped()

        # The wrapper must catch the TimeoutError internally — the
        # public function must not raise on timeout (UoW-5 says the
        # wrapper records the row and notifies; it does not propagate).
        await run_wrapped(
            ctx=ctx,
            fn=fn,
            timeout_s=0,  # immediate timeout via wait_for(0)
            recorder=recorder,
            notifier=notifier,
        )

        assert len(recorder.finished_calls) == 1
        finished = recorder.finished_calls[0]
        assert finished.status == "timeout"

        # Exactly one notifier call, naming the skill+run.
        assert len(notifier.messages) == 1
        msg = notifier.messages[0]
        assert "slow_skill" in msg
        assert "slow_run" in msg


# --- Error ----------------------------------------------------------------


class TestError:
    async def test_status_error_with_traceback_and_notification(self) -> None:
        recorder = FakeRunRecorder()
        notifier = FakeRunNotifier()
        ctx = _make_ctx(skill="brokenskill", run_name="brokenrun")

        async def fn(ctx_in: RunContext) -> str:
            raise ValueError("kaboom-sentinel")

        run_wrapped = _import_run_wrapped()

        # Must not propagate the ValueError; wrapper records + notifies.
        await run_wrapped(ctx=ctx, fn=fn, timeout_s=5, recorder=recorder, notifier=notifier)

        assert len(recorder.finished_calls) == 1
        finished = recorder.finished_calls[0]
        assert finished.status == "error"
        # error_text contains the traceback (not just the bare message).
        # Hint of a traceback: presence of the exception class name AND
        # the sentinel from the message.
        assert finished.error_text is not None
        assert "ValueError" in finished.error_text
        assert "kaboom-sentinel" in finished.error_text

        assert len(notifier.messages) == 1
        msg = notifier.messages[0]
        assert "brokenskill" in msg
        assert "brokenrun" in msg


# --- run_id wiring --------------------------------------------------------


class TestRunIdWiring:
    async def test_finished_uses_id_returned_from_started(self) -> None:
        # The recorder returns sequential ids 1, 2, 3, ... — a single
        # run must call record_finished with id=1.
        recorder = FakeRunRecorder()
        notifier = FakeRunNotifier()
        ctx = _make_ctx()

        async def fn(ctx_in: RunContext) -> int:
            return 1

        run_wrapped = _import_run_wrapped()
        await run_wrapped(ctx=ctx, fn=fn, timeout_s=5, recorder=recorder, notifier=notifier)

        assert recorder.finished_calls[0].run_id == 1
