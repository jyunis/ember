"""Anthropic API provider.

Calls the Messages API via the `anthropic` Python SDK. Metered
(per-token); `Usage.subscription = False`, `cost_usd` populated from
the model's published prices applied to the response's reported usage.

v2 surface is text-only — `tools` argument is accepted but not forwarded
to the API. v3 will wire tool use through the provider's native tool
schema (decision deferred to keep v2 scope narrow).
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic
import httpx

from ember.errors import AuthError, ProviderError, RateLimitError, TransportError
from ember.providers.base import (
    Capabilities,
    CompletionResult,
    Message,
    StreamHandler,
    ToolSpec,
    Usage,
)

_log = logging.getLogger("ember.providers.anthropic_api")

# Approximate per-million-token prices in USD (input, output).
# Coarse — refine when prices change. Returning None for unknown models
# is preferable to misleading numbers.
_PRICES_PER_M: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet": (3.0, 15.0),
    "sonnet": (3.0, 15.0),
    "claude-opus-4-7": (15.0, 75.0),
    "opus": (15.0, 75.0),
    "claude-haiku-4-5-20251001": (0.80, 4.0),
    "haiku": (0.80, 4.0),
}

_DEFAULT_MAX_TOKENS = 8192


class AnthropicApiProvider:
    """anthropic SDK-backed provider."""

    name: str = "anthropic_api"
    # tool_use is False: this v2 surface is text-only — it accepts a
    # `tools` arg but never forwards it to the API and doesn't parse
    # tool_use/tool_result blocks (see `complete`). Declaring True caused
    # a conversation that failed over here to silently lose all tools and
    # start *narrating* fake tool calls in prose. Used for cheap-tier
    # text tasks (compaction/profile extraction), not tool work.
    capabilities: Capabilities = Capabilities(
        tool_use=False,
        streaming=True,
        prompt_cache=True,
    )

    def __init__(
        self,
        *,
        api_key: str,
        default_model: str = "claude-sonnet-4-6",
    ) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._default_model = default_model

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
        stream_handler: StreamHandler | None = None,
        session_id: str | None = None,
    ) -> CompletionResult:
        chosen_model = model or self._default_model
        api_messages = self._to_api_messages(messages)
        kwargs: dict[str, Any] = {
            "model": chosen_model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "messages": api_messages,
        }
        if system:
            kwargs["system"] = system

        try:
            response = await self._client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise RateLimitError(str(e)) from e
        except anthropic.AuthenticationError as e:
            raise AuthError(str(e)) from e
        except anthropic.APIConnectionError as e:
            raise TransportError(str(e)) from e
        except httpx.HTTPError as e:
            raise TransportError(str(e)) from e
        except anthropic.APIError as e:
            raise ProviderError(str(e)) from e
        except Exception as e:
            # FC-2: never raise raw library exceptions out of `complete`.
            raise ProviderError(f"unexpected error: {type(e).__name__}: {e}") from e

        text = self._extract_text(response.content)
        usage = self._build_usage(chosen_model, response.usage)
        return CompletionResult(text=text, usage=usage)

    @staticmethod
    def _to_api_messages(messages: list[Message]) -> list[dict[str, Any]]:
        """Convert our Message dataclasses to anthropic's dict format.

        v2 is text-only — tool_use / tool_result blocks aren't carried
        through. Filter to user/assistant only and skip empty content.
        Anthropic requires alternating roles starting with user; we drop
        leading assistants and consecutive same-role runs by keeping
        the latest in each run.
        """
        filtered: list[dict[str, Any]] = []
        for m in messages:
            if m.role not in ("user", "assistant"):
                continue
            if not m.content:
                continue
            filtered.append({"role": m.role, "content": m.content})

        # Drop leading assistants.
        while filtered and filtered[0]["role"] == "assistant":
            filtered.pop(0)

        # If empty (no user messages at all), inject a stub.
        if not filtered:
            return [{"role": "user", "content": "(empty)"}]

        return filtered

    @staticmethod
    def _extract_text(content_blocks: list[Any]) -> str:
        parts: list[str] = []
        for block in content_blocks:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                parts.append(getattr(block, "text", ""))
        return "".join(parts)

    @classmethod
    def _build_usage(cls, model: str, api_usage: Any) -> Usage:
        input_tokens = int(getattr(api_usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(api_usage, "output_tokens", 0) or 0)
        cached = int(getattr(api_usage, "cache_read_input_tokens", 0) or 0)
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached,
            cost_usd=cls._estimate_cost_usd(model, input_tokens, output_tokens),
            subscription=False,
        )

    @staticmethod
    def _estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
        prices = _PRICES_PER_M.get(model) or _PRICES_PER_M.get(model.removeprefix("claude-"))
        if prices is None:
            return None
        in_per_m, out_per_m = prices
        cost = (input_tokens / 1_000_000) * in_per_m + (output_tokens / 1_000_000) * out_per_m
        return round(cost, 6)
