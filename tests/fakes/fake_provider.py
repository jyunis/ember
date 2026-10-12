"""Programmable Provider stub for tests.

Tester agents use this to drive the agent loop without invoking a real
LLM. Implements the `Provider` Protocol from `ember.providers.base`.

Usage:

    fp = FakeProvider()
    fp.queue_response(CompletionResult(text="hello", usage=Usage(1, 2, subscription=True)))
    fp.queue_error(RateLimitError("slow down"))

    result = await fp.complete(system="", messages=[Message("user", "hi")])
    # → CompletionResult(text="hello", ...)
    assert fp.calls[0].messages[-1].content == "hi"

Stream-event playback (added for v0.5 stream_handler tests):

    The fake can be configured to emit a sequence of synthetic stream
    events through the supplied `stream_handler` BEFORE returning the
    next CompletionResult. This is how tests exercise the agent's
    `_make_stream_handler` (architecture decision #3 — one status
    message per `tool_use` block) without invoking a real LLM.

    Use `queue_stream_events([...])` to attach an event list to the
    NEXT queued response. Order matters: queue the response first, then
    queue the events for that response. Or pass `events=[...]` directly
    to `queue_response`/`queue_text` via the dedicated helpers
    `queue_response_with_events` / `queue_text_with_events`.

    Example:
        fp.queue_text_with_events(
            "the answer",
            events=[
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "toolu_1",
                     "name": "agent__echo", "input": {"text": "hi"}},
                ]}},
                {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "thinking..."},
                ]}},
            ],
        )
        await fp.complete(system="", messages=[], stream_handler=my_handler)
        # → my_handler awaited once per event, in order; then result returned.

    The fake must NOT inspect or react to the events — it only plays
    them through the handler. The agent's handler is what reacts.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ember.providers.base import (
    Capabilities,
    CompletionResult,
    Message,
    StreamHandler,
    ToolSpec,
    Usage,
)


@dataclass
class _QueuedResponse:
    """Internal: a queued completion plus its stream events."""

    payload: CompletionResult | BaseException
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RecordedCall:
    """One captured invocation of `complete`."""

    system: str
    messages: list[Message]
    tools: list[ToolSpec] | None
    model: str | None
    session_id: str | None
    stream_handler_provided: bool


class FakeProvider:
    """A queue-driven Provider implementation for tests.

    Queue each turn's outcome ahead of time with `queue_response` or
    `queue_error`. The provider pops them in FIFO order on each call to
    `complete`. If the queue is empty, returns a default empty
    CompletionResult.
    """

    name: str = "fake"
    capabilities: Capabilities = Capabilities(tool_use=False, streaming=False)

    def __init__(self) -> None:
        self._queue: deque[_QueuedResponse] = deque()
        self.calls: list[RecordedCall] = []

    # --- Programming API --------------------------------------------------

    def queue_response(self, result: CompletionResult) -> None:
        self._queue.append(_QueuedResponse(payload=result))

    def queue_error(self, exc: BaseException) -> None:
        self._queue.append(_QueuedResponse(payload=exc))

    def queue_text(
        self,
        text: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        subscription: bool = True,
        session_id: str | None = None,
    ) -> None:
        """Convenience for the common case: a plain text reply with
        subscription billing."""
        self._queue.append(
            _QueuedResponse(
                payload=CompletionResult(
                    text=text,
                    usage=Usage(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        subscription=subscription,
                        cost_usd=None if subscription else 0.0,
                    ),
                    session_id=session_id,
                )
            )
        )

    # --- Stream event playback (v0.5) -------------------------------------

    def queue_stream_events(self, events: list[dict[str, Any]]) -> None:
        """Attach a list of synthetic stream events to the LAST queued
        response. Events will be played through the `stream_handler`
        (in order) BEFORE the response is returned.

        Raises if no response has been queued yet — events have to
        belong to a specific completion.
        """
        if not self._queue:
            raise RuntimeError("queue_stream_events requires a queued response/error first")
        self._queue[-1].events.extend(events)

    def queue_response_with_events(
        self,
        result: CompletionResult,
        events: list[dict[str, Any]],
    ) -> None:
        """Queue a CompletionResult plus the events to play through
        `stream_handler` before it's returned."""
        self._queue.append(_QueuedResponse(payload=result, events=list(events)))

    def queue_text_with_events(
        self,
        text: str,
        events: list[dict[str, Any]],
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        subscription: bool = True,
        session_id: str | None = None,
    ) -> None:
        """Convenience: text reply + stream events to play first."""
        self.queue_text(
            text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            subscription=subscription,
            session_id=session_id,
        )
        self._queue[-1].events = list(events)

    # --- Provider protocol ------------------------------------------------

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
        stream_handler: StreamHandler | None = None,
        session_id: str | None = None,
    ) -> CompletionResult:
        self.calls.append(
            RecordedCall(
                system=system,
                messages=list(messages),
                tools=list(tools) if tools is not None else None,
                model=model,
                session_id=session_id,
                stream_handler_provided=stream_handler is not None,
            )
        )
        if not self._queue:
            return CompletionResult(text="", usage=Usage(input_tokens=0, output_tokens=0))
        queued = self._queue.popleft()

        # Play any synthetic events through the supplied handler before
        # returning the payload. The fake does not interpret events; it
        # only plays them. The handler under test is what reacts.
        if stream_handler is not None and queued.events:
            for event in queued.events:
                await stream_handler(event)

        if isinstance(queued.payload, BaseException):
            raise queued.payload
        return queued.payload
