"""ModelCommand v9 extensions — `cheap` action, `list` action, extended `show`.

v2's `/model` had `show | set | failover`. v9 adds:

- `/model action:cheap value:<provider>:<model>` — calls
  `router.set_cheap_tier(value)` + `Store.set_runtime_config(...)` so the
  override survives restart. Invalid route → return error string, do NOT
  persist.
- `/model action:cheap value:""` (empty) → `router.set_cheap_tier(None)` +
  persist empty string in runtime_config. Returns "cheap tier disabled".
- `/model action:show` — additionally shows cheap-tier route (or
  "disabled") and the available providers.
- `/model action:list` — prints the available providers from `router.names()`.

Also tests the new `CommandContext.router` field threading.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.commands.base import CommandContext, CommandOption
from ember.commands.model import ModelCommand
from ember.commands.registry import CommandRegistry
from ember.config import Settings
from ember.providers.routing import ProviderRouter
from ember.store.repo import Store
from tests.fakes.fake_provider import FakeProvider


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


def _named_provider(name: str) -> FakeProvider:
    fp = FakeProvider()
    fp.name = name
    return fp


def _make_router(*, cheap_route: str | None = None) -> ProviderRouter:
    primary = _named_provider("claude_cli")
    cheap = _named_provider("anthropic_api")
    providers = {"claude_cli": primary, "anthropic_api": cheap}
    return ProviderRouter(providers, cheap_tier_route=cheap_route)


@pytest.fixture
async def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = _make_settings(monkeypatch, tmp_path)
    store = Store(tmp_path / "conversations.db")
    await store.open()
    registry = CommandRegistry()
    try:
        yield store, settings, registry
    finally:
        await store.close()


def _ctx(
    store: Store,
    settings: Settings,
    registry: CommandRegistry,
    *,
    router: ProviderRouter | None = None,
    conversation_key: str | None = "thread-1",
    args: dict | None = None,
) -> CommandContext:
    return CommandContext(
        conversation_key=conversation_key,
        user_id="1000",
        store=store,
        settings=settings,
        registry=registry,
        args=args or {},
        router=router,
    )


# --- CommandContext carries router ---------------------------------------


class TestCommandContextRouterField:
    def test_router_default_none(self, wired) -> None:
        store, settings, registry = wired
        ctx = CommandContext(
            conversation_key=None,
            user_id="0",
            store=store,
            settings=settings,
            registry=registry,
        )
        # router defaults to None for backwards-compat with prior tests.
        assert ctx.router is None

    def test_router_can_be_set(self, wired) -> None:
        store, settings, registry = wired
        router = _make_router()
        ctx = CommandContext(
            conversation_key=None,
            user_id="0",
            store=store,
            settings=settings,
            registry=registry,
            router=router,
        )
        assert ctx.router is router


# --- choices on `action` includes cheap + list ---------------------------


class TestActionChoicesExtended:
    def test_choices_include_cheap_and_list(self) -> None:
        opt = next(o for o in ModelCommand.options if o.name == "action")
        assert isinstance(opt, CommandOption)
        assert opt.choices is not None
        for action in ("show", "set", "failover", "cheap", "list"):
            assert action in opt.choices, f"missing action {action!r} in choices"


# --- /model action:cheap value:<route> -----------------------------------


class TestActionCheapSet:
    async def test_sets_cheap_tier_on_router(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")
        router = _make_router()
        cmd = ModelCommand()
        await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                router=router,
                args={"action": "cheap", "value": "anthropic_api:haiku-4-5"},
            )
        )
        assert router.cheap_tier_route() == "anthropic_api:haiku-4-5"

    async def test_persists_to_runtime_config(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")
        router = _make_router()
        cmd = ModelCommand()
        await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                router=router,
                args={"action": "cheap", "value": "anthropic_api:haiku-4-5"},
            )
        )
        persisted = await store.get_runtime_config("cheap_tier_route")
        assert persisted == "anthropic_api:haiku-4-5"

    async def test_invalid_route_does_not_persist(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")
        router = _make_router()
        cmd = ModelCommand()
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                router=router,
                args={"action": "cheap", "value": "bogus_provider:x"},
            )
        )
        # Should not raise; should produce a usable error string.
        assert isinstance(out, str)
        assert out != ""
        # Router state untouched.
        assert router.cheap_tier_route() is None
        # Nothing persisted.
        persisted = await store.get_runtime_config("cheap_tier_route")
        assert persisted is None

    async def test_empty_value_disables_cheap_tier(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")
        router = _make_router(cheap_route="anthropic_api:haiku-4-5")
        cmd = ModelCommand()
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                router=router,
                args={"action": "cheap", "value": ""},
            )
        )
        assert router.cheap_tier_route() is None
        # Reply mentions disabled.
        assert isinstance(out, str)
        assert "disabled" in out.lower() or "off" in out.lower()
        # Persisted value is empty string (no delete row needed).
        persisted = await store.get_runtime_config("cheap_tier_route")
        assert persisted == ""

    async def test_cheap_without_router_in_ctx_returns_error(self, wired) -> None:
        """If `CommandContext.router` is None, the command can't validate
        or update the cheap tier — should report a graceful error."""
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")
        cmd = ModelCommand()
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                router=None,
                args={"action": "cheap", "value": "anthropic_api:haiku-4-5"},
            )
        )
        assert isinstance(out, str)
        assert out != ""


# --- /model action:list --------------------------------------------------


class TestActionList:
    async def test_list_shows_provider_names(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")
        router = _make_router()
        cmd = ModelCommand()
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                router=router,
                args={"action": "list", "value": ""},
            )
        )
        # Lists the available providers.
        for name in router.names():
            assert name in out

    async def test_list_without_router_returns_error(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")
        cmd = ModelCommand()
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                router=None,
                args={"action": "list", "value": ""},
            )
        )
        # Graceful — no exception, returns a string.
        assert isinstance(out, str)
        assert out != ""


# --- /model action:show extended -----------------------------------------


class TestExtendedShow:
    async def test_show_includes_cheap_tier_when_set(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")
        router = _make_router(cheap_route="anthropic_api:haiku-4-5")
        cmd = ModelCommand()
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                router=router,
                args={"action": "show", "value": ""},
            )
        )
        # Cheap-tier route appears.
        assert "anthropic_api:haiku-4-5" in out

    async def test_show_says_disabled_when_no_cheap_tier(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")
        router = _make_router(cheap_route=None)
        cmd = ModelCommand()
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                router=router,
                args={"action": "show", "value": ""},
            )
        )
        lowered = out.lower()
        assert "disabled" in lowered or "off" in lowered or "none" in lowered

    async def test_show_includes_available_providers(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")
        router = _make_router()
        cmd = ModelCommand()
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                router=router,
                args={"action": "show", "value": ""},
            )
        )
        for name in router.names():
            assert name in out

    async def test_show_still_works_without_router(self, wired) -> None:
        """Backwards-compat: existing /model show tests didn't pass router.
        With router=None the show should still show route + failover but
        cheap-tier/providers may show 'unknown' or similar."""
        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        cmd = ModelCommand()
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                router=None,
                args={"action": "show", "value": ""},
            )
        )
        assert isinstance(out, str)
        assert conv.provider_route in out
        assert "failover" in out.lower()


# --- Existing v2 surface preserved (regressions) -------------------------


class TestRegressionV2Surface:
    async def test_show_outside_thread(self, wired) -> None:
        store, settings, registry = wired
        cmd = ModelCommand()
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                conversation_key=None,
                args={"action": "show", "value": ""},
            )
        )
        lowered = out.lower()
        assert any(w in lowered for w in ("conversation", "thread", "must"))

    async def test_set_route(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")
        cmd = ModelCommand()
        await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"action": "set", "value": "anthropic_api:claude-sonnet-4-6"},
            )
        )
        conv = await store.get_or_create_conversation("discord", "thread-1")
        assert conv.provider_route == "anthropic_api:claude-sonnet-4-6"

    async def test_failover_on(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")
        cmd = ModelCommand()
        await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"action": "failover", "value": "on"},
            )
        )
        conv = await store.get_or_create_conversation("discord", "thread-1")
        assert conv.auto_failover_on_rate_limit is True


# --- Model options surfaced (opus + fable via claude_cli) ----------------


def _router_with_known_models() -> ProviderRouter:
    """Router whose claude_cli provider advertises selectable model ids."""
    primary = _named_provider("claude_cli")
    primary.known_models = ("sonnet", "opus", "fable")
    cheap = _named_provider("anthropic_api")  # no known_models → no routes
    return ProviderRouter({"claude_cli": primary, "anthropic_api": cheap})


class TestModelOptionsSurfaced:
    async def test_autocomplete_set_suggests_claude_cli_model_routes(self, wired) -> None:
        store, settings, registry = wired
        cmd = ModelCommand()
        ctx = _ctx(
            store,
            settings,
            registry,
            router=_router_with_known_models(),
            args={"action": "set"},
        )
        out = await cmd.autocomplete(ctx, "value", "")
        assert "claude_cli:opus" in out
        assert "claude_cli:fable" in out
        # Bare provider names are still offered alongside the model routes.
        assert "claude_cli" in out

    async def test_autocomplete_cheap_also_suggests_model_routes(self, wired) -> None:
        store, settings, registry = wired
        cmd = ModelCommand()
        ctx = _ctx(
            store,
            settings,
            registry,
            router=_router_with_known_models(),
            args={"action": "cheap"},
        )
        out = await cmd.autocomplete(ctx, "value", "")
        assert "claude_cli:opus" in out
        assert "(disable)" in out  # cheap keeps its disable sentinel

    async def test_autocomplete_filters_by_current_text(self, wired) -> None:
        store, settings, registry = wired
        cmd = ModelCommand()
        ctx = _ctx(
            store,
            settings,
            registry,
            router=_router_with_known_models(),
            args={"action": "set"},
        )
        out = await cmd.autocomplete(ctx, "value", "fable")
        assert out == ["claude_cli:fable"]

    async def test_list_includes_model_options(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")
        cmd = ModelCommand()
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                router=_router_with_known_models(),
                args={"action": "list", "value": ""},
            )
        )
        assert "claude_cli:opus" in out
        assert "claude_cli:fable" in out
