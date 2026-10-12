"""Provider contract (FC-2) — verified via the FakeProvider stub.

We can't test real providers without invoking real backends, but we can
test that any provider conforming to the protocol obeys the FC-2
invariants — and that the fake the rest of the suite leans on actually
satisfies them. If the fake drifts, every other test that depends on it
gets a misleading green.

Specifically:
- FC-2: every CompletionResult has `usage` populated.
- FC-2: subscription providers set cost_usd=None, subscription=True.
- FC-2: complete returns one full result OR raises a typed error
  (RateLimitError, AuthError, TransportError, ProviderError); never
  raw library exceptions.
- The runtime-checkable Protocol accepts the fake as a Provider.
"""

from __future__ import annotations

import pytest

from ember.errors import AuthError, ProviderError, RateLimitError, TransportError
from ember.providers.base import (
    Capabilities,
    CompletionResult,
    Message,
    Provider,
    Usage,
)
from tests.fakes.fake_provider import FakeProvider


# --- Protocol conformance -------------------------------------------------


def test_fake_provider_satisfies_protocol() -> None:
    fp = FakeProvider()
    # runtime_checkable Protocol — duck-typed isinstance.
    assert isinstance(fp, Provider)


def test_fake_provider_exposes_required_attributes() -> None:
    fp = FakeProvider()
    assert isinstance(fp.name, str) and fp.name
    assert isinstance(fp.capabilities, Capabilities)


# --- FC-2: Usage shape invariants ----------------------------------------


class TestUsageShape:
    """The fake's `queue_text` is the canonical shortcut for tests; if it
    enforces FC-2 here, every test that uses it transitively gets the
    invariant."""

    async def test_subscription_text_has_cost_none(self) -> None:
        fp = FakeProvider()
        fp.queue_text("hi", input_tokens=3, output_tokens=4, subscription=True)
        result = await fp.complete(system="", messages=[])
        assert result.usage.subscription is True
        assert result.usage.cost_usd is None
        assert result.usage.input_tokens == 3
        assert result.usage.output_tokens == 4

    async def test_metered_text_has_cost_value(self) -> None:
        fp = FakeProvider()
        fp.queue_text("hi", subscription=False)
        result = await fp.complete(system="", messages=[])
        assert result.usage.subscription is False
        assert result.usage.cost_usd is not None  # at least set, even if 0

    async def test_default_completion_result_has_usage(self) -> None:
        # Empty queue → default CompletionResult; FC-2 still says usage
        # is populated (zero-valued).
        fp = FakeProvider()
        result = await fp.complete(system="", messages=[])
        assert result.usage is not None
        assert isinstance(result.usage, Usage)

    async def test_explicit_completion_result_with_subscription_usage(self) -> None:
        fp = FakeProvider()
        fp.queue_response(
            CompletionResult(
                text="ok",
                usage=Usage(input_tokens=10, output_tokens=20, subscription=True),
            )
        )
        result = await fp.complete(system="", messages=[])
        assert result.usage.subscription is True
        # Per FC-2, subscription → cost_usd None. Usage's default
        # cost_usd is None, so this just confirms the dataclass default
        # is consistent with FC-2.
        assert result.usage.cost_usd is None


# --- FC-2: error surface --------------------------------------------------


class TestErrorPropagation:
    async def test_rate_limit_propagates(self) -> None:
        fp = FakeProvider()
        fp.queue_error(RateLimitError("slow down", reset_at=100.0))
        with pytest.raises(RateLimitError) as ei:
            await fp.complete(system="", messages=[])
        assert ei.value.reset_at == 100.0

    async def test_auth_error_propagates(self) -> None:
        fp = FakeProvider()
        fp.queue_error(AuthError("bad key"))
        with pytest.raises(AuthError):
            await fp.complete(system="", messages=[])

    async def test_transport_error_propagates(self) -> None:
        fp = FakeProvider()
        fp.queue_error(TransportError("conn reset"))
        with pytest.raises(TransportError):
            await fp.complete(system="", messages=[])

    async def test_generic_provider_error_propagates(self) -> None:
        fp = FakeProvider()
        fp.queue_error(ProviderError("misc"))
        with pytest.raises(ProviderError):
            await fp.complete(system="", messages=[])


# --- Recording / call inspection -----------------------------------------


class TestCallRecording:
    async def test_records_system_prompt(self) -> None:
        fp = FakeProvider()
        fp.queue_text("ok")
        await fp.complete(system="SOUL+PROFILE", messages=[Message("user", "hi")])
        assert len(fp.calls) == 1
        assert fp.calls[0].system == "SOUL+PROFILE"

    async def test_records_messages_in_order(self) -> None:
        fp = FakeProvider()
        fp.queue_text("ok")
        msgs = [
            Message("user", "first"),
            Message("assistant", "reply"),
            Message("user", "second"),
        ]
        await fp.complete(system="", messages=msgs)
        assert [m.content for m in fp.calls[0].messages] == ["first", "reply", "second"]

    async def test_records_optional_args(self) -> None:
        fp = FakeProvider()
        fp.queue_text("ok")
        await fp.complete(system="", messages=[], model="claude-sonnet", session_id="abc")
        rc = fp.calls[0]
        assert rc.model == "claude-sonnet"
        assert rc.session_id == "abc"
        assert rc.stream_handler_provided is False
