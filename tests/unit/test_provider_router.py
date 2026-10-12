"""ProviderRouter (`providers/routing.py`) — v2 routing contract.

Maps `<provider>:<model>` strings to a `(Provider, model_id)` pair.
Per architecture doc § Routing rules, the router is the boundary between
the conversation's stored `provider_route` string and the constructed
provider instance the agent loop calls.

These tests assert the public surface: parse_route / resolve / has /
names. The router is fully implemented for v2, so we test it directly
(unlike `anthropic_api.py` whose body is `...`).
"""

from __future__ import annotations

import pytest

from ember.errors import ConfigError
from ember.providers.routing import ProviderRouter, ResolvedRoute
from tests.fakes.fake_provider import FakeProvider


# --- Helpers --------------------------------------------------------------


def _named_provider(name: str) -> FakeProvider:
    fp = FakeProvider()
    fp.name = name  # FakeProvider's `name` is a regular class attr; per-instance override is fine.
    return fp


# --- parse_route ----------------------------------------------------------


class TestParseRoute:
    def test_provider_and_model(self) -> None:
        assert ProviderRouter.parse_route("foo:bar") == ("foo", "bar")

    def test_provider_only_returns_none_model(self) -> None:
        assert ProviderRouter.parse_route("foo") == ("foo", None)

    def test_strips_whitespace_around_provider_and_model(self) -> None:
        assert ProviderRouter.parse_route("  foo  :  bar  ") == ("foo", "bar")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ConfigError):
            ProviderRouter.parse_route("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(ConfigError):
            ProviderRouter.parse_route("   ")

    def test_missing_provider_raises(self) -> None:
        # `:bar` → no provider name. ConfigError per the spec.
        with pytest.raises(ConfigError):
            ProviderRouter.parse_route(":bar")

    def test_missing_provider_whitespace_raises(self) -> None:
        with pytest.raises(ConfigError):
            ProviderRouter.parse_route("  :bar")


# --- resolve --------------------------------------------------------------


class TestResolve:
    def test_resolve_provider_and_model(self) -> None:
        foo = _named_provider("foo")
        router = ProviderRouter({"foo": foo})
        resolved = router.resolve("foo:bar")
        assert isinstance(resolved, ResolvedRoute)
        assert resolved.provider is foo
        assert resolved.model == "bar"
        assert resolved.route == "foo:bar"

    def test_resolve_provider_only(self) -> None:
        foo = _named_provider("foo")
        router = ProviderRouter({"foo": foo})
        resolved = router.resolve("foo")
        assert resolved.provider is foo
        assert resolved.model is None
        assert resolved.route == "foo"

    def test_resolve_unknown_provider_raises_with_helpful_message(self) -> None:
        router = ProviderRouter({"foo": _named_provider("foo"), "baz": _named_provider("baz")})
        with pytest.raises(ConfigError) as ei:
            router.resolve("missing:x")
        # Error mentions which providers ARE available so the user can
        # see what they meant.
        msg = str(ei.value)
        assert "foo" in msg
        assert "baz" in msg

    def test_resolve_strips_whitespace_in_route(self) -> None:
        # parse_route strips; resolve should accept whitespace-padded routes.
        foo = _named_provider("foo")
        router = ProviderRouter({"foo": foo})
        resolved = router.resolve("  foo  :  bar  ")
        assert resolved.provider is foo
        assert resolved.model == "bar"


# --- has / names ----------------------------------------------------------


class TestHasAndNames:
    def test_has_known_provider(self) -> None:
        router = ProviderRouter({"foo": _named_provider("foo")})
        assert router.has("foo") is True

    def test_has_missing_provider(self) -> None:
        router = ProviderRouter({"foo": _named_provider("foo")})
        assert router.has("missing") is False

    def test_names_returns_sorted(self) -> None:
        # Insertion order is intentionally non-alphabetical to prove the
        # sort isn't just "preserved insertion order."
        router = ProviderRouter(
            {
                "zeta": _named_provider("zeta"),
                "alpha": _named_provider("alpha"),
                "mid": _named_provider("mid"),
            }
        )
        assert router.names() == ["alpha", "mid", "zeta"]

    def test_names_empty_router(self) -> None:
        router = ProviderRouter({})
        assert router.names() == []


# --- model_routes ---------------------------------------------------------


class TestModelRoutes:
    def test_aggregates_known_models_as_provider_model_routes(self) -> None:
        foo = _named_provider("foo")
        foo.known_models = ("m1", "m2")
        router = ProviderRouter({"foo": foo})
        assert router.model_routes() == ["foo:m1", "foo:m2"]

    def test_provider_without_known_models_contributes_nothing(self) -> None:
        # FakeProvider has no `known_models` attribute → getattr default ().
        router = ProviderRouter({"bare": _named_provider("bare")})
        assert router.model_routes() == []

    def test_sorted_by_provider_name_models_keep_order(self) -> None:
        zeta = _named_provider("zeta")
        zeta.known_models = ("z1",)
        alpha = _named_provider("alpha")
        alpha.known_models = ("a1", "a2")
        router = ProviderRouter({"zeta": zeta, "alpha": alpha})
        assert router.model_routes() == ["alpha:a1", "alpha:a2", "zeta:z1"]

    def test_empty_router(self) -> None:
        assert ProviderRouter({}).model_routes() == []
