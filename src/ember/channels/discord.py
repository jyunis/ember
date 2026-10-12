"""Discord channel adapter.

Implements the `Channel` protocol from `channels.base` against
discord.py. Conversation keys are stringified Discord thread IDs;
v0 expects threads to live under the configured `conversations` forum
channel.

TLC-1 (allowlist) is enforced here before invoking the agent's
on_message handler. TLC-10 (dedup) uses a bounded LRU of seen
Discord message IDs.

v1 adds Discord-side slash command registration: when a `CommandRegistry`
+ `Store` are wired in via the constructor, the adapter registers each
command as a guild-scoped `app_commands.Command` on bot ready and
syncs the tree.
"""

from __future__ import annotations

import inspect
import logging
import re
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import discord
import httpx
from discord import app_commands

from ember.channels.base import (
    Channel,
    ChannelMessage,
    InboundAttachment,
    MessageHandler,
    SendKind,
)
from ember.commands.base import (
    CommandContext,
    CommandOption,
    SlashCommand,
    SupportsAutocomplete,
)
from ember.commands.registry import CommandRegistry
from ember.config import Settings
from ember.providers.routing import ProviderRouter
from ember.store.repo import Store

_log = logging.getLogger("ember.channels.discord")

_DEDUP_CACHE_SIZE = 1024
_DISCORD_MSG_CHAR_LIMIT = 2000

# Inbound upload caps (BC-3). Module-level so they're easy to grep and
# stable across runs; not user-configurable in v1.
MAX_UPLOAD_BYTES: Final[int] = 25 * 1024 * 1024
MAX_UPLOADS_PER_MESSAGE: Final[int] = 8
_UPLOAD_FETCH_TIMEOUT_S: Final[float] = 30.0
_FILENAME_MAX_LEN: Final[int] = 128
# Anything outside this set in a sanitized filename is collapsed to `_`.
_FILENAME_ALLOWED_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]")

# Discord doesn't render GitHub-flavored markdown tables — the `|---|---|`
# alignment row leaks as text and column widths don't align. Wrapping
# tables in a triple-backtick fence preserves the fixed-width columns,
# which is the readable form. We detect tables by the alignment-row
# signature (a row of `:?-+:?` cells) — that is unambiguous in practice
# and keeps the false-positive rate near zero on prose with stray pipes.
_TABLE_ALIGN_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$"
)


def _is_table_row(line: str) -> bool:
    """A line that looks like a table row: starts and ends with `|`
    (after strip) AND has at least two pipe characters total. Strict
    enough to avoid catching prose like 'use a | b construct'."""
    s = line.strip()
    if not s.startswith("|") or not s.endswith("|"):
        return False
    return s.count("|") >= 2


def _wrap_markdown_tables(content: str) -> str:
    """Wrap each markdown table in triple-backtick fences.

    Discord-only post-processing — see `_TABLE_ALIGN_RE` comment for why.
    Fenced code blocks already in the message are passed through
    untouched (no double-wrap). Tables are detected by an alignment row
    immediately following a row that looks like a header.
    """
    if "|" not in content:
        return content
    lines = content.splitlines(keepends=False)
    out: list[str] = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        # Track fenced code blocks so we don't wrap an already-fenced table.
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if (
            not in_fence
            and i + 1 < len(lines)
            and _is_table_row(line)
            and _TABLE_ALIGN_RE.match(lines[i + 1])
        ):
            # Greedy: include header + alignment row + every following
            # row that still looks like a table row.
            end = i + 2
            while end < len(lines) and _is_table_row(lines[end]):
                end += 1
            out.append("```")
            out.extend(lines[i:end])
            out.append("```")
            i = end
            continue
        out.append(line)
        i += 1
    # `splitlines` drops the trailing newline — preserve it if the
    # caller had one, so we don't subtly alter messages without tables.
    suffix = "\n" if content.endswith("\n") else ""
    return "\n".join(out) + suffix


def _sanitize_filename(raw: str) -> str:
    """Sanitize a user-supplied filename for safe on-disk storage (BC-2).

    Strips path separators, NUL bytes, and leading dots; replaces
    anything outside `[A-Za-z0-9._-]` with `_`; truncates to
    `_FILENAME_MAX_LEN`, preserving the extension when present.
    """
    s = raw.replace("\x00", "")
    # Discard any directory component the channel might have leaked.
    s = s.replace("\\", "/").rsplit("/", 1)[-1]
    s = s.lstrip(".") or "file"
    s = _FILENAME_ALLOWED_RE.sub("_", s)
    if not s:
        s = "file"
    if len(s) <= _FILENAME_MAX_LEN:
        return s
    # Preserve the extension on truncation.
    p = Path(s)
    ext = p.suffix
    stem = p.stem
    keep = max(1, _FILENAME_MAX_LEN - len(ext))
    return stem[:keep] + ext


def _resolve_safe_filename(raw: str, used: set[str]) -> str:
    """Return a sanitized name unique within `used`. Collisions get
    `__1`, `__2`, ... suffixes inserted before the extension (BC-2).
    """
    base = _sanitize_filename(raw)
    if base not in used:
        return base
    p = Path(base)
    stem = p.stem
    ext = p.suffix
    n = 1
    while True:
        candidate = f"{stem}__{n}{ext}"
        if candidate not in used:
            return candidate
        n += 1


class DiscordChannel(Channel):
    """discord.py-backed adapter."""

    name: str = "discord"

    def __init__(
        self,
        settings: Settings,
        *,
        command_registry: CommandRegistry | None = None,
        store: Store | None = None,
        router: ProviderRouter | None = None,
    ) -> None:
        self._settings = settings
        self._command_registry = command_registry
        self._store = store
        self._router = router
        self._on_message: MessageHandler | None = None
        self._seen_ids_q: deque[str] = deque()
        self._seen_ids_set: set[str] = set()
        self._slash_synced = False
        # v14 (UoW-26): the reconciliation sweep runs once per process —
        # on_ready can fire again after a gateway reconnect.
        self._archive_sweep_started = False

        intents = discord.Intents.default()
        intents.message_content = True
        intents.guild_messages = True
        # v10 (UoW-20): reaction add/remove on agent messages become
        # explicit feedback signal. `Intents.default()` already includes
        # `reactions`, but we set it explicitly so the dependency is
        # grep-able and survives an intents refactor.
        intents.reactions = True
        self._client = _BotClient(channel=self, intents=intents)
        self._tree = app_commands.CommandTree(self._client)

    async def start(self, on_message: MessageHandler) -> None:
        self._on_message = on_message
        token = self._settings.discord_bot_token.get_secret_value()
        await self._client.start(token)

    async def stop(self) -> None:
        if not self._client.is_closed():
            await self._client.close()

    async def send(
        self,
        conversation_key: str,
        content: str,
        *,
        kind: SendKind = "reply",
    ) -> list[str]:
        thread = await self._resolve_thread(conversation_key)
        if thread is None:
            _log.error("send: thread %s not found", conversation_key)
            return []
        # Channel-side post-processing: wrap markdown tables in code
        # fences for `reply`/`final` only. Status messages stay raw so
        # `🔧 tool_name(...)` lines render normally and so we don't
        # accidentally fence a status that happens to mention pipes.
        rendered = _wrap_markdown_tables(content) if kind in ("reply", "final") else content
        sent_ids: list[str] = []
        for chunk in self._chunk(rendered):
            # suppress_embeds=True kills Discord's OpenGraph preview cards
            # while keeping markdown links clickable. Without this, a
            # news_brief reply with ~20 links renders as a wall of stacked
            # preview cards instead of a clean list.
            sent = await thread.send(chunk, suppress_embeds=True)
            sent_ids.append(str(sent.id))
        return sent_ids

    async def send_attachments(
        self,
        conversation_key: str,
        paths: list[str],
    ) -> None:
        if not paths:
            return
        thread = await self._resolve_thread(conversation_key)
        if thread is None:
            _log.error("send_attachments: thread %s not found", conversation_key)
            return
        # Discord caps a single message at 10 attachments; chunk past that.
        for i in range(0, len(paths), 10):
            batch = paths[i : i + 10]
            files = [discord.File(p) for p in batch]
            try:
                await thread.send(files=files)
            except discord.HTTPException:
                _log.exception("send_attachments failed for %s", batch)

    async def set_typing(self, conversation_key: str, on: bool = True) -> None:
        if not on:
            return
        thread = await self._resolve_thread(conversation_key)
        if thread is None:
            return
        try:
            await thread.typing()
        except Exception:
            _log.debug("set_typing failed", exc_info=True)

    # --- Liveness surface (v16, FC-32) ------------------------------------
    # Best-effort is enforced by the caller (the agent's activity manager
    # wraps every call, UoW-31), so these stay thin and let discord.py
    # exceptions propagate — the manager needs to SEE a failed delete to
    # retry it once. An unresolvable thread degrades quietly: post returns
    # None, edit/delete no-op.

    async def typing_started(self, conversation_key: str) -> None:
        thread = await self._resolve_thread(conversation_key)
        if thread is None:
            return
        # `await thread.typing()` is discord.py's one-shot trigger: the
        # indicator shows for ~10s, so the manager re-triggers every
        # TYPING_REFRESH_S to keep it lit for the whole turn (TLC-27).
        await thread.typing()

    async def post_status(self, conversation_key: str, text: str) -> str | None:
        thread = await self._resolve_thread(conversation_key)
        if thread is None:
            return None
        sent = await thread.send(text[:_DISCORD_MSG_CHAR_LIMIT], suppress_embeds=True)
        return str(sent.id)

    async def edit_status(self, conversation_key: str, native_id: str, text: str) -> None:
        thread = await self._resolve_thread(conversation_key)
        if thread is None:
            return
        await thread.get_partial_message(int(native_id)).edit(
            content=text[:_DISCORD_MSG_CHAR_LIMIT]
        )

    async def delete_status(self, conversation_key: str, native_id: str) -> None:
        thread = await self._resolve_thread(conversation_key)
        if thread is None:
            return
        await thread.get_partial_message(int(native_id)).delete()

    async def notify_scheduler(self, content: str) -> None:
        """Post `content` to the configured scheduler text channel.

        The scheduler channel is a regular Discord text channel (not a
        thread); we resolve it via `discord_scheduler_channel_id` and
        chunk the message to fit Discord's 2000-char limit.
        """
        channel_id = self._settings.discord_scheduler_channel_id
        ch = self._client.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self._client.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                _log.error("scheduler channel %s not found / forbidden", channel_id)
                return
        sender = getattr(ch, "send", None)
        if sender is None:
            _log.error("scheduler channel %s does not support send()", channel_id)
            return
        try:
            for chunk in self._chunk(content):
                await sender(chunk)
        except Exception:
            _log.exception("notify_scheduler send failed")

    # --- Internal: discord.py event handling -----------------------------

    async def _handle_ready(self) -> None:
        user = self._client.user
        _log.info("discord ready: logged in as %s (id=%s)", user, getattr(user, "id", "?"))
        if (
            not self._slash_synced
            and self._command_registry is not None
            and self._store is not None
        ):
            await self._register_and_sync_slash_commands()
            self._slash_synced = True
        # UoW-26 reconciliation sweep — once per process (the flag flips
        # before the sweep runs so a repeat on_ready never re-enters it;
        # a failed sweep waits for the next boot, by design).
        if not self._archive_sweep_started and self._store is not None:
            self._archive_sweep_started = True
            await self._reconcile_archived_threads()

    async def _register_and_sync_slash_commands(self) -> None:
        guild = discord.Object(id=self._settings.discord_guild_id)
        for cmd in self._command_registry.list_commands():  # type: ignore[union-attr]
            self._add_slash_command(cmd, guild)
        try:
            synced = await self._tree.sync(guild=guild)
            _log.info(
                "synced %d slash commands to guild %s",
                len(synced),
                self._settings.discord_guild_id,
            )
        except Exception:
            _log.exception("slash command sync failed")

    def _add_slash_command(self, cmd: SlashCommand, guild: discord.Object) -> None:
        name = cmd.name
        description = cmd.description
        options: list[CommandOption] = list(getattr(cmd, "options", []))

        if not options:

            @self._tree.command(name=name, description=description, guild=guild)
            async def _handler(interaction: discord.Interaction) -> None:
                await self._dispatch_slash(interaction, name, {})

            return

        # Build a typed handler whose signature carries each declared
        # CommandOption as a discord.py app_commands parameter. discord.py
        # introspects __signature__ + __annotations__ at registration; we
        # synthesize both from the options list so options actually reach
        # the Discord UI (and ctx.args).
        handler = self._build_options_handler(cmd, options)
        cmd_obj = app_commands.Command(
            name=name,
            description=description,
            callback=handler,
        )
        self._tree.add_command(cmd_obj, guild=guild)

    def _build_options_handler(self, cmd: SlashCommand, options: list[CommandOption]):
        """Construct an async callback whose signature matches `options`.

        Required options become positional-or-keyword parameters with no
        default. Optional options use `default=""` (we treat empty string
        as "absent" downstream — `CommandContext.args.get(...)` returning
        empty is the established pattern in v1 commands).

        Beyond the signature we attach three pieces of native Discord
        metadata so the slash UI is self-documenting:
          - `describe(...)`  → per-option help text.
          - `choices(...)`   → a fixed dropdown for options with `choices`.
          - `autocomplete(...)` → a dynamic type-ahead for options with
            `autocomplete=True`, backed by the command's own
            `autocomplete()` method.
        choices and autocomplete are mutually exclusive per option (Discord
        rejects both on one option), matching the CommandOption contract.
        """
        command_name = cmd.name
        param_objs: list[inspect.Parameter] = [
            inspect.Parameter(
                "interaction",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=discord.Interaction,
            ),
        ]
        annotations: dict[str, Any] = {"interaction": discord.Interaction}
        descriptions: dict[str, str] = {}
        choice_map: dict[str, list[app_commands.Choice[str]]] = {}
        autocomplete_map: dict[str, Any] = {}

        for opt in options:
            default: Any = inspect.Parameter.empty
            if not opt.required:
                default = "" if opt.default is None else opt.default
            param_objs.append(
                inspect.Parameter(
                    opt.name,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=default,
                    annotation=str,
                )
            )
            annotations[opt.name] = str
            descriptions[opt.name] = opt.description
            if opt.choices:
                choice_map[opt.name] = [app_commands.Choice(name=c, value=c) for c in opt.choices]
            elif opt.autocomplete and isinstance(cmd, SupportsAutocomplete):
                autocomplete_map[opt.name] = self._make_autocomplete_cb(cmd, opt.name, options)

        new_sig = inspect.Signature(param_objs)

        async def _handler(interaction: discord.Interaction, **kwargs: Any) -> None:
            await self._dispatch_slash(interaction, command_name, kwargs)

        _handler.__signature__ = new_sig  # type: ignore[attr-defined]
        _handler.__annotations__ = annotations
        # These decorators attach sentinels that discord.py reads off the
        # callback; applying them to our synthesized handler is safe.
        decorated: Any = app_commands.describe(**descriptions)(_handler)
        if choice_map:
            decorated = app_commands.choices(**choice_map)(decorated)
        if autocomplete_map:
            decorated = app_commands.autocomplete(**autocomplete_map)(decorated)
        return decorated

    def _make_autocomplete_cb(
        self,
        cmd: "SupportsAutocomplete",
        option_name: str,
        options: list[CommandOption],
    ):
        """Build the per-option autocomplete callback Discord invokes as the
        user types. Delegates to the command's `autocomplete()`, passing the
        other options entered so far (via `ctx.args`) so completers can be
        context-aware. Best-effort: any failure yields no suggestions rather
        than a broken interaction."""
        opt_names = [o.name for o in options]

        async def _ac(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            # Pull the partially-entered values of the sibling options so
            # the completer has context (e.g. /model action=set → routes).
            args = {n: (getattr(interaction.namespace, n, "") or "") for n in opt_names}
            ctx = self._ctx_from_interaction(interaction, args)
            try:
                suggestions = await cmd.autocomplete(ctx, option_name, current or "")
            except Exception:
                _log.exception("autocomplete failed for /%s %s", cmd.name, option_name)
                return []
            return [app_commands.Choice(name=s[:100], value=s[:100]) for s in suggestions[:25]]

        return _ac

    def _ctx_from_interaction(
        self, interaction: discord.Interaction, args: dict[str, Any]
    ) -> CommandContext:
        """Build a CommandContext from a slash/autocomplete interaction.
        Shared by command dispatch and autocomplete callbacks."""
        ch = interaction.channel
        if (
            isinstance(ch, discord.Thread)
            and ch.parent_id == self._settings.discord_conversations_channel_id
        ):
            conversation_key: str | None = str(ch.id)
        else:
            conversation_key = None
        return CommandContext(
            conversation_key=conversation_key,
            user_id=str(interaction.user.id),
            store=self._store,  # type: ignore[arg-type]
            settings=self._settings,
            registry=self._command_registry,  # type: ignore[arg-type]
            args=args,
            router=self._router,
        )

    async def _dispatch_slash(
        self,
        interaction: discord.Interaction,
        command_name: str,
        args: dict[str, Any] | None = None,
    ) -> None:
        # TLC-1 also applies to slash commands.
        if interaction.user.id not in self._settings.discord_allowed_user_ids:
            _log.warning(
                "TLC-1 drop on slash command: non-allowlisted user %s",
                interaction.user.id,
            )
            try:
                await interaction.response.send_message("not authorized", ephemeral=True)
            except Exception:
                _log.debug("rejected-slash-cmd response failed", exc_info=True)
            return

        ctx = self._ctx_from_interaction(interaction, args or {})

        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.errors.InteractionResponded:
            pass

        try:
            reply = await self._command_registry.dispatch(command_name, ctx)  # type: ignore[union-attr]
        except Exception:
            _log.exception("slash command dispatch crashed")
            reply = "❌ internal error"

        try:
            await interaction.followup.send(reply, ephemeral=True)
        except Exception:
            _log.exception("slash command followup send failed")

    async def _handle_message(self, message: discord.Message) -> None:
        if self._client.user is not None and message.author.id == self._client.user.id:
            return

        if message.author.id not in self._settings.discord_allowed_user_ids:
            _log.warning(
                "TLC-1 drop: non-allowlisted user %s in channel %s",
                message.author.id,
                message.channel.id,
            )
            return

        if not isinstance(message.channel, discord.Thread):
            return
        if message.channel.parent_id != self._settings.discord_conversations_channel_id:
            return

        if not self._mark_seen(str(message.id)):
            return

        if self._on_message is None:
            _log.warning("inbound message but no handler registered")
            return

        conversation_key = str(message.channel.id)
        attachments, status_lines = await self._intake_attachments(message, conversation_key)
        if status_lines:
            # Surface skipped/failed files as a single status reply. Successful
            # uploads still flow through (BC-1 — partial-failure-tolerant).
            try:
                await self.send(
                    conversation_key,
                    "\n".join(status_lines),
                    kind="status",
                )
            except Exception:
                _log.debug("upload status send failed", exc_info=True)

        cm = ChannelMessage(
            channel="discord",
            conversation_key=conversation_key,
            user_id=str(message.author.id),
            text=message.content or "",
            message_id=str(message.id),
            attachments=attachments,
        )
        await self._on_message(cm)

    async def _handle_reaction(
        self, payload: discord.RawReactionActionEvent, *, added: bool
    ) -> None:
        """UoW-20 / FC-20: turn a reaction add/remove into feedback signal.

        Raw events are used (not `on_reaction_add`) so reactions on
        messages that predate the process cache still register. Never
        sends a message in response — silence is the feature. TLC-1: drop
        non-allowlisted reactors before any store call. A reaction whose
        message id doesn't resolve to an agent-authored `messages` row
        (user message, pre-bot history) is ignored with a debug line.
        """
        if self._store is None:
            return
        # Ignore the bot's own reactions.
        if self._client.user is not None and payload.user_id == self._client.user.id:
            return
        if payload.user_id not in self._settings.discord_allowed_user_ids:
            _log.warning("TLC-1 drop on reaction: non-allowlisted user %s", payload.user_id)
            return

        emoji = str(payload.emoji)
        try:
            message_id = await self._store.message_id_for_channel_message(
                "discord", str(payload.message_id)
            )
            if message_id is None:
                _log.debug(
                    "reaction on unresolvable message %s (not an agent reply); ignored",
                    payload.message_id,
                )
                return
            if added:
                await self._store.record_feedback(message_id, emoji)
            else:
                await self._store.remove_feedback(message_id, emoji)
        except Exception:
            _log.exception("feedback record failed for reaction on %s", payload.message_id)

    async def _handle_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        """UoW-26 event path: a deleted thread under the conversations
        forum ARCHIVES its conversation (stamps `archived_at`) — rows are
        never deleted (TLC-23).

        Raw events are used so threads absent from the process cache still
        register. No allowlist check: the raw delete event carries no
        actor, and only members with manage-threads rights can delete
        threads — the event itself is trusted. Threads under other parents
        (scheduler channel, unrelated forums) are ignored. Never sends any
        message — the thread is gone, and this is a user-initiated action,
        not an agent side effect (TLC-2 does not apply).
        """
        if self._store is None:
            return
        if payload.parent_id != self._settings.discord_conversations_channel_id:
            return
        key = str(payload.thread_id)
        stamped = await self._store.archive_conversation("discord", key, now=datetime.now(UTC))
        if stamped:
            _log.info("archived conversation for deleted thread %s", key)
        else:
            _log.debug(
                "thread delete %s: no unstamped conversation row (pre-Ember "
                "thread, never spoken in, or already archived); no-op",
                key,
            )

    async def _reconcile_archived_threads(self) -> None:
        """UoW-26 sweep path: stamp every live conversation whose thread no
        longer exists under the conversations forum — catches deletions
        that predate this feature or happened while Ember was down.

        Enumerates BOTH active and Discord-archived threads (Discord's own
        auto-archive ≠ deleted; those threads still exist, are revivable,
        and must NOT be stamped). Fail-safe (Decision #6): if enumeration
        raises — including mid-pagination — stamp NOTHING; a partial
        listing must never mass-archive live conversations. A missed stamp
        self-corrects next boot.
        """
        if self._store is None:
            return
        try:
            present = await self._enumerate_forum_thread_keys()
        except Exception:
            _log.exception(
                "archive sweep: thread enumeration failed; stamping nothing (UoW-26 fail-safe)"
            )
            return

        try:
            keys = await self._store.unarchived_conversation_keys("discord")
            now = datetime.now(UTC)
            stamped = 0
            for key in keys:
                if key not in present:
                    if await self._store.archive_conversation("discord", key, now=now):
                        stamped += 1
            _log.info(
                "archive sweep: %d thread(s) enumerated, %d live conversation(s), "
                "%d newly archived",
                len(present),
                len(keys),
                stamped,
            )
        except Exception:
            _log.exception("archive sweep: store reconciliation failed")

    async def _enumerate_forum_thread_keys(self) -> set[str]:
        """Enumeration seam for the UoW-26 sweep: the string conversation
        keys (thread ids) of EVERY thread under the conversations forum —
        both active and Discord-archived (Discord's auto-archive ≠
        deleted; those threads still exist and must not be stamped).
        Raises on failure or partial listing (HTTP error mid-pagination)
        so the caller's fail-safe stamps nothing."""
        forum_id = self._settings.discord_conversations_channel_id
        forum = self._client.get_channel(forum_id)
        if forum is None:
            forum = await self._client.fetch_channel(forum_id)
        # Active threads come from the cache; archived ones from the
        # paginated async iterator (limit=None walks every page).
        keys: set[str] = {str(t.id) for t in forum.threads}
        async for thread in forum.archived_threads(limit=None):
            keys.add(str(thread.id))
        return keys

    async def _intake_attachments(
        self,
        message: discord.Message,
        conversation_key: str,
    ) -> tuple[tuple[InboundAttachment, ...], list[str]]:
        """Save inbound attachments under `data_dir/uploads/...`.

        Returns a tuple of (saved_attachments, status_lines). Status
        lines describe files that were skipped (oversize / count cap) or
        failed to download — the caller surfaces them as a single status
        reply. Successful saves are NEVER reported here; they show up in
        the manifest the agent appends to the user message (BC-9).
        """
        raw = list(getattr(message, "attachments", None) or ())
        if not raw:
            return (), []

        status: list[str] = []
        accepted: list[discord.Attachment] = []
        for att in raw:
            if len(accepted) >= MAX_UPLOADS_PER_MESSAGE:
                status.append(
                    f"⚠️ skipped {att.filename!r}: per-message cap "
                    f"({MAX_UPLOADS_PER_MESSAGE}) reached"
                )
                continue
            if att.size > MAX_UPLOAD_BYTES:
                status.append(
                    f"⚠️ skipped {att.filename!r}: {att.size} bytes exceeds "
                    f"{MAX_UPLOAD_BYTES} byte cap"
                )
                continue
            accepted.append(att)

        if not accepted:
            return (), status

        target_dir = self._settings.data_dir / "uploads" / conversation_key / str(message.id)
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            status.append(f"⚠️ upload directory create failed: {e}")
            return (), status

        used_names: set[str] = set()
        saved: list[InboundAttachment] = []
        async with httpx.AsyncClient(timeout=_UPLOAD_FETCH_TIMEOUT_S) as client:
            for att in accepted:
                safe = _resolve_safe_filename(att.filename, used_names)
                used_names.add(safe)
                dest = target_dir / safe
                try:
                    resp = await client.get(att.url)
                    resp.raise_for_status()
                    dest.write_bytes(resp.content)
                except (httpx.HTTPError, OSError) as e:
                    status.append(f"⚠️ download of {att.filename!r} failed: {type(e).__name__}: {e}")
                    try:
                        if dest.exists():
                            dest.unlink()
                    except OSError:
                        pass
                    continue
                saved.append(
                    InboundAttachment(
                        filename=att.filename or safe,
                        path=str(dest),
                        content_type=att.content_type or "",
                        size=dest.stat().st_size,
                    )
                )

        return tuple(saved), status

    # --- Internal helpers -------------------------------------------------

    def _mark_seen(self, msg_id: str) -> bool:
        """Return True if newly seen, False if already seen (dedup)."""
        if msg_id in self._seen_ids_set:
            return False
        self._seen_ids_set.add(msg_id)
        self._seen_ids_q.append(msg_id)
        while len(self._seen_ids_q) > _DEDUP_CACHE_SIZE:
            evicted = self._seen_ids_q.popleft()
            self._seen_ids_set.discard(evicted)
        return True

    async def _resolve_thread(self, conversation_key: str) -> discord.Thread | None:
        try:
            tid = int(conversation_key)
        except ValueError:
            return None
        ch = self._client.get_channel(tid)
        if ch is None:
            try:
                ch = await self._client.fetch_channel(tid)
            except (discord.NotFound, discord.Forbidden):
                return None
        return ch if isinstance(ch, discord.Thread) else None

    @staticmethod
    def _chunk(content: str) -> list[str]:
        """Split to fit Discord's limit at a newline, else a sentence
        end, else a space, else a hard cut (only for a single
        2000+-char token, e.g. a long URL) — a naive fixed-width slice
        breaks mid-word across message bubbles, which reads as garbage
        once block streaming posts several messages per turn. Newline
        wins whenever one exists in the window; the sentence/space
        fallbacks only ever fire inside a single >limit paragraph. The
        separator at the cut is dropped (sentence cuts keep their
        punctuation); the message boundary itself renders as the
        break.

        Fence-aware: when a cut lands inside an open ``` code fence, the
        chunk is closed with ``` and the next chunk re-opens the same
        fence (language tag included), so a long fenced body — a 🔒
        confirm prompt carrying a full email or diff — renders as code
        in every bubble instead of leaking into markdown after the first
        one. Only the fence markers are added; no content is dropped."""
        if len(content) <= _DISCORD_MSG_CHAR_LIMIT:
            return [content] if content else [""]
        chunks: list[str] = []
        remaining = content
        # Fence opener line (e.g. "```diff") still open at the start of
        # `remaining`, or None. It is re-emitted as the next chunk's
        # first line.
        open_fence: str | None = None
        while True:
            prefix = f"{open_fence}\n" if open_fence else ""
            if len(prefix) + len(remaining) <= _DISCORD_MSG_CHAR_LIMIT:
                chunks.append(prefix + remaining)
                break
            budget = _DISCORD_MSG_CHAR_LIMIT - len(prefix)
            piece, rest = DiscordChannel._cut(remaining, budget)
            fence_after = _fence_state_after(piece, open_fence)
            if fence_after is not None and len(prefix) + len(piece) + len(_FENCE_CLOSE) > (
                _DISCORD_MSG_CHAR_LIMIT
            ):
                # No room for the closing fence: cut again with space
                # reserved for it. The shorter piece may end at a
                # different fence state, so re-derive it.
                piece, rest = DiscordChannel._cut(remaining, budget - len(_FENCE_CLOSE))
                fence_after = _fence_state_after(piece, open_fence)
            if fence_after is not None:
                piece += _FENCE_CLOSE
            chunks.append(prefix + piece)
            remaining = rest
            open_fence = fence_after
        return chunks

    @staticmethod
    def _cut(remaining: str, budget: int) -> tuple[str, str]:
        """One split of `remaining` into (piece, rest) with
        `len(piece) <= budget`, using the newline → sentence → space →
        hard-cut preference. The separator at the cut is dropped."""
        window = remaining[:budget]
        cut = window.rfind("\n")
        if cut <= 0:
            # No newline: prefer ending the chunk at a sentence
            # boundary (keep the punctuation, drop the space).
            sentence_cut = max(window.rfind(end) for end in (". ", "! ", "? "))
            if sentence_cut > 0:
                cut = sentence_cut + 1
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            return window, remaining[budget:]
        return remaining[:cut], remaining[cut + 1 :]


# Appended to a chunk that ends inside an open code fence (see `_chunk`).
_FENCE_CLOSE = "\n```"


def _fence_state_after(piece: str, open_fence: str | None) -> str | None:
    """Walk `piece` line by line, toggling code-fence state from the
    given starting state. Returns the opener line (e.g. "```diff") if a
    fence is still open at the end, else None. A line whose leading
    whitespace is followed by ``` toggles: it opens when closed (and its
    text becomes the opener to re-emit) and closes when open."""
    state = open_fence
    for line in piece.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("```"):
            continue
        state = None if state is not None else stripped
    return state


class _BotClient(discord.Client):
    """Internal discord.py Client that delegates events to DiscordChannel."""

    def __init__(self, *, channel: DiscordChannel, **kwargs) -> None:
        super().__init__(**kwargs)
        self._channel = channel

    async def on_ready(self) -> None:
        await self._channel._handle_ready()

    async def on_message(self, message: discord.Message) -> None:
        try:
            await self._channel._handle_message(message)
        except Exception:
            _log.exception("error handling inbound discord message")

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        try:
            await self._channel._handle_reaction(payload, added=True)
        except Exception:
            _log.exception("error handling reaction add")

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        try:
            await self._channel._handle_reaction(payload, added=False)
        except Exception:
            _log.exception("error handling reaction remove")

    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        try:
            await self._channel._handle_thread_delete(payload)
        except Exception:
            _log.exception("error handling thread delete")
