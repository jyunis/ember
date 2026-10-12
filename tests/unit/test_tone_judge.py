"""ToneJudge — cheap-tier, schema-validated tone judge (FC-21 / TLC-6 / TLC-12).

Tester barrier (UoW-21 / FC-21): written against `evals/base.py`
(the `Judge` Protocol + `JudgeOutcome`/`JudgeVerdict`), `providers/base.py`,
`providers/routing.py`, and `tests/fakes/fake_provider.py` ONLY. We have
NOT read `evals/judge.py` — the unit under test. ImportError on first run
is expected.

Contract (from `evals/base.py` Judge docstring + doc #36 § FC-21 / Layer 2):
- Constructor: ``ToneJudge(router: ProviderRouter)``.
- ``await judge(reply=..., rubric=..., criteria=...) -> JudgeOutcome``.
- Uses ``router.resolve_cheap_tier()`` for the provider+model, then ONE
  cheap-tier completion whose text must parse+validate to
  ``{pass: bool, score: int 1..5, reason: str}`` (JSON, possibly wrapped
  in ```json fences).
- On parse/schema failure it retries EXACTLY ONCE. If the retry also
  fails: ``verdict is None`` and ``error`` is set — NEVER coerced to
  pass/fail (FC-21). ``usage`` is ALWAYS populated (TLC-5).
- Fixed system prompt (module constant); the rubric + reply + criteria
  travel in the USER message so nothing judge-authored enters a cached
  block (TLC-12 byte stability).
"""

from __future__ import annotations

from ember.evals.base import JudgeOutcome
from ember.evals.judge import ToneJudge
from ember.providers.base import Usage
from ember.providers.routing import ProviderRouter
from tests.fakes.fake_provider import FakeProvider


# --- Helpers --------------------------------------------------------------


def _judge_with(fp: FakeProvider) -> ToneJudge:
    router = ProviderRouter({"fake": fp}, cheap_tier_route="fake")
    return ToneJudge(router)


async def _run(fp: FakeProvider, **kw: str) -> JudgeOutcome:
    kw.setdefault("reply", "Sure, happy to help with that.")
    kw.setdefault("rubric", "Warm, concise, direct.")
    kw.setdefault("criteria", "Should not be sycophantic.")
    return await _judge_with(fp).judge(**kw)  # type: ignore[arg-type]


def _contains(text: str, *needles: str) -> bool:
    return all(n in text for n in needles)


# --- Happy path: valid JSON first try -------------------------------------


class TestValidFirstTry:
    async def test_pass_true_maps_to_verdict(self) -> None:
        fp = FakeProvider()
        fp.queue_text('{"pass": true, "score": 5, "reason": "spot on"}')
        outcome = await _run(fp)
        assert outcome.error is None
        assert outcome.verdict is not None
        assert outcome.verdict.passed is True
        assert outcome.verdict.score == 5
        assert outcome.verdict.reason == "spot on"

    async def test_pass_false_maps_to_verdict(self) -> None:
        fp = FakeProvider()
        fp.queue_text('{"pass": false, "score": 2, "reason": "too cold"}')
        outcome = await _run(fp)
        assert outcome.verdict is not None
        assert outcome.verdict.passed is False
        assert outcome.verdict.score == 2
        assert outcome.verdict.reason == "too cold"

    async def test_usage_always_recorded_tlc5(self) -> None:
        fp = FakeProvider()
        fp.queue_text(
            '{"pass": true, "score": 4, "reason": "ok"}',
            input_tokens=11,
            output_tokens=7,
        )
        outcome = await _run(fp)
        assert isinstance(outcome.usage, Usage)
        # A single provider call was made.
        assert len(fp.calls) == 1

    async def test_single_provider_call_on_success(self) -> None:
        fp = FakeProvider()
        fp.queue_text('{"pass": true, "score": 3, "reason": "fine"}')
        await _run(fp)
        assert len(fp.calls) == 1


# --- Fenced JSON tolerance ------------------------------------------------


class TestFencedJson:
    async def test_json_fence_parses(self) -> None:
        fp = FakeProvider()
        fp.queue_text('```json\n{"pass": true, "score": 5, "reason": "great"}\n```')
        outcome = await _run(fp)
        assert outcome.verdict is not None
        assert outcome.verdict.passed is True
        assert outcome.verdict.score == 5
        assert len(fp.calls) == 1

    async def test_bare_fence_parses(self) -> None:
        fp = FakeProvider()
        fp.queue_text('```\n{"pass": false, "score": 1, "reason": "bad"}\n```')
        outcome = await _run(fp)
        assert outcome.verdict is not None
        assert outcome.verdict.passed is False


# --- Retry exactly once ---------------------------------------------------


class TestRetryOnce:
    async def test_malformed_then_valid_retries_and_succeeds(self) -> None:
        fp = FakeProvider()
        fp.queue_text("not json at all")
        fp.queue_text('{"pass": true, "score": 4, "reason": "recovered"}')
        outcome = await _run(fp)
        assert outcome.verdict is not None
        assert outcome.verdict.score == 4
        # Exactly one retry → two provider calls.
        assert len(fp.calls) == 2

    async def test_malformed_twice_returns_error_never_coerced(self) -> None:
        # FC-21: judge output that never validates does NOT become a
        # pass or a fail — verdict is None, error is set.
        fp = FakeProvider()
        fp.queue_text("not json")
        fp.queue_text("still not json")
        outcome = await _run(fp)
        assert outcome.verdict is None
        assert outcome.error is not None and outcome.error != ""
        # usage still populated (TLC-5).
        assert isinstance(outcome.usage, Usage)
        # Exactly one retry, no more.
        assert len(fp.calls) == 2

    async def test_no_third_attempt(self) -> None:
        # A third queued response must never be consumed — the judge
        # gives up after exactly one retry.
        fp = FakeProvider()
        fp.queue_text("bad 1")
        fp.queue_text("bad 2")
        fp.queue_text('{"pass": true, "score": 5, "reason": "would-be-third"}')
        outcome = await _run(fp)
        assert outcome.verdict is None
        assert len(fp.calls) == 2


# --- TLC-12: prompt-cache byte stability ----------------------------------


class TestByteStability:
    async def test_system_prompt_byte_identical_across_calls(self) -> None:
        # The system prompt is a fixed module constant. Two judge calls
        # with DIFFERENT reply/rubric/criteria must yield a byte-identical
        # `system` block (nothing case-specific leaks into the cached block).
        fp = FakeProvider()
        fp.queue_text('{"pass": true, "score": 5, "reason": "a"}')
        fp.queue_text('{"pass": false, "score": 1, "reason": "b"}')

        await _run(
            fp,
            reply="First reply text ALPHA",
            rubric="First rubric ONE",
            criteria="First criteria X",
        )
        await _run(
            fp,
            reply="Second totally different reply BETA",
            rubric="Second rubric TWO",
            criteria="Second criteria Y",
        )
        assert len(fp.calls) == 2
        assert fp.calls[0].system == fp.calls[1].system, (
            "system prompt must be byte-stable (TLC-12) across different inputs"
        )

    async def test_case_text_travels_in_user_message_not_system(self) -> None:
        fp = FakeProvider()
        fp.queue_text('{"pass": true, "score": 5, "reason": "ok"}')
        reply = "REPLY_MARKER_reply_body"
        rubric = "RUBRIC_MARKER_rubric_body"
        criteria = "CRITERIA_MARKER_criteria_body"
        await _run(fp, reply=reply, rubric=rubric, criteria=criteria)

        call = fp.calls[0]
        # None of the case-specific text is in the (cached) system block.
        assert reply not in call.system
        assert rubric not in call.system
        assert criteria not in call.system
        # All of it appears in the user-role message content.
        user_text = "\n".join(m.content for m in call.messages if m.role == "user")
        assert _contains(user_text, reply, rubric, criteria), (
            "reply/rubric/criteria must travel in the USER message"
        )


# --- Score range enforcement ----------------------------------------------


class TestScoreRange:
    async def test_out_of_range_score_triggers_retry(self) -> None:
        # ASSUMPTION (confirm against spec): score must validate to 1..5
        # (JudgeVerdict docstring: "score: int  # 1..5"). An out-of-range
        # score is a schema violation and should trigger the single retry,
        # not be accepted verbatim. If the impl does not enforce the range,
        # this test flags a spec-gap to resolve.
        fp = FakeProvider()
        fp.queue_text('{"pass": true, "score": 9, "reason": "over"}')
        fp.queue_text('{"pass": true, "score": 5, "reason": "valid"}')
        outcome = await _run(fp)
        assert outcome.verdict is not None
        assert outcome.verdict.score == 5
        assert len(fp.calls) == 2, "out-of-range score should have forced a retry"

    async def test_zero_score_triggers_retry(self) -> None:
        # Lower-bound sibling of the above (1..5 excludes 0). Same
        # assumption; encoded so both ends of the range are covered.
        fp = FakeProvider()
        fp.queue_text('{"pass": false, "score": 0, "reason": "under"}')
        fp.queue_text('{"pass": false, "score": 1, "reason": "valid"}')
        outcome = await _run(fp)
        assert outcome.verdict is not None
        assert outcome.verdict.score == 1
        assert len(fp.calls) == 2


# --- Missing / wrong-typed fields also trigger retry ----------------------


class TestSchemaValidation:
    async def test_missing_field_triggers_retry(self) -> None:
        fp = FakeProvider()
        fp.queue_text('{"pass": true, "score": 4}')  # no reason
        fp.queue_text('{"pass": true, "score": 4, "reason": "now complete"}')
        outcome = await _run(fp)
        assert outcome.verdict is not None
        assert len(fp.calls) == 2

    async def test_wrong_typed_pass_triggers_retry(self) -> None:
        fp = FakeProvider()
        fp.queue_text('{"pass": "yes", "score": 4, "reason": "stringy bool"}')
        fp.queue_text('{"pass": true, "score": 4, "reason": "fixed"}')
        outcome = await _run(fp)
        assert outcome.verdict is not None
        assert outcome.verdict.passed is True
        assert len(fp.calls) == 2
