"""Provider router — picks a `Provider` + model identifier for a turn.

Given a route string like `"anthropic_api:claude-sonnet-4-6"` and a
registry of constructed providers, resolves to `(provider, model_id)`.
Routing rules (active-skill `preferred_model`, task-tier overrides,
conversation default) are evaluated in the agent loop, which calls
`router.resolve(route)` once per turn.

See architecture doc § Routing rules.
"""

from __future__ import annotations

from dataclasses import dataclass

from ember.errors import ConfigError
from ember.providers.base import Provider


@dataclass(frozen=True, slots=True)
class ResolvedRoute:
    provider: Provider
    model: str | None  # None → use the provider's configured default
    route: str  # the canonical "<provider>:<model>" string for logging/persistence


class ProviderRouter:
    """Maps `<provider>:<model>` strings to a `Provider` instance + model."""

    def __init__(
        self,
        providers: dict[str, Provider],
        *,
        cheap_tier_route: str | None = None,
    ) -> None:
        self._providers = dict(providers)
        self._cheap_tier_route = cheap_tier_route

    def has(self, provider_name: str) -> bool:
        return provider_name in self._providers

    def names(self) -> list[str]:
        return sorted(self._providers.keys())

    def model_routes(self) -> list[str]:
        """Curated `<provider>:<model>` routes to surface as selectable
        options (e.g. in `/model` autocomplete + `action:list`).

        Aggregated from each provider's optional `known_models`; providers
        that advertise none contribute nothing. Sorted by provider name so
        the ordering is stable. Routing stays free-form — these are
        suggestions, not an allowlist.
        """
        routes: list[str] = []
        for name in self.names():
            provider = self._providers[name]
            for model in getattr(provider, "known_models", ()):
                routes.append(f"{name}:{model}")
        return routes

    def resolve(self, route: str) -> ResolvedRoute:
        provider_name, model = self.parse_route(route)
        if provider_name not in self._providers:
            raise ConfigError(
                f"unknown provider {provider_name!r} in route {route!r}; available: {self.names()}"
            )
        provider = self._providers[provider_name]
        canonical = f"{provider_name}:{model}" if model else provider_name
        return ResolvedRoute(provider=provider, model=model, route=canonical)

    # --- Cheap-tier routing (v9) -----------------------------------------
    #
    # The cheap tier is the model used for internal bookkeeping tasks —
    # rotation handoff summarization (FC-29), profile-update extraction —
    # where spending subscription/premium tokens isn't justified (TLC-5).
    # The router carries the configured route; callers (rotator,
    # proposer) call `resolve_cheap_tier()` when they need it and skip
    # their work + log if it raises ConfigError.

    def cheap_tier_route(self) -> str | None:
        """Current cheap-tier route string, or None if not configured."""
        return self._cheap_tier_route

    def set_cheap_tier(self, route: str | None) -> None:
        """Update the cheap-tier route.

        `None` disables the cheap tier. A non-None value must parse as a
        valid route AND reference a registered provider, otherwise
        `ConfigError` is raised and the prior value is preserved.
        """
        if route is not None:
            provider_name, _ = self.parse_route(route)  # validates shape
            if provider_name not in self._providers:
                raise ConfigError(
                    f"unknown provider {provider_name!r} for cheap_tier route {route!r}"
                )
        self._cheap_tier_route = route

    def resolve_cheap_tier(self) -> ResolvedRoute:
        """Resolve the configured cheap-tier route.

        Raises `ConfigError` if not configured or if the provider is no
        longer registered.
        """
        if self._cheap_tier_route is None:
            raise ConfigError("cheap_tier route not configured")
        return self.resolve(self._cheap_tier_route)

    @staticmethod
    def parse_route(route: str) -> tuple[str, str | None]:
        if not route or not route.strip():
            raise ConfigError("empty route")
        if ":" not in route:
            return route.strip(), None
        provider_name, _, model = route.partition(":")
        provider_name = provider_name.strip()
        model = model.strip()
        if not provider_name:
            raise ConfigError(f"malformed route {route!r}: missing provider")
        return provider_name, model or None
