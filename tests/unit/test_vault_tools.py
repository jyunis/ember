"""Vault tools (FC-9, TLC-4, UoW-13).

Per FC-9:
- Single broker dependency. The two tools (`vault_list`, `vault_get`)
  are the only paths in the agent that talk to the broker.
- Token from env (`VAULT_BROKER_TOKEN`); construction takes the
  resolved value.
- Redaction by default — `vault_get` returns an elided form unless
  `show_full=true`.
- Errors are typed: `VaultNotFound`, `VaultForbidden`,
  `VaultBrokerUnreachable`. (Spec ambiguity flagged: those types are
  not defined in `errors.py`. We accept any `ToolError` subclass with
  a clear message until the implementer pins it.)

TLC-4 (secrets handling):
- The plaintext value never appears in logs.
- The `ToolResult.output` for an elided lookup never contains the
  plaintext.

Tester-flagged ambiguities:
- HTTP client choice: implementer may use `httpx` or `aiohttp`. We
  monkeypatch `httpx.AsyncClient` (the standard async HTTP client
  already pulled in via `mcp[cli]` and `pydantic`). FLAG: if the
  implementer picks `aiohttp`, these tests need adjustment to patch
  `aiohttp.ClientSession` instead.
- Specific error type: `VaultNotFound` etc. are not in `errors.py`
  yet. Tests assert "ToolError-or-subclass with a meaningful message"
  and flag.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from ember.errors import ToolError
from ember.tools.vault import VaultGetTool, VaultListTool

# A sentinel value used in TLC-4 negative tests. Must be unique enough
# to find via a substring scan of logs/output and confirm absence.
SENTINEL = "SENTINEL_DO_NOT_LEAK_xa9k2j"


# --- Stub HTTP responses -------------------------------------------------


class _StubResponse:
    """Minimal `httpx.Response`-shaped stub.

    Mirrors only what the vault tools should reasonably touch:
    `status_code`, `json()`, `raise_for_status()`, `text`.
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: Any = None,
        text: str | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://stub/")
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )


class _StubClient:
    """Drop-in for `httpx.AsyncClient(...)` for tests.

    Records the args of `get` calls so we can assert URL + headers shape.
    `responses_for(url_substr)` registers the response to return for
    URLs containing `url_substr`. Falls through to a 404 by default.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # The vault tools may use `async with` or just `await client.get(...)`.
    # Support both shapes.
    async def __aenter__(self) -> "_StubClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def aclose(self) -> None:
        return None

    # Per-test registration. The fixture below sets these.
    _registered: list[tuple[str, _StubResponse]] = []

    @classmethod
    def reset(cls) -> None:
        cls._registered = []

    @classmethod
    def register(cls, url_substr: str, response: _StubResponse) -> None:
        cls._registered.append((url_substr, response))

    async def get(self, url: str, **kwargs: Any) -> _StubResponse:
        self.calls.append((url, kwargs))
        for substr, resp in type(self)._registered:
            if substr in url:
                return resp
        # Default: 404
        return _StubResponse(status_code=404, payload={"error": "not_found"})


@pytest.fixture
def stub_httpx(monkeypatch: pytest.MonkeyPatch) -> type[_StubClient]:
    _StubClient.reset()
    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
    return _StubClient


# --- VaultListTool -------------------------------------------------------


class TestVaultList:
    async def test_returns_credential_names(self, stub_httpx: type[_StubClient]) -> None:
        # Broker emits a list of names; the exact wrapper is up to the
        # implementer (could be `{"items": [...]}` or just `[...]`). We
        # only assert names round-trip.
        stub_httpx.register(
            "/secrets",
            _StubResponse(payload={"items": ["Netflix", "Schwab API", "GitHub"]}),
        )
        tool = VaultListTool(broker_url="http://broker.local", token="abc")
        result = await tool.call({})
        blob = repr(result.output)
        assert "Netflix" in blob
        assert "GitHub" in blob

    async def test_returns_tool_result(self, stub_httpx: type[_StubClient]) -> None:
        from ember.tools.base import ToolResult

        stub_httpx.register("/secrets", _StubResponse(payload={"items": []}))
        tool = VaultListTool(broker_url="http://broker.local", token="abc")
        result = await tool.call({})
        assert isinstance(result, ToolResult)


# --- VaultGetTool — redaction by default --------------------------------


class TestVaultGetRedaction:
    async def test_default_elides_long_value(self, stub_httpx: type[_StubClient]) -> None:
        # 13-char value: elision is `****t123` (last 4 chars after ****).
        # FC-9: redaction by default.
        stub_httpx.register("/secrets/", _StubResponse(payload={"value": "supersecret123"}))
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        result = await tool.call({"item": "Netflix"})
        blob = repr(result.output)
        # Elided form must appear; full plaintext must not.
        assert "****t123" in blob or "****" in blob
        assert "supersecret123" not in blob

    async def test_default_elides_short_value(self, stub_httpx: type[_StubClient]) -> None:
        # < 8 chars → just `****` per the `_elide` docstring.
        stub_httpx.register("/secrets/", _StubResponse(payload={"value": "short"}))
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        result = await tool.call({"item": "tiny"})
        blob = repr(result.output)
        assert "****" in blob
        assert "short" not in blob

    async def test_show_full_returns_plaintext(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register("/secrets/", _StubResponse(payload={"value": "supersecret123"}))
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        result = await tool.call({"item": "Netflix", "show_full": True})
        blob = repr(result.output)
        assert "supersecret123" in blob

    async def test_tlc4_sentinel_never_in_elided_output(
        self, stub_httpx: type[_StubClient]
    ) -> None:
        # TLC-4 negative test: with a known sentinel in the broker
        # response, the elided ToolResult must NOT contain the sentinel.
        stub_httpx.register("/secrets/", _StubResponse(payload={"value": SENTINEL}))
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        result = await tool.call({"item": "Netflix"})
        assert SENTINEL not in repr(result.output)
        assert SENTINEL not in str(result.output)

    async def test_tlc4_sentinel_never_in_logs(
        self,
        stub_httpx: type[_StubClient],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # TLC-4: even logs must not contain the sentinel.
        stub_httpx.register("/secrets/", _StubResponse(payload={"value": SENTINEL}))
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        with caplog.at_level(logging.DEBUG):
            await tool.call({"item": "Netflix"})
        for record in caplog.records:
            assert SENTINEL not in record.getMessage()


# --- VaultGetTool._elide static method ----------------------------------


class TestElide:
    def test_long_value_elides_to_last_four(self) -> None:
        # FC-9 docstring: `****<last4>` for >= 8 chars.
        elided = VaultGetTool._elide("supersecret123")
        assert elided == "****t123" or elided.endswith("t123")
        assert "supersecret" not in elided

    def test_short_value_elides_to_just_stars(self) -> None:
        elided = VaultGetTool._elide("short")
        assert elided == "****"
        assert "short" not in elided

    def test_empty_value_does_not_leak(self) -> None:
        # Edge: empty string. Should still emit something redacted-shaped
        # and not raise. Not strictly pinned by spec — flag.
        elided = VaultGetTool._elide("")
        assert "" in elided  # trivially
        # Most importantly: it must not be longer than the elision
        # placeholder.
        assert len(elided) <= 8


# --- Broker error mapping -----------------------------------------------


class TestBrokerErrors:
    async def test_404_raises_tool_error(self, stub_httpx: type[_StubClient]) -> None:
        # FC-9: typed errors. `VaultNotFound` is not in `errors.py`,
        # so we accept any `ToolError` subclass for now and FLAG.
        stub_httpx.register(
            "/secrets/", _StubResponse(status_code=404, payload={"error": "not found"})
        )
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        with pytest.raises(ToolError):
            await tool.call({"item": "DoesNotExist"})

    async def test_401_raises_tool_error(self, stub_httpx: type[_StubClient]) -> None:
        # FC-9: `VaultForbidden` not yet in errors.py — accept ToolError.
        stub_httpx.register(
            "/secrets/", _StubResponse(status_code=401, payload={"error": "unauth"})
        )
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        with pytest.raises(ToolError):
            await tool.call({"item": "Netflix"})

    async def test_403_raises_tool_error(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register(
            "/secrets/", _StubResponse(status_code=403, payload={"error": "forbidden"})
        )
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        with pytest.raises(ToolError):
            await tool.call({"item": "Netflix"})

    async def test_connection_failure_raises_tool_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate "broker connection refused". FLAG: implementer
        # decides whether to surface this as `ToolError` (terminal)
        # or `ToolRetryable` (transient). FC-9 says
        # `VaultBrokerUnreachable`; we accept any `ToolError` subclass.
        class _BrokenClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "_BrokenClient":
                return self

            async def __aexit__(self, *exc: Any) -> None:
                return None

            async def aclose(self) -> None:
                return None

            async def get(self, url: str, **kwargs: Any) -> Any:
                raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "AsyncClient", _BrokenClient)
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        with pytest.raises(ToolError):
            await tool.call({"item": "Netflix"})


# --- Broker response shape + error detail (2026-09-27 Shopify incident) --


class TestBrokerShape:
    async def test_secret_key_shape_is_elided(self, stub_httpx: type[_StubClient]) -> None:
        # The live memory-MCP broker answers `{"secret": ...}`, not
        # `{"value": ...}`. The elided path must recognise it — the old
        # pass-through returned the body (cleartext) unredacted.
        stub_httpx.register("/secrets/", _StubResponse(payload={"secret": SENTINEL}))
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        result = await tool.call({"item": "Netflix"})
        assert result.output["redacted"] is True
        assert SENTINEL not in repr(result.output)
        assert "secret" not in result.output

    async def test_secret_key_shape_show_full(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register("/secrets/", _StubResponse(payload={"secret": "supersecret123"}))
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        result = await tool.call({"item": "Netflix", "show_full": True})
        assert result.output["value"] == "supersecret123"
        assert result.output["redacted"] is False

    async def test_unknown_shape_never_passes_through(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register("/secrets/", _StubResponse(payload={"blob": SENTINEL}))
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        with pytest.raises(ToolError) as excinfo:
            await tool.call({"item": "Netflix"})
        assert SENTINEL not in str(excinfo.value)

    async def test_404_surfaces_broker_detail(self, stub_httpx: type[_StubClient]) -> None:
        # Item exists but the requested field is empty: the broker says so,
        # and the tool must relay it instead of a bare "not found".
        stub_httpx.register(
            "/secrets/",
            _StubResponse(
                status_code=404,
                payload={"error": "No field 'password' on 'Shopify API Key'"},
            ),
        )
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        with pytest.raises(ToolError) as excinfo:
            await tool.call({"item": "Shopify API Key"})
        assert "No field 'password' on 'Shopify API Key'" in str(excinfo.value)

    async def test_500_surfaces_broker_detail(self, stub_httpx: type[_StubClient]) -> None:
        # e.g. bw "More than one result was found" comes back as a 500 with
        # the CLI message in the body; the agent needs it to disambiguate.
        stub_httpx.register(
            "/secrets/",
            _StubResponse(status_code=500, payload={"error": "More than one result was found."}),
        )
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        with pytest.raises(ToolError) as excinfo:
            await tool.call({"item": "API Key"})
        assert "More than one result" in str(excinfo.value)


class TestMessageField:
    async def test_show_full_has_no_message(self, stub_httpx: type[_StubClient]) -> None:
        # The cleartext path carries the value for the model to USE only;
        # nothing render-able (`message`) that a reply formatter would echo.
        stub_httpx.register("/secrets/", _StubResponse(payload={"secret": "supersecret123"}))
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        result = await tool.call(
            {"item": "Shopify API Key", "field": "Client Secret", "show_full": True}
        )
        assert result.output["value"] == "supersecret123"
        assert "message" not in result.output

    async def test_elided_message_never_carries_value(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register("/secrets/", _StubResponse(payload={"secret": SENTINEL}))
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        result = await tool.call({"item": "Shopify API Key", "field": "Client Secret"})
        assert SENTINEL not in result.output["message"]
        assert "redacted" in result.output["message"]


class TestTimeout:
    async def test_timeout_is_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _SlowClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "_SlowClient":
                return self

            async def __aexit__(self, *exc: Any) -> None:
                return None

            async def get(self, url: str, **kwargs: Any) -> Any:
                raise httpx.ReadTimeout("")

        monkeypatch.setattr(httpx, "AsyncClient", _SlowClient)
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        with pytest.raises(ToolError) as excinfo:
            await tool.call({"item": "Netflix"})
        assert "timed out" in str(excinfo.value)


class TestPreflight:
    async def test_preflight_surfaces_broker_detail(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register(
            "/secrets/",
            _StubResponse(
                status_code=404,
                payload={
                    "error": "Item 'X' exists but field 'username' is empty; available fields: Client ID"
                },
            ),
        )
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        with pytest.raises(ToolError, match="available fields: Client ID"):
            await tool.preflight({"item": "X", "field": "username", "show_full": True})

    async def test_preflight_passes_silently(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register("/secrets/", _StubResponse(payload={"secret": SENTINEL}))
        tool = VaultGetTool(broker_url="http://broker.local", token="abc")
        assert await tool.preflight({"item": "X", "show_full": True}) is None
