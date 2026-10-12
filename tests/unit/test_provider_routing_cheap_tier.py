"""ProviderRouter — cheap-tier routing surface (v9).

Per architecture doc § Routing rules (task tier, cheap-tier model) and
FC-10 (cheap-tier model is the only model used for compaction; never
`claude_cli`). The router carries a configured cheap-tier route which
v9 callers (auto-compactor, profile-update proposer) resolve when they
need a cheap model.

Tests cover the public surface added in v9:
- constructor `cheap_tier_route` kwarg + default
- `cheap_tier_route()` getter
- `set_cheap_tier(...)` setter (with validation)
- `resolve_cheap_tier()` resolver
- no regression on existing `resolve(...)` behaviour
"""

from __future__ import annotations

import pytest

from ember.errors import ConfigError
from ember.providers.routing import ProviderRouter, ResolvedRoute
from tests.fakes.fake_provider import FakeProvider


# --- Helpers --------------------------------------------------------------


def _named_provider(name: str) -> FakeProvider:
    fp = FakeProvider()
    fp.name = name
    return fp


def _router_with(*names: str, cheap_tier_route: str | None = None) -> ProviderRouter:
    providers = {n: _named_provider(n) for n in names}
    return ProviderRouter(providers, cheap_tier_route=cheap_tier_route)


# --- Constructor / default state -----------------------------------------


class TestConstructor:
    def test_default_no_cheap_tier(self) -> None:
        """Constructor without `cheap_tier_route` defaults to None."""
        router = ProviderRouter({"foo": _named_provider("foo")})
        assert router.cheap_tier_route() is None

    def test_constructor_accepts_cheap_tier_route_kwarg(self) -> None:
        router = _router_with("anthropic_api", cheap_tier_route="anthropic_api:claude-haiku-4-5")
        assert router.cheap_tier_route() == "anthropic_api:claude-haiku-4-5"

    def test_constructor_accepts_explicit_none(self) -> None:
        router = ProviderRouter({"foo": _named_provider("foo")}, cheap_tier_route=None)
        assert router.cheap_tier_route() is None


# --- cheap_tier_route() getter -------------------------------------------


class TestCheapTierRouteGetter:
    def test_returns_configured_route(self) -> None:
        router = _router_with("foo", cheap_tier_route="foo:bar")
        assert router.cheap_tier_route() == "foo:bar"

    def test_returns_none_when_not_configured(self) -> None:
        router = _router_with("foo")
        assert router.cheap_tier_route() is None


# --- set_cheap_tier() ----------------------------------------------------


class TestSetCheapTier:
    def test_set_to_valid_route_updates(self) -> None:
        router = _router_with("anthropic_api")
        router.set_cheap_tier("anthropic_api:claude-haiku-4-5")
        assert router.cheap_tier_route() == "anthropic_api:claude-haiku-4-5"

    def test_set_to_provider_only_route_is_accepted(self) -> None:
        # `parse_route("foo")` returns ("foo", None). A bare provider name
        # is a valid route shape — model defaults to provider's configured
        # default. Should be accepted iff provider is registered.
        router = _router_with("anthropic_api")
        router.set_cheap_tier("anthropic_api")
        assert router.cheap_tier_route() == "anthropic_api"

    def test_set_to_none_clears(self) -> None:
        router = _router_with("foo", cheap_tier_route="foo:bar")
        assert router.cheap_tier_route() == "foo:bar"
        router.set_cheap_tier(None)
        assert router.cheap_tier_route() is None

    def test_set_to_unknown_provider_with_model_raises(self) -> None:
        router = _router_with("anthropic_api")
        with pytest.raises(ConfigError):
            router.set_cheap_tier("unknown:claude-haiku-4-5")
        # State unchanged after failed set.
        assert router.cheap_tier_route() is None

    def test_set_to_unregistered_bare_provider_raises(self) -> None:
        # `set_cheap_tier("not_registered_provider")` — parses to
        # ("not_registered_provider", None); provider is unknown so this
        # must raise.
        router = _router_with("anthropic_api")
        with pytest.raises(ConfigError):
            router.set_cheap_tier("not_registered_provider")
        assert router.cheap_tier_route() is None

    def test_set_to_empty_string_raises(self) -> None:
        # Empty string is not a valid route (parse_route raises ConfigError).
        router = _router_with("foo")
        with pytest.raises(ConfigError):
            router.set_cheap_tier("")

    def test_set_to_whitespace_only_raises(self) -> None:
        router = _router_with("foo")
        with pytest.raises(ConfigError):
            router.set_cheap_tier("   ")

    def test_set_to_missing_provider_route_raises(self) -> None:
        # ":bar" — empty provider half. parse_route raises.
        router = _router_with("foo")
        with pytest.raises(ConfigError):
            router.set_cheap_tier(":bar")

    def test_failed_set_does_not_overwrite_prior_value(self) -> None:
        router = _router_with("foo", cheap_tier_route="foo:initial")
        with pytest.raises(ConfigError):
            router.set_cheap_tier("unknown:x")
        # The prior route is preserved on failure.
        assert router.cheap_tier_route() == "foo:initial"

    def test_set_then_clear_then_set_again(self) -> None:
        router = _router_with("foo")
        router.set_cheap_tier("foo:a")
        assert router.cheap_tier_route() == "foo:a"
        router.set_cheap_tier(None)
        assert router.cheap_tier_route() is None
        router.set_cheap_tier("foo:b")
        assert router.cheap_tier_route() == "foo:b"


# --- resolve_cheap_tier() ------------------------------------------------


class TestResolveCheapTier:
    def test_resolves_when_configured(self) -> None:
        router = _router_with("anthropic_api", cheap_tier_route="anthropic_api:claude-haiku-4-5")
        resolved = router.resolve_cheap_tier()
        assert isinstance(resolved, ResolvedRoute)
        assert resolved.model == "claude-haiku-4-5"
        assert resolved.route == "anthropic_api:claude-haiku-4-5"

    def test_resolves_to_same_as_resolve(self) -> None:
        # `resolve_cheap_tier()` must yield the same `ResolvedRoute` as
        # `resolve(cheap_tier_route())` — it's a convenience for the
        # configured route, not a separate code path.
        route = "anthropic_api:claude-haiku-4-5"
        router = _router_with("anthropic_api", cheap_tier_route=route)
        via_cheap = router.resolve_cheap_tier()
        via_resolve = router.resolve(route)
        assert via_cheap.provider is via_resolve.provider
        assert via_cheap.model == via_resolve.model
        assert via_cheap.route == via_resolve.route

    def test_raises_when_not_configured(self) -> None:
        router = _router_with("foo")
        with pytest.raises(ConfigError):
            router.resolve_cheap_tier()

    def test_raises_after_clearing(self) -> None:
        router = _router_with("foo", cheap_tier_route="foo:bar")
        router.set_cheap_tier(None)
        with pytest.raises(ConfigError):
            router.resolve_cheap_tier()

    def test_resolves_provider_only_route(self) -> None:
        # Bare provider name → resolved with model=None (provider uses default).
        router = _router_with("anthropic_api", cheap_tier_route="anthropic_api")
        resolved = router.resolve_cheap_tier()
        assert resolved.model is None
        assert resolved.route == "anthropic_api"


# --- No-regression: existing resolve() behaviour -------------------------


class TestNoRegression:
    def test_resolve_still_works_when_cheap_tier_configured(self) -> None:
        """Setting `cheap_tier_route` must not affect ordinary `resolve()`."""
        foo = _named_provider("foo")
        bar = _named_provider("bar")
        router = ProviderRouter({"foo": foo, "bar": bar}, cheap_tier_route="bar:something")
        # Resolve an unrelated route — still works as before.
        resolved = router.resolve("foo:model-a")
        assert resolved.provider is foo
        assert resolved.model == "model-a"
        assert resolved.route == "foo:model-a"

    def test_has_and_names_unaffected_by_cheap_tier(self) -> None:
        router = _router_with("alpha", "beta", cheap_tier_route="alpha:m")
        assert router.has("alpha") is True
        assert router.has("missing") is False
        assert router.names() == ["alpha", "beta"]

    def test_haiku_route_resolves_normally(self) -> None:
        # Explicit spec line: existing `resolve("anthropic_api:claude-haiku-4-5")`
        # still works — no regression.
        api = _named_provider("anthropic_api")
        router = ProviderRouter({"anthropic_api": api})
        resolved = router.resolve("anthropic_api:claude-haiku-4-5")
        assert resolved.provider is api
        assert resolved.model == "claude-haiku-4-5"
        assert resolved.route == "anthropic_api:claude-haiku-4-5"
