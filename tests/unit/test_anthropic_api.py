"""AnthropicApiProvider (FC-2) — Anthropic SDK-backed provider.

The provider's `complete()` body is `...` until the implementer pass.
These tests pin the FC-2 contract surface against a mocked SDK so the
implementer has a concrete target. We do NOT make real network calls.

Mocking strategy
----------------
The tester barrier allows us to read the imports at the top of
`anthropic_api.py` (interface, not body). At spec time the file's body
is `...`, so the SDK symbol the implementer will use isn't yet imported
— but the architecture doc and the contracts brief both say the
provider uses `anthropic.AsyncAnthropic`. We monkeypatch
`ember.providers.anthropic_api.AsyncAnthropic` (the symbol the
implementer is expected to import). If the implementer chooses a
different symbol name, that's a coupling issue to flag, NOT a license
to read the body — the test is the contract.

If `anthropic` is not installed in the test environment, every test
here is skipped with a clear marker. (`pyproject.toml` includes
`anthropic>=0.40` so under `uv sync` they should run.)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

anthropic = pytest.importorskip("anthropic")

from ember.errors import AuthError, ProviderError, RateLimitError, TransportError  # noqa: E402
from ember.providers.anthropic_api import AnthropicApiProvider  # noqa: E402
from ember.providers.base import Capabilities, CompletionResult, Message  # noqa: E402


# --- Helpers --------------------------------------------------------------


def _fake_messages_response(
    *,
    text: str = "hello back",
    input_tokens: int = 10,
    output_tokens: int = 20,
    stop_reason: str = "end_turn",
) -> MagicMock:
    """Build a fake `Message` object that quacks like
    `anthropic.types.Message` enough for the provider to read text +
    usage out of it.
    """
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = text

    msg = MagicMock()
    msg.id = "msg_test"
    msg.role = "assistant"
    msg.content = [text_block]
    msg.stop_reason = stop_reason
    msg.usage = MagicMock(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return msg


def _patch_async_anthropic(
    monkeypatch: pytest.MonkeyPatch,
    *,
    create_return: Any | None = None,
    create_side_effect: BaseException | None = None,
) -> MagicMock:
    """Make `anthropic.AsyncAnthropic(...)` return a fake client whose
    `.messages.create(...)` returns `create_return` or raises
    `create_side_effect`.

    The implementer accesses the SDK as `anthropic.AsyncAnthropic` (per
    the imports at the top of `anthropic_api.py` — interface-level, not
    body). We patch the SDK module's symbol; `monkeypatch` auto-reverts
    so other tests aren't affected.
    """
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    create_mock = AsyncMock()
    if create_side_effect is not None:
        create_mock.side_effect = create_side_effect
    else:
        create_mock.return_value = create_return
    fake_client.messages.create = create_mock

    fake_class = MagicMock(return_value=fake_client)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", fake_class, raising=True)
    return create_mock


# --- Static surface (FC-2) ------------------------------------------------


class TestStaticSurface:
    def test_name_is_anthropic_api(self) -> None:
        # FC-2 requires a stable provider name; routing strings reference it.
        assert AnthropicApiProvider.name == "anthropic_api"

    def test_capabilities_tool_use_is_false(self) -> None:
        # This v2 surface is text-only: it accepts a `tools` arg but never
        # forwards it to the API and doesn't parse tool_use blocks. The
        # capability must report False so failover/routing never treats it
        # as tool-capable — declaring True caused a conversation to fail
        # over here and silently narrate fake tool calls.
        caps = AnthropicApiProvider.capabilities
        assert isinstance(caps, Capabilities)
        assert caps.tool_use is False

    def test_construction_does_not_raise(self) -> None:
        # FC-2: "No I/O on import. Provider construction is cheap; auth
        # resolution (env-var read) happens on first call."
        AnthropicApiProvider(api_key="sk-x", default_model="claude-sonnet-4-6")


# --- Successful completion path ------------------------------------------


class TestSuccessfulCompletion:
    async def test_returns_completion_result_with_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_async_anthropic(
            monkeypatch,
            create_return=_fake_messages_response(text="canned reply"),
        )
        provider = AnthropicApiProvider(api_key="sk-x")
        result = await provider.complete(system="", messages=[Message("user", "hi")])
        assert isinstance(result, CompletionResult)
        assert "canned reply" in result.text

    async def test_usage_is_populated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_async_anthropic(
            monkeypatch,
            create_return=_fake_messages_response(input_tokens=42, output_tokens=99),
        )
        provider = AnthropicApiProvider(api_key="sk-x")
        result = await provider.complete(system="", messages=[Message("user", "hi")])
        assert result.usage.input_tokens == 42
        assert result.usage.output_tokens == 99

    async def test_metered_billing_subscription_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # FC-2: metered providers set `subscription=False`.
        _patch_async_anthropic(
            monkeypatch,
            create_return=_fake_messages_response(),
        )
        provider = AnthropicApiProvider(api_key="sk-x")
        result = await provider.complete(system="", messages=[Message("user", "hi")])
        assert result.usage.subscription is False

    async def test_metered_billing_cost_usd_not_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # TLC-5 + FC-2: metered providers populate `cost_usd` (non-None,
        # non-negative). The exact number depends on the model; we just
        # require that *something* was computed.
        _patch_async_anthropic(
            monkeypatch,
            create_return=_fake_messages_response(),
        )
        provider = AnthropicApiProvider(api_key="sk-x", default_model="claude-sonnet-4-6")
        result = await provider.complete(system="", messages=[Message("user", "hi")])
        assert result.usage.cost_usd is not None
        assert result.usage.cost_usd >= 0.0


# --- Error mapping (FC-2: never raise raw library exceptions) -----------


class TestErrorMapping:
    """FC-2: 'Errors: RateLimitError, AuthError, TransportError,
    ProviderError (catch-all). Never raise raw library exceptions out
    of complete.'
    """

    async def test_anthropic_rate_limit_maps_to_ember_rate_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Construct an `anthropic.RateLimitError` cheaply. The SDK's
        # constructor takes (message, response, body) — we feed a stub
        # response since the test never hits the network.
        rl = _make_anthropic_error(anthropic.RateLimitError, status_code=429)
        _patch_async_anthropic(monkeypatch, create_side_effect=rl)
        provider = AnthropicApiProvider(api_key="sk-x")
        with pytest.raises(RateLimitError):
            await provider.complete(system="", messages=[Message("user", "hi")])

    async def test_anthropic_auth_maps_to_ember_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ae = _make_anthropic_error(anthropic.AuthenticationError, status_code=401)
        _patch_async_anthropic(monkeypatch, create_side_effect=ae)
        provider = AnthropicApiProvider(api_key="sk-x")
        with pytest.raises(AuthError):
            await provider.complete(system="", messages=[Message("user", "hi")])

    async def test_transport_error_maps_to_ember_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # APIConnectionError wraps the underlying httpx connect failure.
        # SDK's constructor is `APIConnectionError(message, request=...)`.
        try:
            ce = anthropic.APIConnectionError(
                message="conn refused",
                request=MagicMock(),
            )
        except Exception:
            # If the constructor signature doesn't match, fall back to a
            # bare httpx.ConnectError, which the implementer should
            # similarly map.
            import httpx

            ce = httpx.ConnectError("conn refused")
        _patch_async_anthropic(monkeypatch, create_side_effect=ce)
        provider = AnthropicApiProvider(api_key="sk-x")
        with pytest.raises(TransportError):
            await provider.complete(system="", messages=[Message("user", "hi")])

    async def test_unexpected_error_maps_to_provider_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Anything not in the typed set should surface as ProviderError
        # (the FC-2 catch-all), NOT a raw library exception or generic
        # Exception.
        unexpected = RuntimeError("kaboom")
        _patch_async_anthropic(monkeypatch, create_side_effect=unexpected)
        provider = AnthropicApiProvider(api_key="sk-x")
        with pytest.raises(ProviderError):
            await provider.complete(system="", messages=[Message("user", "hi")])


# --- Local helpers --------------------------------------------------------


def _make_anthropic_error(cls: type, *, status_code: int) -> Exception:
    """Construct an anthropic SDK error with whichever argument shape
    the installed version expects. The SDK has changed signatures across
    versions; we try the modern one first, fall back to a generic init.
    """
    try:
        # Modern: `RateLimitError(message, response, body)`
        response = MagicMock()
        response.status_code = status_code
        response.headers = {}
        response.request = MagicMock()
        return cls("rate limited", response=response, body=None)
    except TypeError:
        pass
    try:
        # Older: positional message only
        return cls("rate limited")
    except TypeError:
        # Last resort: bare instantiation
        return cls()
