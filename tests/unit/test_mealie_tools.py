"""Mealie tools (FC-3, TLC-3, TLC-6, TLC-7) — v5.

Four tools wrap the local Mealie REST API:
- `mealie_search`        — full-text search.
- `mealie_random`        — one (or more) random recipes, optional tag.
- `mealie_get_recipe`    — full recipe by slug.
- `mealie_add_recipe`    — import from URL (writes; NOT confirmation-gated, see below).

The contract surface tested here is per FC-3:
- name, description, input_schema, requires_confirmation
- `Tool` Protocol conformance (runtime_checkable)
- `call(args)` returns `ToolResult` and raises typed errors per FC-3 / TLC-7
- registry validates args against `input_schema` (TLC-6 — schema well-formed)
- Confirmation — 2026-09-08 owner decision: Mealie writes (add/create/
  update) run without a 🔒 prompt (`requires_confirmation=False`). Recipe
  writes are low-stakes and reversible in Mealie's UI.

Tester-flagged ambiguities (also listed in the report):

1. **HTTP client choice** — implementer may use `httpx` or `aiohttp`.
   Same as `test_vault_tools.py`, we monkeypatch `httpx.AsyncClient`.
   FLAG: switch the patch site if the implementer goes with `aiohttp`.

2. **`mealie_search` return shape** — Mealie returns
   `{"items": [...], "page": N, "per_page": N, "total": N}`. Tests
   accept either a pass-through (full envelope) or just `items`. We
   only assert that recipe identifiers round-trip.

3. **`mealie_random` count > 1 semantics** — Mealie's `/api/recipes/random`
   returns one recipe per call. Two valid implementations:
     a) call N times in parallel (or serially) and return a list, OR
     b) pass `?count=N` (Mealie does not actually support that) and
        receive back N items.
   We test the simpler interpretation: `count > 1` → N GETs to the
   random endpoint. FLAG.

4. **`mealie_random` tag filter shape** — Mealie's docs use
   `queryFilter=tags.slug=dinner` (tag query DSL). Implementer may
   simplify by passing `tag=dinner` or `tags=dinner`. We assert the
   tag string appears *somewhere* in the outbound URL (params or
   path) and FLAG the choice.

5. **`mealie_add_recipe` upstream return shape** — `POST /api/recipes/create/url`
   may return either a bare slug string `"chicken-piccata"` or an
   object `{"slug": "chicken-piccata"}`. We assert the slug string
   appears somewhere in the result. FLAG.

6. **`MealieAddRecipeTool` confirmation enforcement** — TLC-3 says the
   *agent loop* intercepts; the tool itself just exposes the flag.
   For v5 the agent loop doesn't actually prompt. We only assert
   the flag is True (static contract).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ember.errors import ToolError, ToolRetryable, ToolValidationError
from ember.tools.base import Tool, ToolResult
from ember.tools.mealie import (
    MealieAddRecipeTool,
    MealieCreateRecipeTool,
    MealieGetRecipeTool,
    MealieRandomTool,
    MealieSearchTool,
    MealieUpdateRecipeTool,
)
from ember.tools.dynamic import make_bridge_wrapper
from ember.tools.registry import Registry


BASE_URL = "http://mealie.local"
API_KEY = "test-api-key-xyz"


# --- Stub httpx ---------------------------------------------------------------


class _StubResponse:
    """Minimal `httpx.Response`-shaped stub."""

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

    Records GET/POST args. `register(method, url_substr, response)`
    queues a response for matching calls; falls through to 404.

    Each new instance shares the registered responses (class-level)
    but its own per-instance call log.
    """

    _registered: list[tuple[str, str, _StubResponse]] = []
    instances: list["_StubClient"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.init_args = args
        self.init_kwargs = kwargs
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        type(self).instances.append(self)

    async def __aenter__(self) -> "_StubClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def aclose(self) -> None:
        return None

    @classmethod
    def reset(cls) -> None:
        cls._registered = []
        cls.instances = []

    @classmethod
    def register(cls, method: str, url_substr: str, response: _StubResponse) -> None:
        cls._registered.append((method.upper(), url_substr, response))

    def _match(self, method: str, url: str) -> _StubResponse:
        for m, substr, resp in type(self)._registered:
            if m == method and substr in url:
                return resp
        return _StubResponse(status_code=404, payload={"error": "not_found"})

    async def get(self, url: str, **kwargs: Any) -> _StubResponse:
        self.calls.append(("GET", url, kwargs))
        return self._match("GET", url)

    async def post(self, url: str, **kwargs: Any) -> _StubResponse:
        self.calls.append(("POST", url, kwargs))
        return self._match("POST", url)

    # Support `client.request(method, url, ...)` style too — `httpx`
    # offers both verb-named methods and a generic `request`. The
    # implementer may pick either.
    async def request(self, method: str, url: str, **kwargs: Any) -> _StubResponse:
        m = method.upper()
        self.calls.append((m, url, kwargs))
        return self._match(m, url)

    @classmethod
    def all_calls(cls) -> list[tuple[str, str, dict[str, Any]]]:
        out: list[tuple[str, str, dict[str, Any]]] = []
        for inst in cls.instances:
            out.extend(inst.calls)
        return out


@pytest.fixture
def stub_httpx(monkeypatch: pytest.MonkeyPatch) -> type[_StubClient]:
    _StubClient.reset()
    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
    return _StubClient


# --- Helpers ------------------------------------------------------------------


def _make_search() -> MealieSearchTool:
    return MealieSearchTool(base_url=BASE_URL, api_key=API_KEY)


def _make_random() -> MealieRandomTool:
    return MealieRandomTool(base_url=BASE_URL, api_key=API_KEY)


def _make_get() -> MealieGetRecipeTool:
    return MealieGetRecipeTool(base_url=BASE_URL, api_key=API_KEY)


def _make_add() -> MealieAddRecipeTool:
    return MealieAddRecipeTool(base_url=BASE_URL, api_key=API_KEY)


def _kwargs_to_param_string(kwargs: dict[str, Any]) -> str:
    """Render a request's kwargs into a single string suitable for
    substring assertions on params/json/headers without committing to
    where the implementer put them."""
    return repr(kwargs)


def _has_bearer_auth(kwargs: dict[str, Any]) -> bool:
    """The Authorization header may live in the call's kwargs OR have
    been set on the AsyncClient itself. We accept either."""
    headers = kwargs.get("headers", {})
    auth = headers.get("Authorization") if isinstance(headers, dict) else None
    if isinstance(auth, str) and "Bearer" in auth and API_KEY in auth:
        return True
    # If headers were set on the client constructor, look there.
    for inst in _StubClient.instances:
        client_headers = inst.init_kwargs.get("headers", {})
        if isinstance(client_headers, dict):
            v = client_headers.get("Authorization")
            if isinstance(v, str) and "Bearer" in v and API_KEY in v:
                return True
    return False


# --- FC-3 static surface ------------------------------------------------------


class TestStaticSurfaceSearch:
    def test_name(self) -> None:
        assert MealieSearchTool.name == "mealie_search"

    def test_description_nonempty(self) -> None:
        assert isinstance(MealieSearchTool.description, str)
        assert MealieSearchTool.description.strip() != ""

    def test_schema_shape(self) -> None:
        s = MealieSearchTool.input_schema
        assert s["type"] == "object"
        assert "query" in s["properties"]
        assert s["properties"]["query"]["type"] == "string"
        assert "limit" in s["properties"]
        assert s["properties"]["limit"]["type"] == "integer"
        assert s["required"] == ["query"]
        assert s["additionalProperties"] is False

    def test_not_destructive(self) -> None:
        assert MealieSearchTool.requires_confirmation is False

    def test_constructs(self) -> None:
        _make_search()  # must not raise

    def test_satisfies_tool_protocol(self) -> None:
        assert isinstance(_make_search(), Tool)


class TestStaticSurfaceRandom:
    def test_name(self) -> None:
        assert MealieRandomTool.name == "mealie_random"

    def test_description_nonempty(self) -> None:
        assert isinstance(MealieRandomTool.description, str)
        assert MealieRandomTool.description.strip() != ""

    def test_schema_shape(self) -> None:
        s = MealieRandomTool.input_schema
        assert s["type"] == "object"
        # `count` and `tag` both optional, no required.
        assert "count" in s["properties"]
        assert s["properties"]["count"]["type"] == "integer"
        assert "tag" in s["properties"]
        assert s["properties"]["tag"]["type"] == "string"
        assert s["additionalProperties"] is False
        # Per the interface, no `required` key (or empty).
        assert s.get("required", []) == []

    def test_not_destructive(self) -> None:
        assert MealieRandomTool.requires_confirmation is False

    def test_constructs(self) -> None:
        _make_random()

    def test_satisfies_tool_protocol(self) -> None:
        assert isinstance(_make_random(), Tool)


class TestStaticSurfaceGetRecipe:
    def test_name(self) -> None:
        assert MealieGetRecipeTool.name == "mealie_get_recipe"

    def test_description_nonempty(self) -> None:
        assert isinstance(MealieGetRecipeTool.description, str)
        assert MealieGetRecipeTool.description.strip() != ""

    def test_schema_shape(self) -> None:
        s = MealieGetRecipeTool.input_schema
        assert s["type"] == "object"
        assert "slug" in s["properties"]
        assert s["properties"]["slug"]["type"] == "string"
        assert s["required"] == ["slug"]
        assert s["additionalProperties"] is False

    def test_not_destructive(self) -> None:
        assert MealieGetRecipeTool.requires_confirmation is False

    def test_constructs(self) -> None:
        _make_get()

    def test_satisfies_tool_protocol(self) -> None:
        assert isinstance(_make_get(), Tool)


class TestStaticSurfaceAddRecipe:
    def test_name(self) -> None:
        assert MealieAddRecipeTool.name == "mealie_add_recipe"

    def test_description_nonempty(self) -> None:
        assert isinstance(MealieAddRecipeTool.description, str)
        assert MealieAddRecipeTool.description.strip() != ""

    def test_schema_shape(self) -> None:
        s = MealieAddRecipeTool.input_schema
        assert s["type"] == "object"
        assert "url" in s["properties"]
        assert s["properties"]["url"]["type"] == "string"
        assert s["required"] == ["url"]
        assert s["additionalProperties"] is False

    def test_not_confirmation_gated(self) -> None:
        # Writes to Mealie, but owner decision 2026-09-08: no prompt.
        assert MealieAddRecipeTool.requires_confirmation is False

    def test_constructs(self) -> None:
        _make_add()

    def test_satisfies_tool_protocol(self) -> None:
        assert isinstance(_make_add(), Tool)


# --- TLC-6 (registry validates against schema) -------------------------------


class TestRegistryValidation:
    """Each tool's schema must be well-formed enough that the registry
    rejects malformed args before invoking `call`. This is the registry's
    contract, not the tool's — but it doubles as a sanity check on the
    schema shape."""

    async def test_search_rejects_missing_query(self) -> None:
        reg = Registry()
        reg.register(_make_search())
        with pytest.raises(ToolValidationError):
            await reg.call_tool("mealie_search", {})

    async def test_search_rejects_extra_property(self) -> None:
        reg = Registry()
        reg.register(_make_search())
        with pytest.raises(ToolValidationError):
            await reg.call_tool("mealie_search", {"query": "x", "bogus": 1})

    async def test_random_rejects_extra_property(self) -> None:
        reg = Registry()
        reg.register(_make_random())
        with pytest.raises(ToolValidationError):
            await reg.call_tool("mealie_random", {"bogus": 1})

    async def test_get_rejects_missing_slug(self) -> None:
        reg = Registry()
        reg.register(_make_get())
        with pytest.raises(ToolValidationError):
            await reg.call_tool("mealie_get_recipe", {})

    async def test_add_rejects_missing_url(self) -> None:
        reg = Registry()
        reg.register(_make_add())
        with pytest.raises(ToolValidationError):
            await reg.call_tool("mealie_add_recipe", {})


# --- MealieSearchTool — behavior --------------------------------------------


class TestMealieSearch:
    async def test_sends_get_to_recipes_with_search_param(
        self, stub_httpx: type[_StubClient]
    ) -> None:
        stub_httpx.register(
            "GET",
            "/api/recipes",
            _StubResponse(
                payload={
                    "items": [{"id": "1", "slug": "chicken-piccata", "name": "Chicken Piccata"}]
                }
            ),
        )
        result = await _make_search().call({"query": "chicken"})
        calls = stub_httpx.all_calls()
        assert any(method == "GET" and "/api/recipes" in url for method, url, _ in calls)
        # Search term arrives somewhere in the request — params or url.
        joined = "".join(url + _kwargs_to_param_string(kw) for _, url, kw in calls)
        assert "chicken" in joined
        assert isinstance(result, ToolResult)

    async def test_includes_bearer_auth(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register("GET", "/api/recipes", _StubResponse(payload={"items": []}))
        await _make_search().call({"query": "anything"})
        calls = stub_httpx.all_calls()
        assert any(_has_bearer_auth(kw) for _, _, kw in calls)

    async def test_returns_items(self, stub_httpx: type[_StubClient]) -> None:
        items = [
            {"id": "1", "slug": "chicken-piccata", "name": "Chicken Piccata"},
            {"id": "2", "slug": "chicken-soup", "name": "Chicken Soup"},
        ]
        stub_httpx.register("GET", "/api/recipes", _StubResponse(payload={"items": items}))
        result = await _make_search().call({"query": "chicken"})
        # FLAG: we accept either pass-through `{"items": [...]}` or the
        # raw list; only that the slugs round-trip.
        blob = repr(result.output)
        assert "chicken-piccata" in blob
        assert "chicken-soup" in blob

    async def test_default_limit_is_10(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register("GET", "/api/recipes", _StubResponse(payload={"items": []}))
        await _make_search().call({"query": "x"})
        calls = stub_httpx.all_calls()
        joined = "".join(url + _kwargs_to_param_string(kw) for _, url, kw in calls)
        # `perPage` (Mealie's param) or `limit` (some impls) — either acceptable.
        assert "10" in joined
        assert ("perPage" in joined) or ("per_page" in joined) or ("limit" in joined)

    async def test_custom_limit_propagates(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register("GET", "/api/recipes", _StubResponse(payload={"items": []}))
        await _make_search().call({"query": "x", "limit": 25})
        calls = stub_httpx.all_calls()
        joined = "".join(url + _kwargs_to_param_string(kw) for _, url, kw in calls)
        assert "25" in joined


# --- MealieRandomTool — behavior --------------------------------------------


class TestMealieRandom:
    # Mealie 2.x removed the dedicated /api/recipes/random endpoint; the
    # tool now uses /api/recipes with orderBy=random + paginationSeed.

    async def test_sends_get_to_recipes_with_random_order(
        self, stub_httpx: type[_StubClient]
    ) -> None:
        stub_httpx.register(
            "GET",
            "/api/recipes",
            _StubResponse(
                payload={
                    "items": [{"id": "1", "slug": "chicken-piccata", "name": "Chicken Piccata"}]
                }
            ),
        )
        result = await _make_random().call({})
        calls = stub_httpx.all_calls()
        joined = "".join(url + _kwargs_to_param_string(kw) for _, url, kw in calls)
        assert any(method == "GET" and "/api/recipes" in url for method, url, _ in calls)
        assert "random" in joined.lower()
        assert "paginationseed" in joined.lower()
        assert isinstance(result, ToolResult)
        assert "chicken-piccata" in repr(result.output)

    async def test_includes_bearer_auth(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register(
            "GET",
            "/api/recipes",
            _StubResponse(payload={"items": [{"id": "1", "slug": "x", "name": "X"}]}),
        )
        await _make_random().call({})
        calls = stub_httpx.all_calls()
        assert any(_has_bearer_auth(kw) for _, _, kw in calls)

    async def test_count_greater_than_one_uses_per_page(
        self, stub_httpx: type[_StubClient]
    ) -> None:
        # Mealie's paginated list takes perPage; one request returns N items.
        stub_httpx.register(
            "GET",
            "/api/recipes",
            _StubResponse(
                payload={
                    "items": [
                        {"id": "1", "slug": "a", "name": "A"},
                        {"id": "2", "slug": "b", "name": "B"},
                        {"id": "3", "slug": "c", "name": "C"},
                    ]
                }
            ),
        )
        result = await _make_random().call({"count": 3})
        calls = [
            (m, u, kw) for m, u, kw in stub_httpx.all_calls() if m == "GET" and "/api/recipes" in u
        ]
        assert len(calls) == 1
        joined = calls[0][1] + _kwargs_to_param_string(calls[0][2])
        assert "3" in joined
        assert result.output["count"] == 3

    async def test_tag_filter_is_present_in_request(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register(
            "GET",
            "/api/recipes",
            _StubResponse(payload={"items": [{"id": "1", "slug": "x", "name": "X"}]}),
        )
        await _make_random().call({"tag": "dinner"})
        calls = stub_httpx.all_calls()
        joined = "".join(url + _kwargs_to_param_string(kw) for _, url, kw in calls)
        assert "dinner" in joined


# --- MealieGetRecipeTool — behavior -----------------------------------------


class TestMealieGetRecipe:
    async def test_sends_get_to_slug_path(self, stub_httpx: type[_StubClient]) -> None:
        recipe = {
            "id": "1",
            "slug": "chicken-piccata",
            "name": "Chicken Piccata",
            "recipeIngredient": [{"note": "1 lb chicken"}],
            "recipeInstructions": [{"text": "cook"}],
        }
        stub_httpx.register("GET", "/api/recipes/chicken-piccata", _StubResponse(payload=recipe))
        result = await _make_get().call({"slug": "chicken-piccata"})
        calls = stub_httpx.all_calls()
        assert any(
            method == "GET" and "/api/recipes/chicken-piccata" in url for method, url, _ in calls
        )
        assert isinstance(result, ToolResult)
        assert "chicken-piccata" in repr(result.output)
        assert "1 lb chicken" in repr(result.output)

    async def test_includes_bearer_auth(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register("GET", "/api/recipes/x", _StubResponse(payload={"slug": "x"}))
        await _make_get().call({"slug": "x"})
        calls = stub_httpx.all_calls()
        assert any(_has_bearer_auth(kw) for _, _, kw in calls)

    async def test_404_raises_tool_error_with_not_found(
        self, stub_httpx: type[_StubClient]
    ) -> None:
        stub_httpx.register(
            "GET",
            "/api/recipes/nope",
            _StubResponse(status_code=404, payload={"error": "not found"}),
        )
        with pytest.raises(ToolError) as exc_info:
            await _make_get().call({"slug": "nope"})
        assert "not" in str(exc_info.value).lower() and "found" in str(exc_info.value).lower()


# --- MealieAddRecipeTool — behavior -----------------------------------------


class TestMealieAddRecipe:
    async def test_sends_post_to_create_url_endpoint(self, stub_httpx: type[_StubClient]) -> None:
        # FLAG: upstream may return a bare slug string OR `{"slug": ...}`.
        stub_httpx.register(
            "POST",
            "/api/recipes/create/url",
            _StubResponse(payload={"slug": "imported-recipe"}),
        )
        result = await _make_add().call({"url": "https://example.com/recipe"})
        calls = stub_httpx.all_calls()
        post_calls = [
            (m, u, kw) for m, u, kw in calls if m == "POST" and "/api/recipes/create/url" in u
        ]
        assert len(post_calls) == 1
        # Body must include the URL — could be `json=` or `data=`.
        body_blob = _kwargs_to_param_string(post_calls[0][2])
        assert "https://example.com/recipe" in body_blob
        assert isinstance(result, ToolResult)
        assert "imported-recipe" in repr(result.output)

    async def test_includes_bearer_auth(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register(
            "POST",
            "/api/recipes/create/url",
            _StubResponse(payload={"slug": "ok"}),
        )
        await _make_add().call({"url": "https://example.com/r"})
        calls = stub_httpx.all_calls()
        assert any(_has_bearer_auth(kw) for _, _, kw in calls)

    async def test_accepts_bare_string_slug_response(self, stub_httpx: type[_StubClient]) -> None:
        # The Mealie API has been observed to return either; we accept both.
        stub_httpx.register(
            "POST",
            "/api/recipes/create/url",
            _StubResponse(payload="bare-slug-style"),
        )
        result = await _make_add().call({"url": "https://example.com/r"})
        assert "bare-slug-style" in repr(result.output)


# --- Error mapping (FC-3 / TLC-7) -------------------------------------------


class TestErrorMapping:
    """Common error-status mapping across tools.

    FC-3 / TLC-7: failures must surface as typed errors. We don't pin
    exact subclasses — `ToolError` is acceptable for terminal failures,
    `ToolRetryable` for transient connection errors.
    """

    @pytest.mark.parametrize("status", [401, 403])
    async def test_search_auth_error_raises_tool_error(
        self, stub_httpx: type[_StubClient], status: int
    ) -> None:
        stub_httpx.register(
            "GET",
            "/api/recipes",
            _StubResponse(status_code=status, payload={"error": "unauth"}),
        )
        with pytest.raises(ToolError) as exc_info:
            await _make_search().call({"query": "x"})
        msg = str(exc_info.value).lower()
        assert any(kw in msg for kw in ("auth", "forbid", "unauth", "401", "403"))

    @pytest.mark.parametrize("status", [401, 403])
    async def test_get_auth_error_raises_tool_error(
        self, stub_httpx: type[_StubClient], status: int
    ) -> None:
        stub_httpx.register(
            "GET",
            "/api/recipes/x",
            _StubResponse(status_code=status, payload={"error": "unauth"}),
        )
        with pytest.raises(ToolError) as exc_info:
            await _make_get().call({"slug": "x"})
        msg = str(exc_info.value).lower()
        assert any(kw in msg for kw in ("auth", "forbid", "unauth", "401", "403"))

    @pytest.mark.parametrize("status", [401, 403])
    async def test_random_auth_error_raises_tool_error(
        self, stub_httpx: type[_StubClient], status: int
    ) -> None:
        stub_httpx.register(
            "GET",
            "/api/recipes",
            _StubResponse(status_code=status, payload={"error": "unauth"}),
        )
        with pytest.raises(ToolError) as exc_info:
            await _make_random().call({})
        msg = str(exc_info.value).lower()
        assert any(kw in msg for kw in ("auth", "forbid", "unauth", "401", "403"))

    @pytest.mark.parametrize("status", [401, 403])
    async def test_add_auth_error_raises_tool_error(
        self, stub_httpx: type[_StubClient], status: int
    ) -> None:
        stub_httpx.register(
            "POST",
            "/api/recipes/create/url",
            _StubResponse(status_code=status, payload={"error": "unauth"}),
        )
        with pytest.raises(ToolError) as exc_info:
            await _make_add().call({"url": "https://example.com/r"})
        msg = str(exc_info.value).lower()
        assert any(kw in msg for kw in ("auth", "forbid", "unauth", "401", "403"))

    @pytest.mark.parametrize("status", [500, 502, 503])
    async def test_search_5xx_raises_tool_error(
        self, stub_httpx: type[_StubClient], status: int
    ) -> None:
        stub_httpx.register(
            "GET",
            "/api/recipes",
            _StubResponse(status_code=status, payload={"error": "server"}),
        )
        with pytest.raises(ToolError):
            await _make_search().call({"query": "x"})

    async def test_get_5xx_raises_tool_error(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register(
            "GET",
            "/api/recipes/x",
            _StubResponse(status_code=503, payload={"error": "server"}),
        )
        with pytest.raises(ToolError):
            await _make_get().call({"slug": "x"})

    async def test_random_5xx_raises_tool_error(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register(
            "GET",
            "/api/recipes",
            _StubResponse(status_code=502, payload={"error": "server"}),
        )
        with pytest.raises(ToolError):
            await _make_random().call({})

    async def test_add_5xx_raises_tool_error(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register(
            "POST",
            "/api/recipes/create/url",
            _StubResponse(status_code=500, payload={"error": "boom"}),
        )
        with pytest.raises(ToolError):
            await _make_add().call({"url": "https://example.com/r"})


# --- Connection failures: ToolRetryable -------------------------------------


class _BrokenClient:
    """Always raises `httpx.ConnectError` on get/post."""

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

    async def post(self, url: str, **kwargs: Any) -> Any:
        raise httpx.ConnectError("connection refused")

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        raise httpx.ConnectError("connection refused")


class TestConnectionFailures:
    """Local Mealie is on the same box; transient connection errors
    are recoverable. Per FC-3: raise `ToolRetryable`."""

    async def test_search_connect_error_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(httpx, "AsyncClient", _BrokenClient)
        with pytest.raises(ToolRetryable):
            await _make_search().call({"query": "x"})

    async def test_get_connect_error_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(httpx, "AsyncClient", _BrokenClient)
        with pytest.raises(ToolRetryable):
            await _make_get().call({"slug": "x"})

    async def test_random_connect_error_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(httpx, "AsyncClient", _BrokenClient)
        with pytest.raises(ToolRetryable):
            await _make_random().call({})

    async def test_add_connect_error_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(httpx, "AsyncClient", _BrokenClient)
        with pytest.raises(ToolRetryable):
            await _make_add().call({"url": "https://example.com/r"})


# --- MealieCreateRecipeTool / MealieUpdateRecipeTool (2026-09-08) -----------
#
# Write tools that don't need a URL. Semantics verified live against Mealie
# v3.24.0 (see module docstring in ember/tools/mealie.py):
#   POST /api/recipes {name} -> "<slug>" (bare JSON string)
#   PATCH /api/recipes/{slug} -> merges scalars, replaces lists, returns recipe


def _make_create() -> MealieCreateRecipeTool:
    return MealieCreateRecipeTool(base_url=BASE_URL, api_key=API_KEY)


def _make_update() -> MealieUpdateRecipeTool:
    return MealieUpdateRecipeTool(base_url=BASE_URL, api_key=API_KEY)


def _patch_calls(stub: type[_StubClient]) -> list[tuple[str, dict[str, Any]]]:
    return [(url, kw) for m, url, kw in stub.all_calls() if m == "PATCH"]


def _no_tags() -> _StubResponse:
    return _StubResponse(payload={"items": [], "total": 0})


def _all_mealie_tools() -> list[Any]:
    return [
        _make_search(),
        _make_random(),
        _make_get(),
        _make_add(),
        _make_create(),
        _make_update(),
    ]


class TestBridgeWrappable:
    """Regression guard (2026-09-08 outage): the MCP bridge synthesizes a
    Python signature from `input_schema.properties`, so a property named
    after a keyword (`yield`) crashed Ember at startup. Every Mealie tool
    must wrap cleanly."""

    def test_every_mealie_tool_wraps_into_a_bridge_signature(self) -> None:
        async def _noop(args: dict[str, Any]) -> Any:
            return None

        for tool in _all_mealie_tools():
            wrapper = make_bridge_wrapper(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
                invoker=_noop,
            )
            assert set(wrapper.__signature__.parameters) == set(tool.input_schema["properties"])


class TestStaticSurfaceWriteTools:
    def test_names(self) -> None:
        assert _make_create().name == "mealie_create_recipe"
        assert _make_update().name == "mealie_update_recipe"

    def test_neither_confirmation_gated(self) -> None:
        # Owner decision 2026-09-08: recipe writes run without a 🔒 prompt.
        assert _make_create().requires_confirmation is False
        assert _make_update().requires_confirmation is False

    def test_satisfy_tool_protocol(self) -> None:
        assert isinstance(_make_create(), Tool)
        assert isinstance(_make_update(), Tool)

    def test_create_requires_name_only(self) -> None:
        schema = _make_create().input_schema
        assert schema["required"] == ["name"]
        assert schema["additionalProperties"] is False
        for f in ("ingredients", "instructions", "tags", "servings", "prep_time"):
            assert f in schema["properties"]

    def test_update_requires_slug_and_accepts_name(self) -> None:
        schema = _make_update().input_schema
        assert schema["required"] == ["slug"]
        assert "name" in schema["properties"]
        assert "ingredients" in schema["properties"]

    def test_update_description_warns_lists_replace(self) -> None:
        # The one footgun the model must know about.
        assert "REPLACE" in _make_update().description

    def test_summaries_are_human_readable(self) -> None:
        s = _make_create().summarize_action(
            {"name": "Pancakes", "ingredients": ["a", "b"], "instructions": ["x"], "tags": ["b"]}
        )
        assert "Pancakes" in s and "2 ingredients" in s and "1 steps" in s and "tags: b" in s
        u = _make_update().summarize_action({"slug": "pancakes", "name": "Crepes", "servings": 2})
        assert "pancakes" in u and "rename to 'Crepes'" in u and "servings=2" in u


class TestWriteToolsSchemaValidation:
    async def test_create_rejects_missing_name(self) -> None:
        reg = Registry()
        reg.register(_make_create())
        with pytest.raises(ToolValidationError):
            await reg.call_tool("mealie_create_recipe", {"ingredients": ["x"]})

    async def test_create_rejects_unknown_field(self) -> None:
        reg = Registry()
        reg.register(_make_create())
        with pytest.raises(ToolValidationError):
            await reg.call_tool("mealie_create_recipe", {"name": "x", "recipeIngredient": []})

    async def test_update_rejects_missing_slug(self) -> None:
        reg = Registry()
        reg.register(_make_update())
        with pytest.raises(ToolValidationError):
            await reg.call_tool("mealie_update_recipe", {"name": "x"})

    async def test_registry_does_not_gate(self, stub_httpx: type[_StubClient]) -> None:
        # No confirmation marker — the call goes straight through to Mealie.
        stub_httpx.register(
            "POST", "/api/recipes", _StubResponse(status_code=201, payload="pancakes")
        )
        reg = Registry()
        reg.register(_make_create())
        res = await reg.call_tool("mealie_create_recipe", {"name": "Pancakes"})
        assert res.is_error is False
        assert res.output["slug"] == "pancakes"


class TestMealieCreateRecipe:
    async def test_name_only_posts_and_skips_patch(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register(
            "POST", "/api/recipes", _StubResponse(status_code=201, payload="pancakes")
        )
        result = await _make_create().call({"name": "Pancakes"})
        assert result.output["slug"] == "pancakes"
        assert result.output["fields_set"] == []
        posts = [(url, kw) for m, url, kw in stub_httpx.all_calls() if m == "POST"]
        assert len(posts) == 1
        assert posts[0][0].endswith("/api/recipes")
        assert posts[0][1]["json"] == {"name": "Pancakes"}
        assert _has_bearer_auth(posts[0][1])
        assert _patch_calls(stub_httpx) == []

    async def test_full_create_translates_fields_into_mealie_shape(
        self, stub_httpx: type[_StubClient]
    ) -> None:
        stub_httpx.register(
            "POST", "/api/recipes", _StubResponse(status_code=201, payload="pancakes")
        )
        stub_httpx.register("GET", "/api/organizers/tags", _no_tags())
        stub_httpx.register(
            "PATCH", "/api/recipes/pancakes", _StubResponse(payload={"slug": "pancakes"})
        )
        result = await _make_create().call(
            {
                "name": "Pancakes",
                "description": "Fluffy.",
                "ingredients": ["2 cups flour", " 1 egg ", ""],
                "instructions": ["Mix.", "Fry."],
                "tags": ["Breakfast"],
                "servings": 4,
                "recipe_yield": "8 pancakes",
                "prep_time": "10 minutes",
                "cook_time": "15 minutes",
                "total_time": "25 minutes",
                "source_url": "https://example.com/p",
            }
        )
        patches = _patch_calls(stub_httpx)
        assert len(patches) == 1
        url, kw = patches[0]
        assert url.endswith("/api/recipes/pancakes")
        body = kw["json"]
        assert body["description"] == "Fluffy."
        # Blank lines dropped, whitespace trimmed, note-only ingredients.
        assert body["recipeIngredient"] == [{"note": "2 cups flour"}, {"note": "1 egg"}]
        assert body["recipeInstructions"] == [{"text": "Mix."}, {"text": "Fry."}]
        # Unknown tag -> name+slug, no id (Mealie auto-creates).
        assert body["tags"] == [{"name": "Breakfast", "slug": "breakfast"}]
        assert body["recipeServings"] == 4
        assert body["recipeYield"] == "8 pancakes"
        assert body["prepTime"] == "10 minutes"
        assert body["cookTime"] == "15 minutes"
        assert body["totalTime"] == "25 minutes"
        assert body["orgURL"] == "https://example.com/p"
        # Friendly names never leak into the Mealie body.
        for bad in ("ingredients", "instructions", "servings", "prep_time", "source_url"):
            assert bad not in body
        assert result.output["slug"] == "pancakes"
        assert "recipeIngredient" in result.output["fields_set"]

    async def test_existing_tag_reused_with_id(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register(
            "POST", "/api/recipes", _StubResponse(status_code=201, payload="pancakes")
        )
        stub_httpx.register(
            "GET",
            "/api/organizers/tags",
            _StubResponse(
                payload={
                    "items": [
                        {"id": "t-1", "name": "Breakfast", "slug": "breakfast"},
                        {"id": "t-2", "name": "Breakfast Burrito", "slug": "breakfast-burrito"},
                    ]
                }
            ),
        )
        stub_httpx.register("PATCH", "/api/recipes/pancakes", _StubResponse(payload={}))
        await _make_create().call({"name": "Pancakes", "tags": ["breakfast", "BREAKFAST", "Vegan"]})
        body = _patch_calls(stub_httpx)[0][1]["json"]
        # Case-insensitive exact match wins (not the prefix match); dupes collapse;
        # unknown tags fall through as name+slug.
        assert body["tags"] == [
            {"id": "t-1", "name": "Breakfast", "slug": "breakfast"},
            {"name": "Vegan", "slug": "vegan"},
        ]

    async def test_accepts_object_shaped_create_response(
        self, stub_httpx: type[_StubClient]
    ) -> None:
        stub_httpx.register(
            "POST", "/api/recipes", _StubResponse(status_code=201, payload={"slug": "pancakes"})
        )
        result = await _make_create().call({"name": "Pancakes"})
        assert result.output["slug"] == "pancakes"

    async def test_blank_name_rejected_before_any_request(
        self, stub_httpx: type[_StubClient]
    ) -> None:
        with pytest.raises(ToolError):
            await _make_create().call({"name": "   "})
        assert stub_httpx.all_calls() == []

    async def test_create_auth_error(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register("POST", "/api/recipes", _StubResponse(status_code=401, payload={}))
        with pytest.raises(ToolError, match="auth"):
            await _make_create().call({"name": "Pancakes"})

    async def test_patch_failure_reports_bare_recipe_slug(
        self, stub_httpx: type[_StubClient]
    ) -> None:
        stub_httpx.register(
            "POST", "/api/recipes", _StubResponse(status_code=201, payload="pancakes")
        )
        stub_httpx.register(
            "PATCH",
            "/api/recipes/pancakes",
            _StubResponse(status_code=422, payload={"detail": "bad"}),
        )
        with pytest.raises(ToolError, match="pancakes.*mealie_update_recipe"):
            await _make_create().call({"name": "Pancakes", "ingredients": ["x"]})

    async def test_connect_error_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Boom:
            def __init__(self, *a: Any, **k: Any) -> None: ...

            async def __aenter__(self) -> "_Boom":
                return self

            async def __aexit__(self, *exc: Any) -> None:
                return None

            async def request(self, *a: Any, **k: Any) -> Any:
                raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx, "AsyncClient", _Boom)
        with pytest.raises(ToolRetryable):
            await _make_create().call({"name": "Pancakes"})


class TestMealieUpdateRecipe:
    async def test_patches_only_given_fields(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register(
            "PATCH",
            "/api/recipes/pancakes",
            _StubResponse(payload={"slug": "pancakes", "name": "Pancakes"}),
        )
        result = await _make_update().call({"slug": "pancakes", "servings": 6})
        patches = _patch_calls(stub_httpx)
        assert len(patches) == 1
        assert patches[0][0].endswith("/api/recipes/pancakes")
        assert patches[0][1]["json"] == {"recipeServings": 6}
        assert _has_bearer_auth(patches[0][1])
        assert result.output == {
            "slug": "pancakes",
            "previous_slug": "pancakes",
            "name": "Pancakes",
            "fields_updated": ["recipeServings"],
        }

    async def test_rename_reports_new_slug(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register(
            "PATCH",
            "/api/recipes/pancakes",
            _StubResponse(payload={"slug": "buttermilk-pancakes", "name": "Buttermilk Pancakes"}),
        )
        result = await _make_update().call({"slug": "pancakes", "name": "Buttermilk Pancakes"})
        assert _patch_calls(stub_httpx)[0][1]["json"] == {"name": "Buttermilk Pancakes"}
        assert result.output["slug"] == "buttermilk-pancakes"
        assert result.output["previous_slug"] == "pancakes"

    async def test_lists_sent_whole(self, stub_httpx: type[_StubClient]) -> None:
        stub_httpx.register("GET", "/api/organizers/tags", _no_tags())
        stub_httpx.register("PATCH", "/api/recipes/pancakes", _StubResponse(payload={}))
        await _make_update().call(
            {"slug": "pancakes", "ingredients": ["a", "b"], "instructions": ["s1"], "tags": ["x"]}
        )
        body = _patch_calls(stub_httpx)[0][1]["json"]
        assert body == {
            "recipeIngredient": [{"note": "a"}, {"note": "b"}],
            "recipeInstructions": [{"text": "s1"}],
            "tags": [{"name": "x", "slug": "x"}],
        }

    async def test_no_fields_is_an_error_without_request(
        self, stub_httpx: type[_StubClient]
    ) -> None:
        with pytest.raises(ToolError, match="at least one field"):
            await _make_update().call({"slug": "pancakes"})
        assert stub_httpx.all_calls() == []

    async def test_unknown_slug_is_not_found(self, stub_httpx: type[_StubClient]) -> None:
        # Stub falls through to 404 for unregistered routes.
        with pytest.raises(ToolError, match="not found"):
            await _make_update().call({"slug": "nope", "servings": 1})
