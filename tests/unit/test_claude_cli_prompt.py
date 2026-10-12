"""What claude_cli sends as the prompt (FC-2 + turn interrupt).

The claude CLI is the one provider that does *not* receive history: with
`--resume` it reconstructs the conversation from its own session jsonl
and takes only this turn's text as the positional prompt. So the prompt
the provider builds out of `messages` is the whole contract, and it has
one non-obvious job — carrying messages claude's session never saw.

The turn gate cancels an in-flight turn when a new message arrives and
reaps the subprocess, which usually dies before it writes its prompt to
the session file. The interrupted message is durably in *our* store and
invisible to claude's, so the next turn must resend it. Prompting with
only the newest message dropped the interrupted one outright.
"""

from __future__ import annotations

from typing import Any

import pytest

from ember.providers.base import Message
from ember.providers.claude_cli import ClaudeCliProvider


@pytest.fixture
def captured_args(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Intercept create_subprocess_exec and record argv, so the prompt
    can be asserted at the public boundary — what the provider hands to
    the OS — rather than through an internal helper."""
    captured: list[list[str]] = []

    class _FakeStream:
        def __init__(self, lines: list[bytes]) -> None:
            self._lines = list(lines)

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
            self.stdout = _FakeStream(
                [
                    b'{"type": "result", "is_error": false, "result": "ok", '
                    b'"session_id": "fake-sess", "usage": {"input_tokens": 1, '
                    b'"output_tokens": 1}}\n'
                ]
            )
            self.stderr = _FakeStream([])

        async def wait(self) -> int:
            return 0

    async def _fake_create(*args: Any, **kwargs: Any):  # noqa: ANN401, ARG001
        captured.append(list(args))
        return _FakeProc()

    import asyncio

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    return captured


class TestPromptViaSubprocessIntercept:
    async def test_ordinary_turn_prompts_with_the_new_message_only(
        self, captured_args: list[list[str]]
    ) -> None:
        provider = ClaudeCliProvider()
        await provider.complete(
            system="",
            messages=[
                Message("user", "what is the weather"),
                Message("assistant", "sunny"),
                Message("user", "and tomorrow?"),
            ],
            session_id="sess-1",
        )

        # Everything before the reply is already in claude's session file.
        assert captured_args[0][-1] == "and tomorrow?"

    async def test_interrupted_message_rides_along_with_the_one_that_killed_it(
        self, captured_args: list[list[str]]
    ) -> None:
        """The regression: two user rows with no assistant between them is
        exactly what an interrupted turn leaves in the store."""
        provider = ClaudeCliProvider()
        await provider.complete(
            system="",
            messages=[
                Message("user", "so the errors would need to sell assets?"),
                Message("user", "heirs"),
            ],
            session_id="sess-1",
        )

        assert captured_args[0][-1] == "so the errors would need to sell assets?\n\nheirs"


class TestExtractUserPromptDirect:
    """Same boundary, narrower failure mode."""

    def test_trailing_run_stops_at_the_last_assistant_turn(self) -> None:
        prompt = ClaudeCliProvider._extract_user_prompt(
            [
                Message("user", "old question"),
                Message("assistant", "old answer"),
                Message("user", "a"),
                Message("user", "b"),
                Message("user", "c"),
            ]
        )
        assert prompt == "a\n\nb\n\nc"

    def test_empty_messages_are_skipped_not_joined_as_blanks(self) -> None:
        prompt = ClaudeCliProvider._extract_user_prompt(
            [
                Message("assistant", "answer"),
                Message("user", ""),
                Message("user", "the real one"),
            ]
        )
        assert prompt == "the real one"

    def test_tool_dispatch_reentry_still_prompts_with_the_user_turn(self) -> None:
        """The tool loop re-calls complete() with assistant + tool rows
        appended, so the history does not end on a user message. That
        path keeps the pre-existing behavior: the newest user message."""
        prompt = ClaudeCliProvider._extract_user_prompt(
            [
                Message("user", "check my calendar"),
                Message("assistant", "calling a tool"),
                Message("tool", '{"events": []}'),
            ]
        )
        assert prompt == "check my calendar"

    def test_no_user_message_at_all_yields_empty(self) -> None:
        assert ClaudeCliProvider._extract_user_prompt([Message("assistant", "hi")]) == ""
