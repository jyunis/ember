"""Slash command interface (FC-12).

The authoritative list of commands lives in a single registry; channel
adapters do not invent commands. v1 surface: `/context`, `/help`.
v2 adds `/model`. v3 adds `/memory revert`. Other commands referenced
in the architecture doc (`/new`, `/topics`, `/skill`) are deferred —
see Deferrals log.

All commands declare a stable `name` + `description`. Commands may
declare typed `options` (Discord slash-command parameters); the channel
adapter wires those into the platform's slash UI and passes the values
through `CommandContext.args`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ember.commands.registry import CommandRegistry
    from ember.config import Settings
    from ember.providers.routing import ProviderRouter
    from ember.store.repo import Store


@dataclass(frozen=True, slots=True)
class CommandOption:
    """Declarative slash-command option.

    The channel adapter translates this into the platform's native
    parameter type (Discord `app_commands` parameters). `default=None`
    AND `required=True` means the option must be provided; otherwise
    `default` (or empty string) is used.
    """

    name: str
    description: str
    required: bool = False
    default: str | None = None
    # Static dropdown values rendered natively by the channel (Discord
    # `Choice`s). Mutually exclusive with `autocomplete` — an option is
    # either a fixed menu or a dynamic type-ahead, never both.
    choices: list[str] | None = None
    # When True, the channel wires a dynamic type-ahead backed by the
    # command's `autocomplete(...)` method (see SupportsAutocomplete).
    autocomplete: bool = False


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Plumbing handed to a slash command's `run` method.

    `conversation_key` is the channel-native identifier of the thread
    the command was invoked in (Discord forum thread ID). `None` if
    the command was invoked outside a conversation thread.

    `args` carries the parsed values for any options the command
    declared. Options not provided fall back to their `default` (or
    are absent from the dict if no default and not required).
    """

    conversation_key: str | None
    user_id: str
    store: "Store"
    settings: "Settings"
    registry: "CommandRegistry"
    args: dict[str, Any] = field(default_factory=dict)
    router: "ProviderRouter | None" = None


@runtime_checkable
class SlashCommand(Protocol):
    """Every slash command implements this surface."""

    name: str
    description: str
    options: list[CommandOption]  # default `[]` for parameter-less commands

    async def run(self, ctx: CommandContext) -> str:
        """Return the user-visible reply. The channel adapter sends it
        as an ephemeral interaction response.

        May raise — the registry catches and renders a user-facing
        error string.
        """
        ...


@runtime_checkable
class SupportsAutocomplete(Protocol):
    """Optional capability: a command that backs one or more of its
    `autocomplete=True` options with dynamic suggestions.

    The channel adapter calls this as the user types. `option` is the
    option name being completed; `current` is the partial text entered so
    far; `ctx.args` carries the other options already filled in (so a
    completer can be context-aware, e.g. `/model value` suggesting routes
    for `action:set` but `on|off` for `action:failover`). Return up to ~25
    short suggestions; the adapter truncates and renders them. Must never
    raise — return `[]` on any trouble.
    """

    async def autocomplete(self, ctx: CommandContext, option: str, current: str) -> list[str]: ...
