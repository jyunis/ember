"""DeepSeek API provider.

Calls DeepSeek's OpenAI-compatible /chat/completions endpoint via httpx.
Metered (per-token); `Usage.subscription = False`. Tool use is wired
through OpenAI-style function calling — `tools` are forwarded as
`[{type:"function", function:{name, description, parameters}}]` and the
response's `choices[0].message.tool_calls` are parsed back into
`CompletionResult.tool_calls`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ember.errors import AuthError, ProviderError, RateLimitError, TransportError
from ember.providers.base import (
    Capabilities,
    CompletionResult,
    Message,
    StreamHandler,
    ToolCall,
    ToolSpec,
    Usage,
)

_log = logging.getLogger("ember.providers.deepseek_api")

# Per-million-token prices in USD (input cache-miss, output). DeepSeek's
# cache-hit input is much cheaper but the API doesn't always disambiguate
# in the response shape we read; treat all input as cache-miss for cost.
_PRICES_PER_M: dict[str, tuple[float, float]] = {
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
}

_DEFAULT_MAX_TOKENS = 8192


class DeepseekApiProvider:
    """OpenAI-compatible DeepSeek provider."""

    name: str = "deepseek_api"
    capabilities: Capabilities = Capabilities(
        tool_use=True,
        streaming=False,
        prompt_cache=True,
    )

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        default_model: str = "deepseek-chat",
        timeout_s: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._timeout_s = timeout_s

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
        api_messages = self._to_api_messages(system, messages)
        payload: dict[str, Any] = {
            "model": chosen_model,
            "messages": api_messages,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "stream": False,
        }
        api_tools = self._to_api_tools(tools)
        if api_tools:
            payload["tools"] = api_tools
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self._base_url}/chat/completions"

        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as e:
            raise TransportError(f"deepseek_api: timeout: {e}") from e
        except httpx.HTTPError as e:
            raise TransportError(f"deepseek_api: transport: {e}") from e
        except Exception as e:
            raise ProviderError(f"deepseek_api: unexpected: {type(e).__name__}: {e}") from e

        if response.status_code == 401 or response.status_code == 403:
            raise AuthError(f"deepseek_api: auth refused: {response.text[:200]}")
        if response.status_code == 429:
            raise RateLimitError(f"deepseek_api: rate-limited: {response.text[:200]}")
        if response.status_code >= 400:
            raise ProviderError(f"deepseek_api: http_{response.status_code}: {response.text[:300]}")

        try:
            body = response.json()
        except Exception as e:
            raise ProviderError(f"deepseek_api: non-JSON response: {e}") from e

        choices = body.get("choices") or []
        if not choices:
            raise ProviderError(f"deepseek_api: empty choices: {body!r}")
        msg = choices[0].get("message") or {}
        text = msg.get("content") or ""
        tool_calls = self._extract_tool_calls(msg.get("tool_calls"))

        usage = self._build_usage(chosen_model, body.get("usage") or {})
        return CompletionResult(text=text, tool_calls=tool_calls, usage=usage)

    @staticmethod
    def _to_api_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert to OpenAI-compatible message list.

        - System prepended as a `{role:"system"}` entry (vs. anthropic's
          separate `system` field).
        - Assistant turns that carried `tool_calls` are emitted with the
          OpenAI `tool_calls` array attached; `content` may be empty.
        - `role="tool"` history entries are passed through with their
          `tool_call_id` so the API can stitch the result to the right call.
        - Empty-content messages without tool_calls are dropped to keep
          payloads minimal.
        """
        out: list[dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            if m.role == "tool":
                if m.tool_call_id is None:
                    # No way to bind this back to a call — skip rather
                    # than send a malformed payload.
                    continue
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": m.tool_call_id,
                        "content": m.content or "",
                    }
                )
                continue
            if m.role not in ("user", "assistant", "system"):
                continue
            has_tool_calls = bool(m.tool_calls)
            if not m.content and not has_tool_calls:
                continue
            entry: dict[str, Any] = {"role": m.role, "content": m.content or ""}
            if m.role == "assistant" and has_tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.args, ensure_ascii=False),
                        },
                    }
                    for call in (m.tool_calls or [])
                ]
            out.append(entry)
        out = DeepseekApiProvider._drop_dangling_tool_calls(out)
        if not any(m["role"] == "user" for m in out):
            out.append({"role": "user", "content": "(empty)"})
        return out

    @staticmethod
    def _drop_dangling_tool_calls(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Guarantee a valid OpenAI tool-call sequence.

        OpenAI-compatible APIs (DeepSeek included) reject any assistant
        `tool_calls` message that isn't immediately followed by one `tool`
        message per `tool_call_id`, and any `tool` message that doesn't
        answer a preceding call. Our history can violate this — claude
        resolves tools inside its own session (so results never become
        `tool` rows), a turn can be interrupted mid-dispatch, or a `tool`
        row can lose its `tool_call_id` and get dropped above. This matters
        most on FAILOVER, where claude-originated history is handed to
        deepseek verbatim (the 400 that surfaced this).

        Keep complete call→result groups intact (so deepseek-as-primary
        tool use still works); for an incomplete group, strip the
        assistant's `tool_calls` (degrading it to its text, dropped if
        empty) and drop the partial/orphan `tool` messages.
        """
        n = len(entries)
        keep = [True] * n
        strip_calls = [False] * n
        i = 0
        while i < n:
            e = entries[i]
            if e.get("role") == "assistant" and e.get("tool_calls"):
                ids = {tc["id"] for tc in e["tool_calls"]}
                # Scan the contiguous run of tool messages that answers it.
                run = []
                j = i + 1
                while j < n and entries[j].get("role") == "tool":
                    run.append(j)
                    j += 1
                answered = {entries[k].get("tool_call_id") for k in run}
                if ids <= answered:
                    # Complete: keep the calls; drop any orphan tool msgs in
                    # the run whose id wasn't actually requested.
                    for k in run:
                        if entries[k].get("tool_call_id") not in ids:
                            keep[k] = False
                else:
                    # Incomplete: degrade the assistant, drop the whole run.
                    strip_calls[i] = True
                    for k in run:
                        keep[k] = False
                i = j
                continue
            if e.get("role") == "tool":
                # A tool message not consumed by an assistant run above is
                # an orphan (no preceding call) — invalid on its own.
                keep[i] = False
            i += 1

        out: list[dict[str, Any]] = []
        for idx in range(n):
            if not keep[idx]:
                continue
            e = entries[idx]
            if strip_calls[idx]:
                e = {k: v for k, v in e.items() if k != "tool_calls"}
                if not e.get("content"):
                    continue  # nothing left to say
            out.append(e)
        return out

    @staticmethod
    def _to_api_tools(tools: list[ToolSpec] | None) -> list[dict[str, Any]]:
        """Translate ToolSpec list to OpenAI function-tool shape.

        Returns `[]` when no tools were advertised; the caller omits the
        `tools` key entirely in that case so the API knows the turn is
        plain-text.
        """
        if not tools:
            return []
        out: list[dict[str, Any]] = []
        for spec in tools:
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.input_schema or {"type": "object", "properties": {}},
                    },
                }
            )
        return out

    @staticmethod
    def _extract_tool_calls(raw: Any) -> list[ToolCall]:
        """Parse OpenAI-style tool_calls into our `ToolCall` shape.

        Each entry's `function.arguments` is a JSON-encoded string per
        the OpenAI spec. If it fails to parse, surface a single
        `_raw_arguments` key so the tool layer can return a clean error
        instead of dropping the call silently.
        """
        if not raw:
            return []
        if not isinstance(raw, list):
            _log.warning("deepseek_api: tool_calls not a list: %r", raw)
            return []
        out: list[ToolCall] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            call_id = entry.get("id")
            fn = entry.get("function") or {}
            name = fn.get("name")
            if not call_id or not name:
                continue
            args_raw = fn.get("arguments")
            if args_raw is None or args_raw == "":
                args: dict[str, Any] = {}
            elif isinstance(args_raw, dict):
                args = args_raw
            else:
                try:
                    decoded = json.loads(args_raw)
                except (json.JSONDecodeError, TypeError):
                    _log.warning("deepseek_api: tool_call %s args not JSON: %r", call_id, args_raw)
                    args = {"_raw_arguments": str(args_raw)}
                else:
                    args = decoded if isinstance(decoded, dict) else {"_raw_arguments": decoded}
            out.append(ToolCall(id=str(call_id), name=str(name), args=args))
        return out

    @classmethod
    def _build_usage(cls, model: str, api_usage: dict[str, Any]) -> Usage:
        input_tokens = int(api_usage.get("prompt_tokens", 0) or 0)
        output_tokens = int(api_usage.get("completion_tokens", 0) or 0)
        cached = int(api_usage.get("prompt_cache_hit_tokens", 0) or 0)
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached,
            cost_usd=cls._estimate_cost_usd(model, input_tokens, output_tokens),
            subscription=False,
        )

    @staticmethod
    def _estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
        prices = _PRICES_PER_M.get(model)
        if prices is None:
            return None
        in_per_m, out_per_m = prices
        cost = (input_tokens / 1_000_000) * in_per_m + (output_tokens / 1_000_000) * out_per_m
        return round(cost, 6)
