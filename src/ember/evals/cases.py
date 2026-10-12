"""Case loader (doc #36 § Case format / FC-21).

Cases are data, not code: TOML files under `<data_dir>/evals/cases/*.toml`,
one case per file. This module only READS them — cases are the agent-forbidden
write set (TLC-19). Loading is strict and validated at parse time (FC-21):

- unknown `kind` → rejected;
- `routing.expect_tool` / `tool_flow.expect_tools` / scripted tool names that
  aren't registered tools → rejected ("unknown expect_tool");
- a `tool_flow` case whose `expect_tools` names a tool with no scripted
  response → rejected (a fake tool with no script must never fall through to a
  real tool).

A file that fails to parse/validate is collected as a skip reason (the run
names it in its summary) rather than aborting the whole load — one malformed
case shouldn't sink the suite.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any

from ember.evals.base import EvalCase, RoutingCase, ToneCase, ToolFlowCase

_log = logging.getLogger("ember.evals.cases")

_VALID_KINDS = {"routing", "tone", "tool_flow"}


class CaseParseError(Exception):
    """A single case file failed to parse or validate. Carries a
    human-readable reason for the run summary."""


def _require(data: dict[str, Any], key: str, path: Path) -> Any:
    if key not in data:
        raise CaseParseError(f"{path.name}: missing required key '{key}'")
    return data[key]


def _parse_case(path: Path, known_tools: frozenset[str]) -> EvalCase:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise CaseParseError(f"{path.name}: {type(e).__name__}: {e}") from e

    case_id = str(_require(data, "id", path))
    kind = str(_require(data, "kind", path))
    if kind not in _VALID_KINDS:
        raise CaseParseError(
            f"{path.name}: unknown kind {kind!r} (expected one of {sorted(_VALID_KINDS)})"
        )
    prompt = str(_require(data, "prompt", path))
    trials = int(data.get("trials", 3 if kind == "routing" else 1))
    if trials < 1:
        raise CaseParseError(f"{path.name}: trials must be >= 1")

    if kind == "routing":
        expect_tool = str(_require(data, "expect_tool", path))
        if expect_tool not in known_tools:
            raise CaseParseError(
                f"{path.name}: unknown expect_tool {expect_tool!r} (not a registered tool)"
            )
        return RoutingCase(id=case_id, prompt=prompt, expect_tool=expect_tool, trials=trials)

    if kind == "tone":
        criteria = str(_require(data, "criteria", path))
        return ToneCase(id=case_id, prompt=prompt, criteria=criteria, trials=trials)

    # tool_flow
    raw_expect = _require(data, "expect_tools", path)
    if not isinstance(raw_expect, list) or not all(isinstance(t, str) for t in raw_expect):
        raise CaseParseError(f"{path.name}: expect_tools must be a list of tool-name strings")
    expect_tools = tuple(raw_expect)
    scripted = dict(data.get("scripted", {}))
    if not isinstance(scripted, dict):
        raise CaseParseError(f"{path.name}: [scripted] must be a table of tool-name -> response")
    # Every named / scripted tool must be a real registered tool (FC-21:
    # unknown tool rejected — the model could never call an unregistered one).
    for tool_name in (*expect_tools, *scripted.keys()):
        if tool_name not in known_tools:
            raise CaseParseError(
                f"{path.name}: unknown tool {tool_name!r} in tool_flow case (not registered)"
            )
    # Every expected tool must have a scripted response — a fake tool with no
    # script must never fall through to a real tool (FC-21).
    missing = [t for t in expect_tools if t not in scripted]
    if missing:
        raise CaseParseError(
            f"{path.name}: expect_tools {missing} have no scripted response under [scripted]"
        )
    judge_criteria = data.get("judge_criteria")
    if judge_criteria is not None:
        judge_criteria = str(judge_criteria)
    return ToolFlowCase(
        id=case_id,
        prompt=prompt,
        expect_tools=expect_tools,
        scripted=scripted,
        judge_criteria=judge_criteria,
        trials=trials,
    )


def load_cases(
    cases_dir: Path,
    known_tools: frozenset[str],
    *,
    selector: str | None = None,
) -> tuple[list[EvalCase], list[str]]:
    """Load and validate all cases under `cases_dir`.

    `known_tools` is the set of registered tool names, used to reject unknown
    `expect_tool`/scripted tools at parse (FC-21). `selector` optionally filters
    to a single case id OR a kind (`routing`|`tone`|`tool_flow`).

    Returns `(cases, skipped)` where `skipped` is a list of human-readable
    reasons for files that failed to parse/validate or were filtered out by an
    unknown selector — surfaced in the run summary, never silently dropped.
    """
    cases: list[EvalCase] = []
    skipped: list[str] = []

    if not cases_dir.is_dir():
        return cases, [f"cases dir {cases_dir} does not exist"]

    sel_is_kind = selector in _VALID_KINDS if selector else False

    for path in sorted(cases_dir.glob("*.toml")):
        try:
            case = _parse_case(path, known_tools)
        except CaseParseError as e:
            skipped.append(str(e))
            _log.warning("eval case skipped: %s", e)
            continue
        if selector:
            if sel_is_kind:
                if case.kind != selector:
                    continue
            elif case.id != selector:
                continue
        cases.append(case)

    if selector and not sel_is_kind and not cases and not skipped:
        skipped.append(f"no case matched selector {selector!r}")

    # Stable order: kind then id, so run summaries and deltas line up.
    cases.sort(key=lambda c: (c.kind, c.id))
    return cases, skipped
