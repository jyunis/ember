"""EvalService — single-port bridge + serial job queue (FC-25 / v13).

Tester-authored from the CONTRACT + SIGNATURES only (implementer/tester
barrier): these tests never read `service.py`'s body. They pin the two
load-bearing invariants — **single-in-flight** (the lone worker guarantees
at most one job runs at a time) and **FIFO** (every submit is measured, in
order, nothing dropped under bursts) — plus enqueue accept/reject, depth
reporting, on_run_created/on_complete plumbing through the REAL `_run_job`,
and worker survival across a job error.

The queue internals are driven by monkeypatching the documented seams
(`ember.evals.service.load_cases`, `service._run_job`, `service._build_runner`)
so ordering/concurrency are observable without touching privates.
"""

from __future__ import annotations

import asyncio
import types


from ember.evals.base import Fingerprint, RunResult
from ember.evals.service import EnqueueResult, EvalService


# --- Fixtures -------------------------------------------------------------


def _fingerprint() -> Fingerprint:
    return Fingerprint(
        composite="fp-abc",
        identity_sha=None,
        skills_hash=None,
        provider_route="claude_cli",
        image_version=None,
    )


def _make_service(notify=None):
    """Build an EvalService with inert stubs for every dep. `load_cases` is
    monkeypatched per-test, so the deps are never really exercised."""
    settings = types.SimpleNamespace(evals_cases_dir="/nonexistent/cases", eval_route="claude_cli")
    tool_registry = types.SimpleNamespace(names=lambda: [])
    if notify is None:

        async def notify(_msg: str) -> None:  # noqa: ANN001
            return None

    return EvalService(
        store=object(),
        router=object(),
        identity=object(),
        skill_registry=object(),
        tool_registry=tool_registry,
        settings=settings,
        notify=notify,
    )


async def _drain(service, *, limit: int = 200) -> None:
    """Poll `busy` (public) until the queue empties, bounded."""
    for _ in range(limit):
        if not service.busy:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("service did not drain in time")


def _patch_load(monkeypatch, cases, skipped):
    monkeypatch.setattr(
        "ember.evals.service.load_cases", lambda *a, **k: (list(cases), list(skipped))
    )


# --- Tests ----------------------------------------------------------------


async def test_enqueue_rejects_empty_selector(monkeypatch):
    _patch_load(monkeypatch, [], ["case-x: bad kind"])
    service = _make_service()

    result = service.enqueue("routing", label="manual")

    assert isinstance(result, EnqueueResult)
    assert result.accepted is False
    assert result.count == 0
    assert result.skipped == ["case-x: bad kind"]
    assert result.depth == 0
    assert service.busy is False  # nothing queued, no worker


async def test_enqueue_accepts_and_reports_depth(monkeypatch):
    _patch_load(monkeypatch, ["c1", "c2"], [])
    service = _make_service()

    gate = asyncio.Event()

    async def blocking_run_job(job):
        await gate.wait()

    monkeypatch.setattr(service, "_run_job", blocking_run_job)

    first = service.enqueue(None, label="a")
    assert first.accepted is True
    assert first.count == 2
    assert first.skipped == []
    assert first.depth == 0  # first submit sees an empty pipeline

    # Let the worker pick up + start the first (now blocked) job.
    for _ in range(50):
        if service.busy:
            break
        await asyncio.sleep(0.01)

    second = service.enqueue(None, label="b")
    assert second.accepted is True
    assert second.depth >= 1  # a job is in flight ahead of this one

    gate.set()
    await _drain(service)


async def test_single_in_flight(monkeypatch):
    _patch_load(monkeypatch, ["c1"], [])
    service = _make_service()

    active = 0
    max_seen = 0
    ran = 0

    async def counting_run_job(job):
        nonlocal active, max_seen, ran
        active += 1
        max_seen = max(max_seen, active)
        for _ in range(3):
            await asyncio.sleep(0)
        active -= 1
        ran += 1

    monkeypatch.setattr(service, "_run_job", counting_run_job)

    for i in range(4):
        assert service.enqueue(None, label=f"j{i}").accepted is True

    await _drain(service)

    assert max_seen == 1  # single worker => never overlapping
    assert ran == 4  # nothing dropped


async def test_fifo_order(monkeypatch):
    _patch_load(monkeypatch, ["c1"], [])
    service = _make_service()

    order: list[str] = []

    async def recording_run_job(job):
        # A yield so a naive concurrent impl would interleave and reorder.
        await asyncio.sleep(0)
        order.append(job.label)

    monkeypatch.setattr(service, "_run_job", recording_run_job)

    for label in ("a", "b", "c"):
        service.enqueue(None, label=label)

    await _drain(service)

    assert order == ["a", "b", "c"]


async def test_on_run_created_and_on_complete_plumbing(monkeypatch):
    # Exercises the REAL _run_job: only _build_runner is faked.
    _patch_load(monkeypatch, ["c1"], [])
    service = _make_service()

    expected = RunResult(run_id=4242, fingerprint=_fingerprint(), outcomes=[])

    class FakeRunner:
        async def run(self, cases, *, selector, skipped, on_run_created):
            if on_run_created is not None:
                await on_run_created(4242)
            return expected

    monkeypatch.setattr(service, "_build_runner", lambda: FakeRunner())

    created: list[int] = []
    completed: list[RunResult] = []

    async def on_run_created(run_id):
        created.append(run_id)

    async def on_complete(result):
        completed.append(result)

    service.enqueue(None, label="real", on_run_created=on_run_created, on_complete=on_complete)

    await _drain(service)

    assert created == [4242]
    assert completed == [expected]


async def test_job_error_is_isolated(monkeypatch):
    _patch_load(monkeypatch, ["c1"], [])
    service = _make_service()

    boom = RuntimeError("build failed")

    class GoodRunner:
        async def run(self, cases, *, selector, skipped, on_run_created):
            return RunResult(run_id=7, fingerprint=_fingerprint(), outcomes=[])

    calls = {"n": 0}

    def build_runner():
        calls["n"] += 1
        if calls["n"] == 1:
            raise boom
        return GoodRunner()

    monkeypatch.setattr(service, "_build_runner", build_runner)

    errors: list[BaseException] = []
    completed: list[RunResult] = []

    async def on_error(exc):
        errors.append(exc)

    async def on_complete(result):
        completed.append(result)

    service.enqueue(None, label="bad", on_error=on_error)
    service.enqueue(None, label="good", on_complete=on_complete)

    await _drain(service)

    assert errors == [boom]  # first job's error routed to its on_error
    assert len(completed) == 1  # worker survived; second job ran
    assert completed[0].run_id == 7
