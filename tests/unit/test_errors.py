"""Error taxonomy — verifies the public surface promised by `ember.errors`.

These types are part of the public contract surface (FC-2, FC-3, FC-1, FC-7,
TLC-15). Tests assert the subclass relationships the agent loop branches on,
plus the small bits of behavior `errors.py` actually defines (the
`RateLimitError.reset_at` carry-over and the `TurnBoundExceeded` constructor).
"""

from __future__ import annotations

import pytest

from ember.errors import (
    AuthError,
    ChannelDisconnected,
    ChannelError,
    ConfigError,
    EmberError,
    ProviderError,
    RateLimitError,
    SchemaVersionError,
    StoreError,
    ToolError,
    ToolRetryable,
    ToolValidationError,
    TransportError,
    TurnBoundExceeded,
)


class TestEmberError:
    def test_is_exception(self) -> None:
        assert issubclass(EmberError, Exception)


class TestProviderErrors:
    def test_provider_error_is_ember(self) -> None:
        assert issubclass(ProviderError, EmberError)

    def test_rate_limit_is_provider(self) -> None:
        # The agent loop's UoW-15 branch hinges on `except RateLimitError`
        # not catching `AuthError` or vice versa.
        assert issubclass(RateLimitError, ProviderError)
        assert not issubclass(AuthError, RateLimitError)
        assert not issubclass(TransportError, RateLimitError)

    def test_auth_is_provider(self) -> None:
        assert issubclass(AuthError, ProviderError)

    def test_transport_is_provider(self) -> None:
        assert issubclass(TransportError, ProviderError)

    def test_rate_limit_default_construction(self) -> None:
        e = RateLimitError()
        assert e.reset_at is None
        # message defaults to "rate limited"
        assert "rate limited" in str(e)

    def test_rate_limit_carries_reset_at(self) -> None:
        e = RateLimitError("slow down", reset_at=12345.0)
        assert e.reset_at == 12345.0
        assert "slow down" in str(e)

    def test_rate_limit_can_be_raised_and_caught_as_provider_error(self) -> None:
        with pytest.raises(ProviderError):
            raise RateLimitError("x")


class TestStoreErrors:
    def test_store_error_is_ember(self) -> None:
        assert issubclass(StoreError, EmberError)

    def test_schema_version_is_store_error(self) -> None:
        assert issubclass(SchemaVersionError, StoreError)


class TestChannelErrors:
    def test_channel_error_is_ember(self) -> None:
        assert issubclass(ChannelError, EmberError)

    def test_channel_disconnected_is_channel_error(self) -> None:
        assert issubclass(ChannelDisconnected, ChannelError)


class TestToolErrors:
    def test_tool_error_is_ember(self) -> None:
        assert issubclass(ToolError, EmberError)

    def test_retryable_is_tool_error(self) -> None:
        # The agent loop's retry policy branches on this hierarchy.
        assert issubclass(ToolRetryable, ToolError)

    def test_validation_is_tool_error(self) -> None:
        assert issubclass(ToolValidationError, ToolError)


class TestConfigError:
    def test_config_error_is_ember(self) -> None:
        assert issubclass(ConfigError, EmberError)


class TestTurnBoundExceeded:
    def test_is_ember(self) -> None:
        assert issubclass(TurnBoundExceeded, EmberError)

    def test_carries_bound_and_limit(self) -> None:
        e = TurnBoundExceeded("provider_calls", 25)
        assert e.bound == "provider_calls"
        assert e.limit == 25
        assert "provider_calls" in str(e)
        assert "25" in str(e)

    def test_is_not_provider_error(self) -> None:
        # TLC-15: bound exceeded is the agent loop's own concept, not a
        # provider failure. Catch hierarchies must not conflate them.
        assert not issubclass(TurnBoundExceeded, ProviderError)
