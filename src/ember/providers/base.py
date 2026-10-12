"""Provider interface.

Every LLM backend implements `Provider` and returns a `CompletionResult`
or raises a typed error (see ember.errors).

See contracts doc § FC-2.

This file is interface-only. Implementations live alongside it
(claude_cli.py, anthropic_api.py, ...). Tester agents may read this file;
they MUST NOT read implementation files.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol, runtime_checkable

# v16 (FC-33 / TLC-28): the pinned join rule between a result's
# `text_parts` and its `text`. Providers that report block structure MUST
# guarantee `text == TEXT_PARTS_SEPARATOR.join(text_parts)`; the agent's
# final send joins the unstreamed remainder `text_parts[posted:]` with the
# same separator, which is what makes count-based exactly-once delivery
# hold with zero character math.
TEXT_PARTS_SEPARATOR: Final[str] = "\n\n"

# --- Message shapes --------------------------------------------------------

Role = Literal["user", "assistant", "system", "tool"]


@dataclass(frozen=True, slots=True)
class Message:
    """A single message in a conversation, normalized across providers.

    `tool_calls` and `tool_call_id` are populated only when the message
    is part of a tool-use exchange; they are unused in v0.
    """

    role: Role
    content: str
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCall:
    """An invocation of a tool by the model. v0 has no tools; lands now
    so the agent loop and provider results can carry the field through.

    `status` is set only for calls the provider's backend already
    executed (bridged claude_cli turns, UoW-4 step 5): "ok" or "error"
    per the observed tool_result. It stays None for in-process dispatch
    (results land in tool-role rows) and for old persisted rows.
    """

    id: str
    name: str
    args: dict[str, Any]
    status: str | None = None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Tool definition advertised to a provider. Anthropic-format
    internally; providers translate at their boundary."""

    name: str
    description: str
    input_schema: dict[str, Any]


# --- Usage / result --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Usage:
    """Per-completion token + cost accounting (TLC-5).

    Subscription-billed providers (claude_cli) MUST set
    `subscription=True` and `cost_usd=None`. Metered providers set
    `subscription=False` and a non-None `cost_usd`.

    v15 (FC-28 / TLC-24): `cache_creation_tokens` carries the cache-write
    input tokens providers report separately from cache reads
    (`cache_creation_input_tokens` on claude_cli). Providers that don't
    report it leave the default 0.
    """

    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float | None = None
    subscription: bool = False


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """The single return shape from `Provider.complete`.

    `tool_calls` are calls the model is REQUESTING — the agent loop
    dispatches them (UoW-2). `executed_tool_calls` are calls the
    provider's backend ALREADY ran during the completion (claude CLI
    via the MCP bridge, UoW-4): audit-only — the loop persists them to
    `tool_calls_json` (TLC-8) and must never dispatch them.
    """

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    executed_tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=lambda: Usage(input_tokens=0, output_tokens=0))
    # Provider-internal continuation handle (e.g. claude CLI session ID).
    # Opaque to the agent loop except as a key to pass back on the next call.
    session_id: str | None = None
    # v15 (FC-28 / TLC-24): the model's true context size for this turn —
    # input + cache_read + cache_creation tokens from the turn's FINAL
    # assistant event. None when the provider doesn't measure it (every
    # non-claude_cli provider) — the agent leaves the conversation's
    # stored value untouched in that case.
    context_tokens: int | None = None
    # v16 (FC-33 / TLC-28): the reply as an ordered tuple of the turn's
    # non-empty assistant text blocks, in stream order. Invariant:
    # `text == TEXT_PARTS_SEPARATOR.join(text_parts)` whenever this is
    # not None. None means the provider doesn't report block structure
    # (every non-claude_cli provider, including the deepseek failover) —
    # the agent then uses the legacy single final send.
    text_parts: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a provider supports. Used by routing + tool-loop selection."""

    tool_use: bool = False
    streaming: bool = False
    multimodal: bool = False
    prompt_cache: bool = False
    native_mcp: bool = False


# --- Stream events (forward-looking; v0 buffers) --------------------------


StreamEvent = dict[str, Any]
StreamHandler = Callable[[StreamEvent], Awaitable[None]]


# --- Provider protocol -----------------------------------------------------


@runtime_checkable
class Provider(Protocol):
    """The single internal interface every provider implements."""

    name: str
    capabilities: Capabilities

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
        stream_handler: StreamHandler | None = None,
        session_id: str | None = None,
    ) -> CompletionResult:
        """Run one provider call.

        Args:
            system: System prompt block (cached where the backend supports
                it). MUST be byte-stable across consecutive calls when its
                inputs are unchanged (TLC-12).
            messages: Conversation history, oldest-first. Implementations
                map this to the provider's native shape.
            tools: Tool specs to advertise to the model. None or empty
                means tool use is disabled for this call.
            model: Provider-specific model identifier; None means use the
                provider's configured default.
            stream_handler: Optional async callback invoked with provider
                stream events as they arrive. If None, the call is buffered.
            session_id: Optional opaque continuation handle from a previous
                CompletionResult.session_id. Providers that don't use it
                ignore it.

        Returns:
            A complete CompletionResult. Partial results are not returned
            on failure — the call either yields a full result or raises.

        Raises:
            RateLimitError: provider declined for rate-limit reasons.
            AuthError: auth missing/invalid.
            TransportError: network failure after the provider's internal
                retry budget is exhausted.
            ProviderError: any other provider-side failure.
        """
        ...
