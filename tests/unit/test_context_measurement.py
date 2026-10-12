"""Honest context accounting (v15) — TLC-24 + FC-28, provider side.

- `Usage` gains `cache_creation_tokens: int = 0` (after `cached_tokens`).
- `CompletionResult` gains `context_tokens: int | None = None`.
- `ClaudeCliProvider` stream parsing: the turn's true context size is
  `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`
  from the FINAL assistant event carrying `message.usage` — never the
  first assistant event's, never the result event's aggregate.
- `_parse_usage` maps the result event's `cache_creation_input_tokens`
  onto `Usage.cache_creation_tokens` (asserted at the public
  `complete()` boundary via the returned `result.usage`).
- A stream with no assistant usage events → `context_tokens is None`.

Subprocess-faking pattern follows `tests/unit/test_claude_cli_prompt.py`:
intercept `asyncio.create_subprocess_exec` and feed a scripted stdout.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ember.providers.base import CompletionResult, Message, Usage
from ember.providers.claude_cli import ClaudeCliProvider


# --- Dataclass surface (FC-28) ---------------------------------------------


class TestUsageShape:
    def test_cache_creation_tokens_defaults_zero(self) -> None:
        u = Usage(input_tokens=1, output_tokens=2)
        assert u.cache_creation_tokens == 0

    def test_cache_creation_tokens_settable(self) -> None:
        u = Usage(input_tokens=1, output_tokens=2, cache_creation_tokens=7)
        assert u.cache_creation_tokens == 7

    def test_field_order_places_cache_creation_after_cached(self) -> None:
        """FC-28 pins the field position: after `cached_tokens`."""
        u = Usage(1, 2, 3, 4)
        assert u.input_tokens == 1
        assert u.output_tokens == 2
        assert u.cached_tokens == 3
        assert u.cache_creation_tokens == 4


class TestCompletionResultShape:
    def test_context_tokens_defaults_none(self) -> None:
        r = CompletionResult(text="hi")
        assert r.context_tokens is None

    def test_context_tokens_settable(self) -> None:
        r = CompletionResult(text="hi", context_tokens=51_207)
        assert r.context_tokens == 51_207


# --- Stream parsing (TLC-24) -----------------------------------------------


def _assistant_event(usage: dict[str, Any] | None, *, text: str = "…") -> bytes:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
    }
    if usage is not None:
        message["usage"] = usage
    return (json.dumps({"type": "assistant", "message": message}) + "\n").encode()


def _result_event(usage: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            {
                "type": "result",
                "is_error": False,
                "result": "final text",
                "session_id": "sess-ctx",
                "usage": usage,
            }
        )
        + "\n"
    ).encode()


@pytest.fixture
def fake_stream(monkeypatch: pytest.MonkeyPatch):
    """Intercept create_subprocess_exec; tests set `lines` (a list of
    jsonl bytes) BEFORE calling `provider.complete`."""
    lines: list[bytes] = []

    class _FakeStream:
        def __init__(self, content: list[bytes]) -> None:
            self._lines = list(content)

        def __aiter__(self) -> "_FakeStream":
            return self

        async def __anext__(self) -> bytes:
            if not self._lines:
                raise StopAsyncIteration
            return self._lines.pop(0)

        async def read(self) -> bytes:
            return b""

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = _FakeStream(lines)
            self.stderr = _FakeStream([])

        async def wait(self) -> int:
            return 0

    async def _fake_create(*args: Any, **kwargs: Any):  # noqa: ANN401, ARG001
        return _FakeProc()

    import asyncio

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    return lines


class TestContextTokensFromStream:
    async def test_final_assistant_usage_wins(self, fake_stream: list[bytes]) -> None:
        """Two assistant events with usage + a result event: the sum
        must come from the SECOND (final) assistant event's three
        fields — 7 + 50_000 + 1_200 = 51_207 — not the first's
        (10 + 100 + 5 = 115) and not the result aggregate."""
        fake_stream.extend(
            [
                _assistant_event(
                    {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 100,
                        "cache_creation_input_tokens": 5,
                        "output_tokens": 3,
                    }
                ),
                _assistant_event(
                    {
                        "input_tokens": 7,
                        "cache_read_input_tokens": 50_000,
                        "cache_creation_input_tokens": 1_200,
                        "output_tokens": 9,
                    }
                ),
                _result_event(
                    {
                        "input_tokens": 17,
                        "output_tokens": 42,
                        "cache_read_input_tokens": 999,
                        "cache_creation_input_tokens": 6,
                    }
                ),
            ]
        )

        provider = ClaudeCliProvider()
        result = await provider.complete(
            system="",
            messages=[Message("user", "hi")],
            session_id="sess-1",
        )

        assert result.context_tokens == 51_207
        assert result.context_tokens != 115  # not the first assistant event
        assert result.context_tokens != 17 + 999 + 6  # not the result aggregate

    async def test_trailing_assistant_without_usage_does_not_reset(
        self, fake_stream: list[bytes]
    ) -> None:
        """The tracked event is the last assistant event CARRYING
        message.usage — a later usage-less assistant event must not
        clobber it."""
        fake_stream.extend(
            [
                _assistant_event(
                    {
                        "input_tokens": 4,
                        "cache_read_input_tokens": 40,
                        "cache_creation_input_tokens": 400,
                        "output_tokens": 1,
                    }
                ),
                _assistant_event(None, text="no usage on this one"),
                _result_event({"input_tokens": 1, "output_tokens": 1}),
            ]
        )

        provider = ClaudeCliProvider()
        result = await provider.complete(
            system="",
            messages=[Message("user", "hi")],
            session_id="sess-1",
        )

        assert result.context_tokens == 4 + 40 + 400

    async def test_missing_cache_fields_default_to_zero(self, fake_stream: list[bytes]) -> None:
        """An assistant usage block without the cache fields still yields
        a sum (of what's there)."""
        fake_stream.extend(
            [
                _assistant_event({"input_tokens": 12, "output_tokens": 2}),
                _result_event({"input_tokens": 1, "output_tokens": 1}),
            ]
        )

        provider = ClaudeCliProvider()
        result = await provider.complete(
            system="",
            messages=[Message("user", "hi")],
            session_id="sess-1",
        )

        assert result.context_tokens == 12

    async def test_no_assistant_usage_events_yields_none(self, fake_stream: list[bytes]) -> None:
        fake_stream.extend(
            [
                _assistant_event(None, text="thinking out loud"),
                _result_event(
                    {
                        "input_tokens": 5,
                        "output_tokens": 5,
                        "cache_read_input_tokens": 50,
                        "cache_creation_input_tokens": 5,
                    }
                ),
            ]
        )

        provider = ClaudeCliProvider()
        result = await provider.complete(
            system="",
            messages=[Message("user", "hi")],
            session_id="sess-1",
        )

        assert result.context_tokens is None


class TestParseUsageCacheCreation:
    async def test_result_usage_maps_cache_creation_input_tokens(
        self, fake_stream: list[bytes]
    ) -> None:
        """The per-message aggregate (`_parse_usage`) carries
        `cache_creation_input_tokens` → `Usage.cache_creation_tokens`,
        observed at the public complete() boundary."""
        fake_stream.extend(
            [
                _result_event(
                    {
                        "input_tokens": 11,
                        "output_tokens": 22,
                        "cache_read_input_tokens": 33,
                        "cache_creation_input_tokens": 44,
                    }
                ),
            ]
        )

        provider = ClaudeCliProvider()
        result = await provider.complete(
            system="",
            messages=[Message("user", "hi")],
            session_id="sess-1",
        )

        assert result.usage.cache_creation_tokens == 44

    async def test_result_usage_without_cache_creation_defaults_zero(
        self, fake_stream: list[bytes]
    ) -> None:
        fake_stream.extend(
            [
                _result_event({"input_tokens": 1, "output_tokens": 1}),
            ]
        )

        provider = ClaudeCliProvider()
        result = await provider.complete(
            system="",
            messages=[Message("user", "hi")],
            session_id="sess-1",
        )

        assert result.usage.cache_creation_tokens == 0
