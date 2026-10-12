"""Runner orchestration tests (UoW-21 / FC-21).

Single-author (glue-level): the load-bearing FC-21 invariants live in
`RecordingRegistry` and `ToneJudge`, which are barrier-tested separately
(test_recording_registry.py / test_tone_judge.py). These exercise the runner
wiring against the in-process driver + FakeProviders: verdict computation,
judge-error-is-never-coerced, tool_flow sequence matching, and — as a
belt-and-braces check — that no real tool executes during a run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.evals.base import RoutingCase, ToneCase, ToolFlowCase
from ember.evals.drivers import InProcessDriver
from ember.evals.judge import ToneJudge
from ember.evals.recording import RecordingRegistry
from ember.evals.runner import Runner
from ember.identity.store import IdentityStore
from ember.providers.base import CompletionResult, ToolCall, Usage
from ember.providers.routing import ProviderRouter
from ember.skills.registry import InMemorySkillRegistry
from ember.store.repo import Store
from ember.tools.base import ToolResult
from ember.tools.registry import Registry
from tests.fakes.fake_provider import FakeProvider


class _ExplodingTool:
    """A registered tool that fails the test if ever executed (FC-21)."""

    requires_confirmation = False

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"{name} (must never execute in an eval)"
        self.input_schema = {"type": "object", "properties": {}, "additionalProperties": True}

    async def call(self, args: dict) -> ToolResult:  # pragma: no cover
        raise AssertionError(f"real tool {self.name} executed during eval!")


class _Settings:
    """Minimal Settings stand-in for the runner's fingerprint + dirs."""

    def __init__(self, identity_dir: Path, skills_dir: Path) -> None:
        self.identity_dir = identity_dir
        self.skills_dir = skills_dir


@pytest.fixture
async def store():
    s = Store(":memory:")
    await s.open()
    yield s
    await s.close()


@pytest.fixture
def identity(tmp_path):
    d = tmp_path / "identity"
    d.mkdir()
    (d / "soul.md").write_text("You are terse and never sycophantic.")
    (d / "profile.md").write_text("# User\nAlice.")
    return IdentityStore(d)


def _make_runner(store, identity, *, completion_fp, judge_fp, tool_registry, settings):
    recording = RecordingRegistry(tool_registry)
    driver = InProcessDriver(completion_fp, recording)
    judge = ToneJudge(ProviderRouter({"fakejudge": judge_fp}, cheap_tier_route="fakejudge"))
    return Runner(
        store=store,
        driver=driver,
        judge=judge,
        identity=identity,
        skill_registry=InMemorySkillRegistry(),
        tool_registry=tool_registry,
        settings=settings,
        route="claude_cli",
    )


def _registry(*names):
    r = Registry()
    for n in names:
        r.register(_ExplodingTool(n))
    return r


async def test_routing_pass_and_no_real_execution(store, identity, tmp_path):
    reg = _registry("mealie_search", "web_search")
    settings = _Settings(identity._soul_path.parent, tmp_path / "skills")
    comp = FakeProvider()
    # First completion: model calls mealie_search; second: a plain text turn.
    comp.queue_response(
        CompletionResult(
            text="",
            tool_calls=[ToolCall(id="t1", name="mealie_search", args={"q": "dinner"})],
            usage=Usage(5, 2, subscription=True),
        )
    )
    comp.queue_text("here are recipes")
    runner = _make_runner(
        store,
        identity,
        completion_fp=comp,
        judge_fp=FakeProvider(),
        tool_registry=reg,
        settings=settings,
    )
    case = RoutingCase(id="r-ok", prompt="what's for dinner", expect_tool="mealie_search", trials=1)
    result = await runner.run([case], selector=None, skipped=[])
    assert [o.status for o in result.outcomes] == ["pass"]
    rows = await store.case_results_for_run(result.run_id)
    assert rows[0].status == "pass" and rows[0].kind == "routing"
    run = await store.get_eval_run(result.run_id)
    assert run.passed == 1 and run.finished_at is not None


async def test_routing_fail_wrong_tool(store, identity, tmp_path):
    reg = _registry("mealie_search", "web_search")
    settings = _Settings(identity._soul_path.parent, tmp_path / "skills")
    comp = FakeProvider()
    comp.queue_response(
        CompletionResult(text="", tool_calls=[ToolCall(id="t1", name="web_search", args={})])
    )
    comp.queue_text("done")
    runner = _make_runner(
        store,
        identity,
        completion_fp=comp,
        judge_fp=FakeProvider(),
        tool_registry=reg,
        settings=settings,
    )
    case = RoutingCase(id="r-bad", prompt="x", expect_tool="mealie_search", trials=1)
    result = await runner.run([case], selector=None, skipped=[])
    assert result.outcomes[0].status == "fail"
    assert "web_search" in (result.outcomes[0].detail or "")


async def test_tone_pass(store, identity, tmp_path):
    reg = _registry()
    settings = _Settings(identity._soul_path.parent, tmp_path / "skills")
    comp = FakeProvider()
    comp.queue_text("No.")  # terse reply
    judge = FakeProvider()
    judge.queue_text('{"pass": true, "score": 5, "reason": "terse and direct"}')
    runner = _make_runner(
        store, identity, completion_fp=comp, judge_fp=judge, tool_registry=reg, settings=settings
    )
    case = ToneCase(
        id="t-ok", prompt="should I do the thing?", criteria="Terse; not sycophantic.", trials=1
    )
    result = await runner.run([case], selector=None, skipped=[])
    o = result.outcomes[0]
    assert o.status == "pass" and o.score == 5 and o.judge_reason


async def test_tone_judge_error_not_coerced(store, identity, tmp_path):
    reg = _registry()
    settings = _Settings(identity._soul_path.parent, tmp_path / "skills")
    comp = FakeProvider()
    comp.queue_text("a reply")
    judge = FakeProvider()
    judge.queue_text("not json")  # attempt 1
    judge.queue_text("still not json")  # retry
    runner = _make_runner(
        store, identity, completion_fp=comp, judge_fp=judge, tool_registry=reg, settings=settings
    )
    case = ToneCase(id="t-err", prompt="x", criteria="whatever", trials=1)
    result = await runner.run([case], selector=None, skipped=[])
    o = result.outcomes[0]
    assert o.status == "error"  # NEVER coerced to pass/fail
    run = await store.get_eval_run(result.run_id)
    assert run.errored == 1 and run.passed == 0 and run.failed == 0


async def test_tool_flow_sequence_and_no_side_effects(store, identity, tmp_path):
    reg = _registry("gmail_send")
    settings = _Settings(identity._soul_path.parent, tmp_path / "skills")
    comp = FakeProvider()
    comp.queue_response(
        CompletionResult(
            text="", tool_calls=[ToolCall(id="t1", name="gmail_send", args={"to": "x"})]
        )
    )
    comp.queue_text("sent")
    runner = _make_runner(
        store,
        identity,
        completion_fp=comp,
        judge_fp=FakeProvider(),
        tool_registry=reg,
        settings=settings,
    )
    case = ToolFlowCase(
        id="tf-ok",
        prompt="email support",
        expect_tools=("gmail_send",),
        scripted={"gmail_send": {"status": "sent"}},
        trials=1,
    )
    result = await runner.run([case], selector=None, skipped=[])
    assert result.outcomes[0].status == "pass"  # and _ExplodingTool never raised


async def test_tool_flow_sequence_mismatch_fails(store, identity, tmp_path):
    reg = _registry("gmail_send", "web_search")
    settings = _Settings(identity._soul_path.parent, tmp_path / "skills")
    comp = FakeProvider()
    comp.queue_response(
        CompletionResult(text="", tool_calls=[ToolCall(id="t1", name="web_search", args={})])
    )
    comp.queue_text("hmm")
    runner = _make_runner(
        store,
        identity,
        completion_fp=comp,
        judge_fp=FakeProvider(),
        tool_registry=reg,
        settings=settings,
    )
    case = ToolFlowCase(
        id="tf-bad",
        prompt="email support",
        expect_tools=("gmail_send",),
        scripted={"gmail_send": {"status": "sent"}},
        trials=1,
    )
    result = await runner.run([case], selector=None, skipped=[])
    assert result.outcomes[0].status == "fail"
