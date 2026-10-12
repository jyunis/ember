"""`_StreamObserver` tool-error surfacing.

The observer emits a ❌ status for tool_result errors so the user sees
real upstream failures even when the model narrates them away. Two
behaviors are pinned here:

  - claude CLI's client-side "No such tool available" rejection is a
    transient turn-start tool-resolution race that the model
    self-corrects on retry — it must NOT surface as ❌ (it wrongly
    implies the operation failed).
  - A genuine error still surfaces, and the namespaced tool name is
    backtick-wrapped so Discord markdown doesn't eat the `__` pairs in
    `mcp__agent__<tool>`.
"""

from __future__ import annotations

import pytest

from ember.agent import _StreamObserver
from tests.fakes.fake_channel import FakeChannel


def _assistant_tool_use(tu_id: str, name: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": tu_id, "name": name, "input": {}}]},
    }


def _user_tool_result(tu_id: str, text: str, *, is_error: bool) -> dict:
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "is_error": is_error,
                    "content": text,
                }
            ]
        },
    }


def _make_observer(channel: FakeChannel) -> _StreamObserver:
    return _StreamObserver(
        channel=channel,
        conversation_key="conv-1",
        memory_tracker=None,
        show_tool_calls=False,
    )


@pytest.mark.asyncio
async def test_no_such_tool_available_is_not_surfaced() -> None:
    channel = FakeChannel()
    observer = _make_observer(channel)

    await observer(_assistant_tool_use("tu_1", "mcp__agent__http_request"))
    await observer(
        _user_tool_result(
            "tu_1",
            "<tool_use_error>Error: No such tool available: mcp__agent__http_request"
            "</tool_use_error>",
            is_error=True,
        )
    )

    errors = [s for s in channel.sent if s.content.startswith("❌")]
    assert errors == [], f"transient no-such-tool error should be swallowed; got {errors!r}"


@pytest.mark.asyncio
async def test_real_tool_error_surfaces_with_backticked_name() -> None:
    channel = FakeChannel()
    observer = _make_observer(channel)

    await observer(_assistant_tool_use("tu_2", "mcp__agent__http_request"))
    await observer(
        _user_tool_result(
            "tu_2",
            "<tool_use_error>Error calling tool: HTTP 500 upstream</tool_use_error>",
            is_error=True,
        )
    )

    errors = [s for s in channel.sent if s.content.startswith("❌")]
    assert len(errors) == 1, f"expected one surfaced error; got {errors!r}"
    # Backtick-wrapped so Discord doesn't render `__` as underline.
    assert "`mcp__agent__http_request`" in errors[0].content
    assert "HTTP 500 upstream" in errors[0].content
