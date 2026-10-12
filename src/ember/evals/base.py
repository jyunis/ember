"""Eval-harness interface surface (UoW-21 / FC-21).

Interface-only, per the implementer/tester barrier: tester agents read this
file (plus the contracts doc and `tools/base.py` / `providers/base.py`) to
write tests for the behavior-sensitive surfaces (FC-21: no real tool side
effects; judge output never drives control flow). They do NOT read
`recording.py`, `judge.py`, or `runner.py`.

Three case kinds (doc #36 § Layer 2):
- ``routing``   — assert the FIRST tool the model reaches for equals
                  ``expect_tool``. (v11: measured on the production claude_cli
                  route, where routing is a direct tool call — not ``use_skill``,
                  which is never bridged. doc #36 Decisions log.)
- ``tone``      — a cheap-tier judge scores the reply against a soul-derived
                  rubric plus per-case ``criteria`` (schema-validated, TLC-6).
- ``tool_flow`` — run the turn against fake tools (scripted responses); assert
                  the recorded tool-call sequence and/or judge the final reply.
                  Zero real side effects, by construction (FC-21).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from ember.providers.base import Usage

# --- Case shapes -----------------------------------------------------------

CaseKind = Literal["routing", "tone", "tool_flow"]

# A per-case-trial verdict. `error` means the case could not be scored
# (e.g. judge schema violation after retry) and is NEVER coerced to
# pass/fail (FC-21 / UoW-21).
CaseStatus = Literal["pass", "fail", "error"]


@dataclass(frozen=True, slots=True)
class RoutingCase:
    """Assert the model dispatches to `expect_tool` first.

    `expect_tool` must resolve to a registered tool name at parse time
    (FC-21 "unknown expect_tool rejected"). `trials` defaults to 3 because
    routing is a nondeterministic step — case-level verdict is majority.
    """

    id: str
    prompt: str
    expect_tool: str
    trials: int = 3
    kind: CaseKind = "routing"


@dataclass(frozen=True, slots=True)
class ToneCase:
    """Judge a normal completion's reply against a rubric.

    `criteria` is the per-case rubric, appended to the soul-derived rubric
    the judge always applies. `trials` defaults to 1 (tone is scored, not
    asserted; repeat only when a case is known flaky).
    """

    id: str
    prompt: str
    criteria: str
    trials: int = 1
    kind: CaseKind = "tone"


@dataclass(frozen=True, slots=True)
class ToolFlowCase:
    """Run the turn against fake tools and assert the call sequence.

    `scripted` maps a tool name → the canned output the fake returns for
    any call to it (args-independent in v11). `expect_tools` is the exact
    ordered sequence of tool names the model is expected to call; every
    name in it MUST have a `scripted` entry (FC-21 parse rule). `judge_criteria`
    optionally judges the final reply too. No real tool ever executes.
    """

    id: str
    prompt: str
    expect_tools: tuple[str, ...]
    scripted: dict[str, Any] = field(default_factory=dict)
    judge_criteria: str | None = None
    trials: int = 1
    kind: CaseKind = "tool_flow"


EvalCase = RoutingCase | ToneCase | ToolFlowCase


# --- Tool-call capture (FC-21 core) ---------------------------------------


@dataclass(frozen=True, slots=True)
class RecordedToolCall:
    """One tool invocation captured during a case run. The name is the
    registry tool name (bridge-namespaced `mcp__agent__` prefix stripped)."""

    name: str
    args: dict[str, Any]


@runtime_checkable
class ToolCallRecorder(Protocol):
    """Contract for the recording tool registry the runner serves to the
    model (concrete impl: `evals.recording.RecordingRegistry`).

    Invariants (FC-21):
    - It exposes the SAME tool specs as the wrapped real registry, so the
      model sees an identical catalog and routes faithfully.
    - `call_tool` NEVER invokes the wrapped real tool's `call()` — no real
      side effect can occur. It records the call and returns either the
      case's scripted output for that tool or a generic inert stub.
    - Every call (expected or not, scripted or not) is appended to `calls`
      in invocation order — the audit log the runner asserts against.
    - Confirmation gating is bypassed (evals never prompt a human).
    """

    calls: list[RecordedToolCall]

    def list_tools(self) -> list[Any]: ...

    async def call_tool(
        self, name: str, args: dict[str, Any], *, allow_unconfirmed: bool = False
    ) -> Any: ...


# --- Tone judge (FC-21 / TLC-6) -------------------------------------------


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """Schema-validated judge output (TLC-6). `passed` is the only field
    that may influence the case verdict; `score`/`reason` are recorded but
    never drive control flow beyond the per-case verdict (FC-21)."""

    passed: bool
    score: int  # 1..5
    reason: str


@dataclass(frozen=True, slots=True)
class JudgeOutcome:
    """Result of one judge invocation. `verdict is None` means the judge
    output failed schema validation after the single retry — the case
    becomes `status=error`, never coerced (FC-21). `usage` is always
    populated (TLC-5); `error` carries a diagnostic when `verdict is None`."""

    verdict: JudgeVerdict | None
    usage: Usage
    error: str | None = None


@runtime_checkable
class Judge(Protocol):
    """Cheap-tier tone judge (concrete impl: `evals.judge.ToneJudge`).

    Fixed system prompt (module constant); the rubric + reply travel in the
    USER message so nothing judge-authored ever enters a cached block
    (TLC-12). Output is schema-validated `{pass, score, reason}` with exactly
    one retry on violation before returning `verdict=None` (FC-21)."""

    async def judge(self, *, reply: str, rubric: str, criteria: str) -> JudgeOutcome: ...


# --- Run results -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrialOutcome:
    """One case×trial outcome, before persistence."""

    case_id: str
    kind: CaseKind
    trial: int
    status: CaseStatus
    detail: str | None = None
    score: int | None = None
    judge_reason: str | None = None
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class RunResult:
    """Aggregate of one `/eval run`. `run_id` is the `eval_runs` row id.
    `skipped` counts cases dropped at parse time (named in the summary)."""

    run_id: int
    fingerprint: "Fingerprint"
    outcomes: list[TrialOutcome]
    skipped: list[str] = field(default_factory=list)


# --- Config fingerprint ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """The config fingerprint (doc #36 § Config fingerprint). `composite` is
    the stable hash stored on the run; the components are stored alongside so
    a report can name exactly what differed between two runs."""

    composite: str
    identity_sha: str | None
    skills_hash: str | None
    provider_route: str | None
    image_version: str | None


# --- Runner ----------------------------------------------------------------


@runtime_checkable
class EvalRunner(Protocol):
    """Executes a list of cases and persists results (concrete impl:
    `evals.runner.Runner`). `progress` is an optional async callback the
    command uses to stream status lines; the runner must tolerate `None`."""

    async def run(
        self,
        cases: list[EvalCase],
        *,
        selector: str | None,
        skipped: list[str],
        progress: Callable[[str], Awaitable[None]] | None = None,
        on_run_created: Callable[[int], Awaitable[None]] | None = None,
    ) -> RunResult: ...
