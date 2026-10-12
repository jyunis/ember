"""DeepseekApiProvider — OpenAI-compatible tool use over /chat/completions.

Pins the FC-2 contract surface for the deepseek_api provider:
- text-only round trip works
- tools forwarded in payload as OpenAI function shape
- response `tool_calls` parsed back into `CompletionResult.tool_calls`
- malformed function.arguments JSON degrades safely
- role="tool" history entries pass through with `tool_call_id`
- HTTP status → typed Ember error mapping

Mocking strategy: monkeypatch `httpx.AsyncClient` on the provider's
module so the real network is never hit. The fake client records the
last request payload so assertions can inspect it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ember.errors import AuthError, ProviderError, RateLimitError
from ember.providers import deepseek_api as ds_mod
from ember.providers.base import (
    Capabilities,
    CompletionResult,
    Message,
    ToolCall,
    ToolSpec,
)
from ember.providers.deepseek_api import DeepseekApiProvider


# --- Fakes ---------------------------------------------------------------


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, body: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeAsyncClient:
    """Records the last POST and returns a configured response."""

    last_payload: dict[str, Any] | None = None
    last_headers: dict[str, str] | None = None
    last_url: str | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str]
    ) -> _FakeResponse:
        _FakeAsyncClient.last_url = url
        _FakeAsyncClient.last_payload = json
        _FakeAsyncClient.last_headers = headers
        return _FakeAsyncClient._response


def _install_fake(
    monkeypatch: pytest.MonkeyPatch, response: _FakeResponse
) -> type[_FakeAsyncClient]:
    _FakeAsyncClient._response = response  # type: ignore[attr-defined]
    _FakeAsyncClient.last_payload = None
    _FakeAsyncClient.last_headers = None
    _FakeAsyncClient.last_url = None
    monkeypatch.setattr(ds_mod.httpx, "AsyncClient", _FakeAsyncClient, raising=True)
    return _FakeAsyncClient


def _completion_body(
    *,
    content: str | None = "hi",
    tool_calls: list[dict[str, Any]] | None = None,
    input_tokens: int = 5,
    output_tokens: int = 7,
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {
        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens},
    }


# --- Static surface ------------------------------------------------------


class TestStaticSurface:
    def test_name(self) -> None:
        assert DeepseekApiProvider.name == "deepseek_api"

    def test_capabilities_advertise_tool_use(self) -> None:
        caps = DeepseekApiProvider.capabilities
        assert isinstance(caps, Capabilities)
        assert caps.tool_use is True


# --- Text-only round trip ------------------------------------------------


class TestPlainText:
    async def test_returns_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake(monkeypatch, _FakeResponse(body=_completion_body(content="hello back")))
        provider = DeepseekApiProvider(api_key="sk-x")
        result = await provider.complete(system="sys", messages=[Message("user", "hi")])
        assert isinstance(result, CompletionResult)
        assert result.text == "hello back"
        assert result.tool_calls == []
        assert result.usage.subscription is False

    async def test_omits_tools_field_when_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _install_fake(monkeypatch, _FakeResponse(body=_completion_body()))
        provider = DeepseekApiProvider(api_key="sk-x")
        await provider.complete(system="", messages=[Message("user", "hi")], tools=None)
        assert fake.last_payload is not None
        assert "tools" not in fake.last_payload


# --- Tool forwarding (request side) --------------------------------------


class TestToolForwarding:
    async def test_tools_translated_to_openai_function_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _install_fake(monkeypatch, _FakeResponse(body=_completion_body()))
        provider = DeepseekApiProvider(api_key="sk-x")
        spec = ToolSpec(
            name="get_weather",
            description="lookup",
            input_schema={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        )
        await provider.complete(system="", messages=[Message("user", "hi")], tools=[spec])
        assert fake.last_payload is not None
        tools_field = fake.last_payload.get("tools")
        assert tools_field == [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ]

    async def test_empty_input_schema_defaults_to_object(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _install_fake(monkeypatch, _FakeResponse(body=_completion_body()))
        provider = DeepseekApiProvider(api_key="sk-x")
        await provider.complete(
            system="",
            messages=[Message("user", "hi")],
            tools=[ToolSpec(name="noop", description="", input_schema={})],
        )
        assert fake.last_payload is not None
        params = fake.last_payload["tools"][0]["function"]["parameters"]
        assert params == {"type": "object", "properties": {}}


# --- Tool calls (response side) ------------------------------------------


class TestToolCallParsing:
    async def test_well_formed_tool_calls_round_trip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake(
            monkeypatch,
            _FakeResponse(
                body=_completion_body(
                    content=None,
                    tool_calls=[
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": json.dumps({"city": "Paris"}),
                            },
                        }
                    ],
                )
            ),
        )
        provider = DeepseekApiProvider(api_key="sk-x")
        result = await provider.complete(system="", messages=[Message("user", "weather?")])
        assert result.text == ""
        assert result.tool_calls == [
            ToolCall(id="call_abc", name="get_weather", args={"city": "Paris"})
        ]

    async def test_missing_id_or_name_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake(
            monkeypatch,
            _FakeResponse(
                body=_completion_body(
                    content=None,
                    tool_calls=[
                        {
                            "id": "ok",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{}"},
                        },
                        {"function": {"name": "no_id", "arguments": "{}"}},
                        {"id": "no_name", "function": {"arguments": "{}"}},
                    ],
                )
            ),
        )
        provider = DeepseekApiProvider(api_key="sk-x")
        result = await provider.complete(system="", messages=[Message("user", "x")])
        assert [c.id for c in result.tool_calls] == ["ok"]

    async def test_malformed_arguments_surface_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake(
            monkeypatch,
            _FakeResponse(
                body=_completion_body(
                    content=None,
                    tool_calls=[
                        {
                            "id": "call_bad",
                            "function": {"name": "f", "arguments": "not-json{"},
                        }
                    ],
                )
            ),
        )
        provider = DeepseekApiProvider(api_key="sk-x")
        result = await provider.complete(system="", messages=[Message("user", "x")])
        assert len(result.tool_calls) == 1
        call = result.tool_calls[0]
        assert call.id == "call_bad"
        assert call.args == {"_raw_arguments": "not-json{"}

    async def test_empty_arguments_decodes_to_empty_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake(
            monkeypatch,
            _FakeResponse(
                body=_completion_body(
                    content=None,
                    tool_calls=[{"id": "c", "function": {"name": "f", "arguments": ""}}],
                )
            ),
        )
        provider = DeepseekApiProvider(api_key="sk-x")
        result = await provider.complete(system="", messages=[Message("user", "x")])
        assert result.tool_calls[0].args == {}


# --- History conversion --------------------------------------------------


class TestHistoryConversion:
    async def test_assistant_tool_call_history_emits_tool_calls_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _install_fake(monkeypatch, _FakeResponse(body=_completion_body()))
        provider = DeepseekApiProvider(api_key="sk-x")
        history = [
            Message("user", "weather?"),
            Message(
                "assistant",
                "",
                tool_calls=[ToolCall(id="call_1", name="get_weather", args={"city": "Paris"})],
            ),
            Message("tool", '{"temp_c": 12}', tool_call_id="call_1"),
        ]
        await provider.complete(system="", messages=history)
        assert fake.last_payload is not None
        msgs = fake.last_payload["messages"]
        # user, assistant(with tool_calls), tool(with tool_call_id)
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["tool_calls"][0]["id"] == "call_1"
        assert msgs[1]["tool_calls"][0]["function"]["name"] == "get_weather"
        assert json.loads(msgs[1]["tool_calls"][0]["function"]["arguments"]) == {"city": "Paris"}
        assert msgs[2] == {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": '{"temp_c": 12}',
        }

    async def test_tool_message_without_id_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _install_fake(monkeypatch, _FakeResponse(body=_completion_body()))
        provider = DeepseekApiProvider(api_key="sk-x")
        await provider.complete(
            system="",
            messages=[
                Message("user", "x"),
                Message("tool", "orphan-result", tool_call_id=None),
            ],
        )
        assert fake.last_payload is not None
        roles = [m["role"] for m in fake.last_payload["messages"]]
        assert "tool" not in roles


def _assert_valid_openai_tool_sequence(msgs: list[dict[str, Any]]) -> None:
    """Every assistant `tool_calls` is immediately followed by one `tool`
    message per id; no `tool` message is an orphan. Mirrors what
    OpenAI-compatible endpoints enforce."""
    n = len(msgs)
    i = 0
    while i < n:
        m = msgs[i]
        if m["role"] == "assistant" and m.get("tool_calls"):
            ids = {tc["id"] for tc in m["tool_calls"]}
            answered = set()
            j = i + 1
            while j < n and msgs[j]["role"] == "tool":
                answered.add(msgs[j]["tool_call_id"])
                j += 1
            assert ids <= answered, f"dangling tool_calls at {i}: {ids - answered}"
            i = j
            continue
        assert m["role"] != "tool", f"orphan tool message at {i}"
        i += 1


class TestDanglingToolCalls:
    """Failover hands claude-originated history to deepseek; it must be
    coerced into a valid OpenAI tool sequence (the 400 we hit)."""

    def test_dangling_assistant_tool_calls_no_followup_is_dropped(self) -> None:
        msgs = DeepseekApiProvider._to_api_messages(
            "",
            [
                Message("user", "find me a recipe"),
                Message(
                    "assistant",
                    "",  # tool_use with no text, no result row persisted
                    tool_calls=[ToolCall(id="c1", name="mealie_search", args={"q": "pesto"})],
                ),
            ],
        )
        _assert_valid_openai_tool_sequence(msgs)
        assert not any(m.get("tool_calls") for m in msgs)
        assert all(m["role"] != "tool" for m in msgs)

    def test_dangling_with_text_degrades_to_plain_assistant(self) -> None:
        msgs = DeepseekApiProvider._to_api_messages(
            "",
            [
                Message("user", "hi"),
                Message(
                    "assistant",
                    "let me check that",
                    tool_calls=[ToolCall(id="c1", name="x", args={})],
                ),
            ],
        )
        _assert_valid_openai_tool_sequence(msgs)
        asst = [m for m in msgs if m["role"] == "assistant"]
        assert len(asst) == 1
        assert asst[0]["content"] == "let me check that"
        assert "tool_calls" not in asst[0]

    def test_orphan_tool_message_with_id_dropped(self) -> None:
        msgs = DeepseekApiProvider._to_api_messages(
            "",
            [
                Message("user", "x"),
                Message("tool", "result with no preceding call", tool_call_id="ghost"),
            ],
        )
        _assert_valid_openai_tool_sequence(msgs)
        assert all(m["role"] != "tool" for m in msgs)

    def test_partial_group_is_stripped(self) -> None:
        # assistant requests two calls, only one result present → incomplete.
        msgs = DeepseekApiProvider._to_api_messages(
            "",
            [
                Message("user", "x"),
                Message(
                    "assistant",
                    "",
                    tool_calls=[
                        ToolCall(id="c1", name="a", args={}),
                        ToolCall(id="c2", name="b", args={}),
                    ],
                ),
                Message("tool", "only c1 answered", tool_call_id="c1"),
            ],
        )
        _assert_valid_openai_tool_sequence(msgs)
        assert not any(m.get("tool_calls") for m in msgs)
        assert all(m["role"] != "tool" for m in msgs)

    def test_complete_group_preserved(self) -> None:
        msgs = DeepseekApiProvider._to_api_messages(
            "",
            [
                Message("user", "weather?"),
                Message(
                    "assistant",
                    "",
                    tool_calls=[ToolCall(id="c1", name="get_weather", args={"city": "Paris"})],
                ),
                Message("tool", '{"temp_c": 12}', tool_call_id="c1"),
                Message("assistant", "It's 12°C in Paris."),
            ],
        )
        _assert_valid_openai_tool_sequence(msgs)
        asst_with_calls = [m for m in msgs if m["role"] == "assistant" and m.get("tool_calls")]
        assert len(asst_with_calls) == 1
        assert asst_with_calls[0]["tool_calls"][0]["id"] == "c1"
        assert any(m["role"] == "tool" and m["tool_call_id"] == "c1" for m in msgs)


# --- Error mapping -------------------------------------------------------


class TestErrorMapping:
    async def test_401_maps_to_auth_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake(monkeypatch, _FakeResponse(status_code=401, body={"error": "no key"}))
        provider = DeepseekApiProvider(api_key="bad")
        with pytest.raises(AuthError):
            await provider.complete(system="", messages=[Message("user", "x")])

    async def test_429_maps_to_rate_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake(monkeypatch, _FakeResponse(status_code=429, body={"error": "slow down"}))
        provider = DeepseekApiProvider(api_key="sk-x")
        with pytest.raises(RateLimitError):
            await provider.complete(system="", messages=[Message("user", "x")])

    async def test_5xx_maps_to_provider_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake(monkeypatch, _FakeResponse(status_code=503, body={"error": "down"}))
        provider = DeepseekApiProvider(api_key="sk-x")
        with pytest.raises(ProviderError):
            await provider.complete(system="", messages=[Message("user", "x")])
