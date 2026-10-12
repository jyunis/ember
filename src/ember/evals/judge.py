"""Tone judge (FC-21 / TLC-6 / TLC-12).

A cheap-tier, fixed-prompt LLM call that scores a completion's reply against a
rubric. Output is schema-validated `{pass, score, reason}` with exactly one
retry on violation; on a second failure the verdict is `None` and the case
becomes `status=error` — NEVER coerced to pass/fail (FC-21). The judge's own
text never drives control flow beyond the per-case verdict, and never enters a
cached block: the system prompt is a fixed module constant (byte-stable,
TLC-12) and the rubric/criteria/reply travel in the user message.
"""

from __future__ import annotations

import json
import logging

import jsonschema

from ember.evals.base import JudgeOutcome, JudgeVerdict
from ember.providers.base import Message, Usage
from ember.providers.routing import ProviderRouter

_log = logging.getLogger("ember.evals.judge")

# Fixed system prompt — byte-stable across every judge call (TLC-12). Contains
# no per-case content; the rubric/criteria/reply arrive in the user message.
TONE_JUDGE_SYSTEM = (
    "You are a strict evaluation judge for an AI assistant's replies. You are "
    "given a rubric, per-case criteria, and one assistant reply. Judge ONLY "
    "whether the reply satisfies the rubric and criteria — not whether it is "
    "factually correct in the world, and not your own opinion of the topic.\n\n"
    "Respond with ONLY a single JSON object, no prose and no markdown fences, "
    "of exactly this shape:\n"
    '{"pass": <true|false>, "score": <integer 1-5>, "reason": "<one sentence>"}\n\n'
    "score 5 = fully satisfies the rubric; 1 = clearly violates it. Set "
    '"pass" to true only when the reply meets the criteria. Keep "reason" to a '
    "single short sentence."
)

# Schema the judge output must satisfy (TLC-6). A violation (missing field,
# wrong type, out-of-range score) triggers the single retry.
_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "pass": {"type": "boolean"},
        "score": {"type": "integer", "minimum": 1, "maximum": 5},
        "reason": {"type": "string"},
    },
    "required": ["pass", "score", "reason"],
    "additionalProperties": True,
}


def _strip_fences(text: str) -> str:
    """Tolerate a ```json ... ``` (or bare ```) fence around the JSON."""
    s = text.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        # drop the opening fence (``` or ```json) and any closing fence
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


def _parse_verdict(text: str) -> JudgeVerdict | None:
    """Parse + schema-validate one judge response. Returns None on any
    failure (the caller retries once, then records error)."""
    try:
        data = json.loads(_strip_fences(text))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        jsonschema.validate(instance=data, schema=_VERDICT_SCHEMA)
    except jsonschema.ValidationError:
        return None
    return JudgeVerdict(
        passed=bool(data["pass"]),
        score=int(data["score"]),
        reason=str(data["reason"]),
    )


def _add_usage(a: Usage, b: Usage) -> Usage:
    """Sum two usages so a retried judge reports its full cost (TLC-5)."""
    cost: float | None
    if a.cost_usd is None and b.cost_usd is None:
        cost = None
    else:
        cost = (a.cost_usd or 0.0) + (b.cost_usd or 0.0)
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cached_tokens=a.cached_tokens + b.cached_tokens,
        cost_usd=cost,
        subscription=a.subscription or b.subscription,
    )


class ToneJudge:
    """Cheap-tier tone judge. Constructed with the process `ProviderRouter`;
    resolves the cheap tier per call (`router.resolve_cheap_tier()`)."""

    _MAX_ATTEMPTS = 2  # initial try + exactly one retry (FC-21)

    def __init__(self, router: ProviderRouter) -> None:
        self._router = router

    async def judge(self, *, reply: str, rubric: str, criteria: str) -> JudgeOutcome:
        user_msg = (
            f"## Rubric\n{rubric}\n\n"
            f"## Per-case criteria\n{criteria}\n\n"
            f"## Assistant reply to evaluate\n{reply}\n\n"
            "Respond with ONLY the JSON verdict object."
        )
        messages = [Message(role="user", content=user_msg)]

        try:
            resolved = self._router.resolve_cheap_tier()
        except Exception as e:  # ConfigError — cheap tier not configured
            _log.warning("tone judge: cheap tier unavailable: %s", e)
            return JudgeOutcome(
                verdict=None,
                usage=Usage(input_tokens=0, output_tokens=0),
                error=f"cheap-tier unavailable: {e}",
            )

        total = Usage(input_tokens=0, output_tokens=0)
        last_err = "no response"
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            try:
                result = await resolved.provider.complete(
                    system=TONE_JUDGE_SYSTEM,
                    messages=messages,
                    tools=None,
                    model=resolved.model,
                    session_id=None,
                    stream_handler=None,
                )
            except Exception as e:
                # A provider error is a failed attempt; retry once, then error.
                last_err = f"provider error: {type(e).__name__}: {e}"
                _log.warning("tone judge attempt %d: %s", attempt, last_err)
                continue
            total = _add_usage(total, result.usage)
            verdict = _parse_verdict(result.text)
            if verdict is not None:
                return JudgeOutcome(verdict=verdict, usage=total, error=None)
            last_err = f"schema violation: {result.text.strip()[:160]!r}"
            _log.info("tone judge attempt %d rejected: %s", attempt, last_err)

        return JudgeOutcome(verdict=None, usage=total, error=last_err)
