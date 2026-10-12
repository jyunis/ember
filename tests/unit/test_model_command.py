"""ModelCommand (`/model`) — FC-12 + UoW-15 surface.

Per architecture doc § Routing rules and decision #6 (rate-limit
failover):

- `/model action:show` → display current route + failover state
- `/model action:set value:<provider>:<model>` → set route on this conv
- `/model action:failover value:on|off` → toggle auto-failover

The command body is `...` until the implementer pass; these tests pin
the contract.

Spec ambiguity (flagged in report)
----------------------------------
`action:set value:bogus_provider:x` — should the command validate
provider existence against a `ProviderRouter`? `CommandContext` does
NOT carry a router today. Two interpretations:

(a) Command does no validation; `/model set` is trust-the-user. The
    next turn fails to resolve the route and the agent loop's existing
    fallback (see agent.py: `_router.resolve` ConfigError → first
    available) handles it.

(b) `CommandContext` is extended with a `router` field so `/model set`
    can validate up-front and reject unknown providers immediately.

Default: option (a). The "bogus_provider" test is marked `xfail` so it
documents the desired stronger behavior (option b) without blocking the
implementer who picks (a). Same for `action:failover value:bogus` shape
checks where the right answer is unambiguous (usage message).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.commands.base import CommandContext, CommandOption
from ember.commands.model import ModelCommand
from ember.commands.registry import CommandRegistry
from ember.config import Settings
from ember.store.repo import Store


# --- Fixtures -------------------------------------------------------------


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


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
    )


# --- Static surface (FC-12) ----------------------------------------------


class TestStaticSurface:
    def test_name(self) -> None:
        assert ModelCommand.name == "model"

    def test_description_present(self) -> None:
        assert isinstance(ModelCommand.description, str)
        assert ModelCommand.description.strip() != ""

    def test_options_declares_action_and_value(self) -> None:
        opts = {o.name: o for o in ModelCommand.options}
        assert "action" in opts
        assert "value" in opts

    def test_action_option_has_choices_and_show_default(self) -> None:
        opt = next(o for o in ModelCommand.options if o.name == "action")
        assert isinstance(opt, CommandOption)
        # Architecture doc § Routing rules: at minimum `show | set | failover`
        # are the v2 actions. v9 adds `cheap` and `list`; extending the
        # choices list with additional actions is allowed.
        assert opt.choices is not None
        assert {"show", "set", "failover"}.issubset(set(opt.choices))
        assert opt.default == "show"

    def test_value_option_default_is_empty_string(self) -> None:
        opt = next(o for o in ModelCommand.options if o.name == "value")
        # Architecture: "unused for show" — empty string default lets the
        # command treat absence and "no value" the same.
        assert opt.default == ""


# --- action:show ---------------------------------------------------------


class TestActionShow:
    async def test_show_outside_thread_returns_must_be_invoked_message(self, wired) -> None:
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
        assert isinstance(out, str)
        # Some indication of "wrong place to invoke." Following the
        # /context pattern in the codebase.
        lowered = out.lower()
        assert any(word in lowered for word in ("conversation", "thread", "must"))

    async def test_show_in_thread_mentions_route_and_failover(self, wired) -> None:
        store, settings, registry = wired
        # Conversation row created with default route.
        conv = await store.get_or_create_conversation("discord", "thread-1")

        cmd = ModelCommand()
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                conversation_key="thread-1",
                args={"action": "show", "value": ""},
            )
        )
        # Spec: shows the current `provider_route` and the failover flag.
        assert conv.provider_route in out
        # "failover" naming is part of the user-facing surface — UoW-15
        # is the contract that establishes it.
        assert "failover" in out.lower()


# --- action:set ----------------------------------------------------------


class TestActionSet:
    async def test_set_updates_provider_route_in_store(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")

        cmd = ModelCommand()
        new_route = "anthropic_api:claude-sonnet-4-6"
        await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"action": "set", "value": new_route},
            )
        )

        conv = await store.get_or_create_conversation("discord", "thread-1")
        assert conv.provider_route == new_route

    async def test_show_after_set_reflects_new_route(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")
        cmd = ModelCommand()
        new_route = "anthropic_api:claude-sonnet-4-6"
        await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"action": "set", "value": new_route},
            )
        )
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"action": "show", "value": ""},
            )
        )
        assert new_route in out

    async def test_set_with_empty_value_returns_usage_no_db_write(self, wired) -> None:
        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        original_route = conv.provider_route

        cmd = ModelCommand()
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"action": "set", "value": ""},
            )
        )
        # Some kind of usage message; not necessarily exact wording.
        assert isinstance(out, str)
        assert out != ""

        # Confirm no write happened.
        conv_after = await store.get_or_create_conversation("discord", "thread-1")
        assert conv_after.provider_route == original_route

    @pytest.mark.xfail(
        reason=(
            "Spec ambiguity: `/model set` validation requires access to a "
            "ProviderRouter, which CommandContext does not currently carry. "
            "Default v2 implementation (option a) trusts the user; the agent "
            "loop's resolve-fallback handles unknown providers next turn. "
            "This test pins the stronger desired behavior (option b: extend "
            "CommandContext with a router and validate up-front). Flip to a "
            "passing test once the router lands in CommandContext."
        ),
        strict=False,
    )
    async def test_set_with_bogus_provider_returns_error_no_db_write(self, wired) -> None:
        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        original_route = conv.provider_route

        cmd = ModelCommand()
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"action": "set", "value": "bogus_provider:x"},
            )
        )
        lowered = out.lower()
        # Error message names the unknown provider so the user can see
        # what was rejected.
        assert "bogus_provider" in lowered or "unknown" in lowered

        # No DB mutation.
        conv_after = await store.get_or_create_conversation("discord", "thread-1")
        assert conv_after.provider_route == original_route


# --- action:failover -----------------------------------------------------


class TestActionFailover:
    async def test_failover_on_sets_flag_true(self, wired) -> None:
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

    async def test_failover_off_sets_flag_false(self, wired) -> None:
        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        # First flip on so we can prove off actually clears it.
        await store.update_failover_flag(conv.id, True)

        cmd = ModelCommand()
        await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"action": "failover", "value": "off"},
            )
        )

        conv_after = await store.get_or_create_conversation("discord", "thread-1")
        assert conv_after.auto_failover_on_rate_limit is False

    async def test_show_reflects_failover_flag(self, wired) -> None:
        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        await store.update_failover_flag(conv.id, True)

        cmd = ModelCommand()
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"action": "show", "value": ""},
            )
        )
        # Some indication that failover is on. Exact wording isn't pinned;
        # `True` / `on` / `enabled` are all plausible.
        lowered = out.lower()
        assert any(t in lowered for t in ("on", "true", "enabled"))

    async def test_failover_bogus_value_returns_usage_no_write(self, wired) -> None:
        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        original_flag = conv.auto_failover_on_rate_limit

        cmd = ModelCommand()
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"action": "failover", "value": "bogus"},
            )
        )
        assert isinstance(out, str)
        assert out != ""

        conv_after = await store.get_or_create_conversation("discord", "thread-1")
        assert conv_after.auto_failover_on_rate_limit == original_flag


# --- Unknown / malformed action ------------------------------------------


class TestUnknownAction:
    async def test_unknown_action_returns_usage(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")

        cmd = ModelCommand()
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                args={"action": "wat", "value": "noop"},
            )
        )
        # Should not raise; should return something user-readable.
        assert isinstance(out, str)
        assert out != ""
