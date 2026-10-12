"""`/model` — show or set the provider/model route, toggle failover, and
manage the cheap-tier route.

Forms:
- `/model action:show` (default) → display current route + failover state +
  cheap-tier route + available providers.
- `/model action:set value:<provider>:<model>` → set route on this conversation.
- `/model action:failover value:on|off` → toggle auto-failover (UoW-15).
- `/model action:cheap value:<provider>:<model>` → set the cheap-tier route
  (used by auto-compaction + profile-update extraction). Persisted via
  `runtime_config` so it survives restart. Empty value disables the
  cheap tier.
- `/model action:list` → list available providers plus the curated
  `<provider>:<model>` options (e.g. `claude_cli:opus`,
  `claude_cli:fable`) from the router.

Model options: `set`/`cheap` autocomplete and `list` surface each
provider's curated `known_models` as full `<provider>:<model>` routes
(via `ProviderRouter.model_routes()`). These are suggestions only —
routing stays free-form, so any model string the backend accepts still
works.

Validation (v9): `cheap` and `list` need access to the `ProviderRouter`,
threaded through `CommandContext.router`. Setting a `cheap` route validates
against the router's registered providers; an invalid route returns an
error string and persists nothing.
"""

from __future__ import annotations

from ember.commands.base import CommandContext, CommandOption
from ember.errors import ConfigError

_RUNTIME_CONFIG_CHEAP_KEY = "cheap_tier_route"


class ModelCommand:
    name: str = "model"
    description: str = "Show or set the provider/model + failover + cheap-tier."
    options: list[CommandOption] = [
        CommandOption(
            name="action",
            description="show | set | failover | cheap | list",
            default="show",
            choices=["show", "set", "failover", "cheap", "list"],
        ),
        CommandOption(
            name="value",
            description="route (set/cheap) · on/off (failover) · unused for show/list",
            default="",
            autocomplete=True,
        ),
    ]

    async def autocomplete(self, ctx: CommandContext, option: str, current: str) -> list[str]:
        """Suggest values for `value`, keyed off the chosen `action`:
        on/off for failover; registered provider routes for set/cheap."""
        if option != "value":
            return []
        action = (ctx.args.get("action") or "show").strip()
        cur = current.strip().lower()
        if action == "failover":
            return [v for v in ("on", "off") if v.startswith(cur)]
        if action in ("set", "cheap") and ctx.router is not None:
            # Bare provider names (default model) + curated provider:model
            # options (e.g. `claude_cli:opus`). Routing is free-form, so
            # these are suggestions, not the only valid routes.
            routes = [*ctx.router.names(), *ctx.router.model_routes()]
            if action == "cheap":
                routes = ["(disable)", *routes]
            return [r for r in routes if cur in r.lower()][:25]
        return []

    async def run(self, ctx: CommandContext) -> str:
        action = (ctx.args.get("action") or "show").strip()
        value = (ctx.args.get("value") or "").strip()

        # `list` doesn't require a conversation thread.
        if action == "list":
            return self._render_list(ctx)

        if ctx.conversation_key is None:
            return "ℹ️ `/model` must be invoked in a conversation thread."

        conv = await ctx.store.get_or_create_conversation("discord", ctx.conversation_key)

        if action == "show":
            return self._render_show(ctx, conv)

        if action == "set":
            if not value:
                return "Usage: `/model action:set value:<provider>:<model>`"
            # Validate the route against the live router so typos don't
            # silently persist. Without this guard a bad provider name
            # gets written to the conversation row, then the agent loop
            # silently falls back to the first registered provider on
            # every turn — and tool-use breaks if the fallback is
            # text-only (e.g. `anthropic_api`).
            if ctx.router is not None:
                try:
                    provider_name, _ = ctx.router.parse_route(value)
                except ConfigError as e:
                    return f"❌ invalid route `{value}`: {e}"
                if not ctx.router.has(provider_name):
                    available = ", ".join(f"`{n}`" for n in ctx.router.names()) or "(none)"
                    return (
                        f"❌ unknown provider `{provider_name}` in route `{value}`. "
                        f"Available: {available}"
                    )
            await ctx.store.update_provider_route(conv.id, value)
            return f"✅ route set to `{value}`"

        if action == "failover":
            if value not in ("on", "off"):
                return "Usage: `/model action:failover value:on` or `value:off`"
            await ctx.store.update_failover_flag(conv.id, value == "on")
            return f"✅ auto-failover {value}"

        if action == "cheap":
            return await self._set_cheap(ctx, value)

        return "Unknown action. Use `show`, `set`, `failover`, `cheap`, or `list`."

    # --- show -------------------------------------------------------------

    def _render_show(self, ctx: CommandContext, conv) -> str:
        failover_str = "on" if conv.auto_failover_on_rate_limit else "off"
        lines = [
            "**Model settings**",
            f"Route: `{conv.provider_route}`",
            f"Auto-failover on rate limit: {failover_str}",
        ]
        if ctx.router is not None:
            cheap = ctx.router.cheap_tier_route()
            if cheap:
                lines.append(f"Cheap-tier route: `{cheap}`")
            else:
                lines.append("Cheap-tier route: disabled")
            names = ctx.router.names()
            if names:
                lines.append("Available providers: " + ", ".join(f"`{n}`" for n in names))
        else:
            lines.append("Cheap-tier route: unknown (router not wired)")
        return "\n".join(lines)

    # --- list -------------------------------------------------------------

    def _render_list(self, ctx: CommandContext) -> str:
        if ctx.router is None:
            return "ℹ️ provider router not available in this context."
        names = ctx.router.names()
        if not names:
            return "ℹ️ no providers registered."
        lines = ["**Available providers**"]
        for n in names:
            lines.append(f"- `{n}`")
        model_routes = ctx.router.model_routes()
        if model_routes:
            lines.append("**Model options**")
            for route in model_routes:
                lines.append(f"- `{route}`")
        lines.append(
            "Routes: append `:<model>` for a specific model "
            "(e.g. `anthropic_api:claude-haiku-4-5`)."
        )
        return "\n".join(lines)

    # --- cheap ------------------------------------------------------------

    async def _set_cheap(self, ctx: CommandContext, value: str) -> str:
        if ctx.router is None:
            return "ℹ️ provider router not available; cannot update cheap tier."

        if not value:
            ctx.router.set_cheap_tier(None)
            await ctx.store.set_runtime_config(_RUNTIME_CONFIG_CHEAP_KEY, "")
            return "✅ cheap tier disabled"

        try:
            ctx.router.set_cheap_tier(value)
        except ConfigError as e:
            return f"❌ invalid cheap-tier route `{value}`: {e}"

        await ctx.store.set_runtime_config(_RUNTIME_CONFIG_CHEAP_KEY, value)
        return f"✅ cheap tier set to `{value}`"
