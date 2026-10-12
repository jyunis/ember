"""Agent loop.

Implements UoW-1 (inbound message handling) for v0:

  inbound ChannelMessage
    → resolve conversation (UoW-14)
    → persist user message
    → assemble prompt from recent history
    → provider.complete(...)
    → persist assistant message + usage (TLC-5)
    → channel.send(..., kind="final")

v0 has no tools, no compaction, no skills. TLC-1 (allowlist) is the
channel adapter's responsibility (FC-1) — not enforced here. TLC-10
(dedup) is also adapter-side per FC-1.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ember.bridge.active import clear_active as _clear_active_bridge
from ember.bridge.active import set_active as _set_active_bridge
from ember.channels import base as _channels_base
from ember.channels.base import Channel, ChannelMessage
from ember.context.interactive import in_interactive_turn as _in_interactive_turn
from ember.context.interruption import mark_user_message_persisted
from ember.totp import TotpError, TotpVerifier
from ember.commands.memory import MemoryRevertTracker
from ember.config import Settings
from ember.context.rotation import SessionRotator
from ember.errors import (
    AuthError,
    ConfigError,
    ProviderError,
    RateLimitError,
    SessionNotFoundError,
    ToolRetryable,
    TransportError,
)
from ember.identity.proposer import Proposer
from ember.identity.store import IdentityStore
from ember.identity.writer import ProfileWriter
from ember.providers.base import TEXT_PARTS_SEPARATOR, Provider, StreamHandler, ToolSpec
from ember.providers.routing import ProviderRouter, ResolvedRoute
from ember.skills.base import Skill, SkillRegistry
from ember.skills.router_prompt import build_skills_block, build_tools_catalog_block
from ember.store.repo import Conversation, Store
from ember.tools.base import Tool, ToolResult
from ember.tools.registry import CONFIRMATION_REQUIRED_ERROR, Registry
from ember.unlock import UnlockStore

# Rate-limit failover target (UoW-15). Must be a tool-capable provider:
# Ember is tool-heavy, and failing over to a text-only provider silently
# strips every tool and makes the model narrate fake tool calls. deepseek
# forwards OpenAI-style tools through /chat/completions; anthropic_api
# (the old target) is text-only, which is exactly how a conversation
# rotted into fake-tool mode.
_FAILOVER_ROUTE = "deepseek_api"

# runtime_config flag marking an active "claude auth is down" alert, so a
# sustained outage (every turn raising AuthError) pings the scheduler
# channel once, not every turn. Cleared on the next healthy claude_cli turn.
_CLAUDE_AUTH_ALERT_KEY = "claude_auth_alert_active"

# The re-auth one-liner, kept as a single line so it pastes-and-runs as-is
# and is cleanly copyable from Discord's iOS app (sent as its own bare
# message — a multi-line fenced block isn't selectable there).
_CLAUDE_REAUTH_COMMAND = "cd /srv/ember && docker compose exec agent claude setup-token"

_TOOL_STATUS_ARGS_LIMIT = 200

# FC-32 timing constants (v16). Canonical values live in channels/base.py
# alongside the liveness surface they pace; they are re-bound here because
# the activity manager reads them from THIS module's globals at call time —
# tests compress the timeline by patching `ember.agent.ACTIVITY_DELAY_S`
# and friends.
ACTIVITY_DELAY_S = _channels_base.ACTIVITY_DELAY_S
ACTIVITY_EDIT_INTERVAL_S = _channels_base.ACTIVITY_EDIT_INTERVAL_S
TYPING_REFRESH_S = _channels_base.TYPING_REFRESH_S

# Floor between event-driven edits of the activity message (UoW-31 step 3:
# an observer event edits promptly; this floor just keeps a burst of
# back-to-back tool calls from hammering Discord's per-message edit rate
# limit). The regular cadence is ACTIVITY_EDIT_INTERVAL_S (FC-32).
_ACTIVITY_MIN_EDIT_GAP_S = 1.0

# Tools that bypass FC-18's "router mode advertises only use_skill"
# rule. These are general-purpose utilities where wrapping them in a
# skill would be ceremony for no benefit — `generate_image` because
# media generation is a natural one-shot ask in any thread, and
# `http_request` because the model often needs ad-hoc HTTP for
# debugging or simple API calls. Tools listed here are advertised in
# router mode IFF they are present in the global registry.
_ROUTER_DIRECT_TOOL_NAMES: tuple[str, ...] = (
    "web_search",
    "web_fetch",
    "web_browse",
    "http_request",
    "generate_image",
)

_log = logging.getLogger("ember.agent")


def _format_size(n: int) -> str:
    """Human-readable size used in attachment manifests (BC-4).

    Uses 1024-based units. Sub-KB shows raw bytes ("813 B"). 1 KB to
    <10 KB shows no fractional digit (per BC-4 "no fractional digits
    below 10 KB"). 10 KB+ also shows no fractional digit (a single-digit
    KB precision is plenty for a manifest line). MB applies once size
    is at least 1 MB and uses one decimal place.
    """
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        kb = n / 1024
        if kb < 10:
            return f"{int(kb)} KB"
        return f"{int(kb)} KB"
    mb = n / (1024 * 1024)
    return f"{mb:.1f} MB"


def _user_text_with_manifest(msg: ChannelMessage) -> str:
    """Build the persisted user text with an attachment manifest appended.

    Returns `msg.text` unchanged when there are no attachments (BC-5 —
    byte-stable with prior turn behavior). With attachments, appends a
    blank line then a stable manifest block (BC-4):

        [Attachments:
        - <abs_path> (<mime>, <size>)
        ...
        How to use these: <tool guidance>
        ]

    The tool guidance is embedded so the model anchors on real tool
    names (`Read`, `generate_image`) instead of hallucinating ones from
    its training-data prior.

    When the user sent zero text, the manifest stands alone (no leading
    blank line).
    """
    if not msg.attachments:
        return msg.text
    lines = ["[Attachments:"]
    for a in msg.attachments:
        mime = a.content_type or "application/octet-stream"
        lines.append(f"- {a.path} ({mime}, {_format_size(a.size)})")

    has_image = any((a.content_type or "").lower().startswith("image/") for a in msg.attachments)
    guidance: list[str] = []
    if has_image:
        guidance.append(
            "Image attachments: use `Read` (built-in) with the path above to "
            "view image content — Read renders images natively. To generate "
            "or edit an image, call `generate_image` and pass the path in "
            "`reference_images` to base output on the user's image."
        )
    else:
        guidance.append(
            "File attachments: use `Read` (built-in) with the path above to view the contents."
        )
    # doc #59 (FC-36): PDFs and images are the "file this" candidates.
    # Appended regardless of whether the docs tools are registered (this
    # builder has no registry access; an unregistered call fails loudly).
    has_filable = any(
        (a.content_type or "").lower() == "application/pdf"
        or (a.content_type or "").lower().startswith("image/")
        for a in msg.attachments
    )
    if has_filable:
        guidance.append(
            "To file it permanently into the user's documents, call `docs_file` with the "
            "path (only when asked to file/save/keep it)."
        )
    lines.append("")
    lines.extend(guidance)
    lines.append("]")
    manifest = "\n".join(lines)
    if not msg.text:
        return manifest
    return f"{msg.text}\n\n{manifest}"


# --- Temporal context (TC-1) -----------------------------------------------
# Interactive user turns are prefixed with a bracketed local timestamp so the
# model can reason about time of day and silence between messages (paired with
# `_TRANSCRIPT_TIME_NOTE` in the system block). The stamp is baked into the
# persisted text ONCE, at ingestion, so it becomes immutable history: past
# turns keep byte-identical stamps and the prompt-cache prefix is unaffected —
# only the fresh tail turn adds new bytes. Absolute, never relative: a frozen
# "3 days ago" would read wrong two turns later.
_GAP_NOTE_AFTER = timedelta(hours=6)

_TRANSCRIPT_TIME_NOTE = """## Conversation timing

Each user message begins with a bracketed local timestamp, e.g. `[Fri 2026-07-11 18:52]`. After a long silence the bracket also states the gap, e.g. `[Fri 2026-07-11 18:52 · 5 days after previous message]`.

Use these to stay oriented in time: answer with the current time of day in mind, and treat a stated gap as a likely new context, not a continuation — don't assume the last thing discussed is still the topic. Messages without a gap note are part of the same ongoing exchange. The timestamps are context for you only; don't echo the bracketed text back or narrate the timing unless asked."""

# History-recall nudge (v15, FC-31). One static line: with chat-style
# usage plus session rotation, most of the durable record lives in the
# archive rather than the live context — point the model at the history
# tools instead of letting it guess or claim amnesia. Static text, so
# the system block stays byte-stable after a one-time cache invalidation
# (TLC-12, `_TRANSCRIPT_TIME_NOTE` precedent).
_HISTORY_RECALL_NOTE = "When the user references something from a past or earlier conversation that isn't in your current context, use the `history_search` and `history_fetch` tools to look it up in the conversation archive before answering — don't guess and don't say you can't recall. The archive also holds older turns of THIS conversation (they get condensed over time): treat anything recovered from it that belongs to this thread as your own earlier context, and never narrate it as a prior or separate conversation."

# Sandbox note. The claude subprocess inherits Ember's PATH with
# /app/.venv/bin first, so `python3` in Bash is the app venv: it sees only
# venv packages, never a system-site or `pip --user` install. Before this
# note the model spent whole turns "searching for PIL" — trying alternate
# interpreters, PYTHONPATH tricks and `pip install` (which silently piled
# 750 MB of orphaned wheels into /data/.local). Static text, byte-stable
# after a one-time cache invalidation (TLC-12).
_SANDBOX_NOTE = """## Sandbox

Bash runs inside Ember's container as an unprivileged user. `python3` is the app venv and already has Pillow (`from PIL import Image`), numpy, openpyxl and xlsx2csv; poppler (`pdftotext`, `pdftoppm`) is on PATH. Nothing can be installed at runtime — `pip install` fails by design — so never try it and don't hunt for other interpreters: work with what is preinstalled or use the bridge tools."""

# Injected only for conversations on the siri channel (doc #55 step 3).
# The reply is spoken aloud by Siri, so formatting is not just useless —
# it gets read out as punctuation noise. Static text, so the system
# block stays byte-stable per conversation (TLC-12): a conversation
# never changes channel.
_SIRI_VOICE_NOTE = """## Voice channel

Your reply will be spoken aloud by Siri. Answer in one to three short spoken sentences of plain prose — no markdown, no headers, no bullet lists, no tables, no links, no code blocks, no emoji. If the full answer genuinely needs more than that, give the short spoken version and say you can post the details to Discord."""


def _display_tz() -> tzinfo:
    """Local tz for rendering stamps. IANA name via EMBER_DISPLAY_TZ
    (e.g. 'America/New_York'); UTC when unset or invalid. Set this (or the
    container's TZ) or 'lunch vs dinner' reasoning runs on the UTC clock."""
    name = os.environ.get("EMBER_DISPLAY_TZ")
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


def _human_gap(delta: timedelta) -> str | None:
    """Precomputed elapsed-gap phrase, or None below the threshold. Computed
    here (not left to the model) because Sonnet is unreliable at date
    arithmetic — it reads a stated gap far more dependably than it subtracts
    two dates."""
    if delta < _GAP_NOTE_AFTER:
        return None
    days = delta.days
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''} after previous message"
    hours = int(delta.total_seconds() // 3600)
    return f"{hours} hours after previous message"


def _timestamp_prefix(text: str, *, now: datetime, previous: datetime | None) -> str:
    """Prepend `[Weekday YYYY-MM-DD HH:MM]` (plus a gap note after a long
    silence) to a user turn. `now`/`previous` are tz-aware UTC; the stamp
    renders in the display tz."""
    stamp = now.astimezone(_display_tz()).strftime("%a %Y-%m-%d %H:%M")
    if previous is not None:
        if previous.tzinfo is None:  # defensive; _parse_dt already returns UTC-aware
            previous = previous.replace(tzinfo=timezone.utc)
        gap = _human_gap(now - previous)
        if gap:
            stamp = f"{stamp} · {gap}"
    return f"[{stamp}]\n{text}"


# Pre-turn proposal-decision regexes (UoW-8 confirm/discard/edit).
_YES_RE = re.compile(r"^\s*y(?:es)?\s*[.!]?\s*$", re.IGNORECASE)
_NO_RE = re.compile(r"^\s*n(?:o)?\s*[.!]?\s*$", re.IGNORECASE)
# `y 418902` — a yes carrying a TOTP code, for tools in
# `settings.confirm_totp_tools`. The code is captured loosely (digits and
# spacing) and validated by the verifier, so a mistyped code reports as a
# bad code rather than falling through as an unrelated message.
_YES_CODE_RE = re.compile(r"^\s*y(?:es)?[\s,:]+([\d\s]{4,12})[.!]?\s*$", re.IGNORECASE)
_EDIT_RE = re.compile(r"^\s*edit\s+(.+)$", re.IGNORECASE | re.DOTALL)
# `unlock 418902` — proactive unlock of the conversation (doc #60 UoW-36 b).
_UNLOCK_RE = re.compile(r"^\s*unlock[\s,:]+([\d\s]{4,12})[.!]?\s*$", re.IGNORECASE)

# BR-FC-11: Schwab redirect-URL paste detector. We only intercept if the
# message is plausibly a redirect URL — the prefix matches the documented
# callback (`https://127.0.0.1:8182`) and both `code=` and `state=` query
# params are present. Any other message (questions, banter, "what was
# that command?") falls through to the LLM with the pending state intact.
_SCHWAB_REDIRECT_PREFIX = "https://127.0.0.1:8182/?"


def _looks_like_schwab_redirect_url(text: str) -> bool:
    if not text.startswith(_SCHWAB_REDIRECT_PREFIX):
        return False
    return "code=" in text and "state=" in text


class Agent:
    """The turn loop. Channel-agnostic per TLC-14."""

    def __init__(
        self,
        *,
        store: Store,
        channel: Channel,
        settings: Settings,
        provider: Provider | None = None,
        router: ProviderRouter | None = None,
        identity: IdentityStore | None = None,
        memory_tracker: MemoryRevertTracker | None = None,
        skill_registry: SkillRegistry | None = None,
        tool_registry: Registry | None = None,
        rotator: SessionRotator | None = None,
        proposer: Proposer | None = None,
        writer: ProfileWriter | None = None,
        unlock_store: UnlockStore | None = None,
    ) -> None:
        if router is None and provider is None:
            raise ValueError("Agent requires either `provider` or `router`")
        # Doc #60 unlock windows. None → locked-set tools behave like plain
        # per-call confirmations (the pre-#60 flow).
        self._unlock_store = unlock_store
        # `_router` is used for sub-agent dispatch routing (UseSkillTool)
        # AND, when `provider` is None, for resolving the parent's
        # primary provider from `conversation.provider_route`. When both
        # are passed (FC-18 wiring tests), `provider` is the parent's
        # primary and `router` is the sub-agent dispatch router.
        if router is None:
            router = ProviderRouter({provider.name: provider})  # type: ignore[union-attr]
        self._store = store
        self._router = router
        self._parent_provider_override = provider if provider is not None else None
        self._channel = channel
        self._settings = settings
        # Built lazily and cached for the process lifetime: the verifier
        # remembers redeemed counters, so rebuilding it per turn would
        # reopen the replay window it exists to close.
        self._totp_verifier: TotpVerifier | None = None
        self._totp_verifier_built = False
        self._identity = identity
        self._memory_tracker = memory_tracker
        self._skill_registry = skill_registry
        self._tool_registry = tool_registry
        self._rotator = rotator
        self._proposer = proposer
        self._writer = writer

    async def handle_turn(self, msg: ChannelMessage) -> None:
        """Run one full turn. Catches all exceptions and surfaces them
        as user-visible status messages (TLC-7) — never raises out."""
        # Bind the bridge's active-conversation handle for the duration of
        # the turn so MCP-routed tools (claude_cli path) can post
        # attachments back into this thread. Cleared in `finally` to
        # guarantee no leak if a later turn runs in a different thread.
        _set_active_bridge(self._channel, msg.conversation_key)
        # BR-TLC-5: mark this as a user-driven turn so tools that must
        # refuse to run from automated paths (e.g. schwab_place_orders)
        # can branch on it. ContextVar is task-local so concurrent
        # background tasks (scheduler) don't see this flag.
        token = _in_interactive_turn.set(True)
        # v16 (TLC-27 / UoW-31): turn-scoped liveness — typing keepalive
        # plus the single activity status message. Discord turns only
        # (siri keeps today's buffered path, doc #57 non-goals), behind
        # the `turn_activity_enabled` kill-switch. Started here — before
        # any pre-turn short-circuit — so typing shows within ~2s of turn
        # start, and finished in the `finally` so the activity message is
        # deleted on EVERY terminal path: success, error, interrupt
        # (CancelledError flows past `except Exception` with the finally
        # intact), or timeout.
        activity: _TurnActivity | None = None
        if msg.channel == "discord" and getattr(self._settings, "turn_activity_enabled", True):
            activity = _TurnActivity(
                channel=self._channel,
                conversation_key=msg.conversation_key,
                show_tool_calls=getattr(self._settings, "show_tool_calls", True),
            )
            activity.start()
        try:
            await self._handle_turn_inner(msg, activity=activity)
        except Exception as e:
            _log.exception("agent loop internal error")
            await self._safe_send(
                msg.conversation_key,
                f"❌ internal error: {type(e).__name__}",
                kind="status",
            )
        finally:
            if activity is not None:
                await activity.finish()
            _in_interactive_turn.reset(token)
            _clear_active_bridge()

    async def _handle_turn_inner(
        self,
        msg: ChannelMessage,
        *,
        activity: _TurnActivity | None = None,
    ) -> None:
        conversation = await self._store.get_or_create_conversation(
            msg.channel, msg.conversation_key
        )

        # Pre-turn proposal-decision short-circuit (UoW-8). Only fires when
        # a writer is wired AND a pending proposal exists. `consume_pending_proposal`
        # always clears the row (even on "irrelevant" replies — next turn supersedes).
        if self._writer is not None:
            pending_json = await self._store.consume_pending_proposal(conversation.id)
            if pending_json:
                handled = await self._handle_proposal_decision(conversation, pending_json, msg)
                if handled:
                    return

        # Proactive unlock (doc #60 UoW-36 b): `unlock <code>` with no
        # pending prompt. Consumes the turn either way.
        if self._unlock_store is not None and _UNLOCK_RE.match((msg.text or "").strip()):
            await self._handle_proactive_unlock(conversation, msg)
            return

        # Pre-turn tool-confirmation short-circuit (TLC-3). Only fires
        # when a tool_registry is wired AND a pending tool confirmation
        # exists. `consume_pending_tool_confirmation` always clears the
        # row (even on "irrelevant" replies — next turn supersedes).
        # Profile-proposal handler runs first; if it consumed the turn
        # we never reach here.
        if self._tool_registry is not None:
            pending_tool_json = await self._store.consume_pending_tool_confirmation(conversation.id)
            if pending_tool_json:
                outcome = await self._handle_tool_confirmation(conversation, pending_tool_json, msg)
                if outcome is True:
                    return
                if isinstance(outcome, ChannelMessage):
                    # A confirmed call failed: the ❌ is already out, now
                    # let the model explain and offer the next step rather
                    # than leaving the user at a dead end.
                    msg = outcome

        # Pre-turn Schwab re-auth short-circuit (BR-FC-11). Peeks (does
        # NOT consume) so an unrelated message — "what was that command?"
        # — flows to the LLM normally. Only an expired TTL or a
        # successfully-parsed redirect URL clears the pending state.
        # TLC-1 (Discord allowlist) is enforced upstream by the channel
        # adapter; no extra check needed here.
        pending_reauth_json = await self._store.get_pending_schwab_reauth(conversation.id)
        if pending_reauth_json:
            handled = await self._handle_schwab_reauth(conversation, pending_reauth_json, msg)
            if handled:
                return

        # Pre-turn session rotation (UoW-28 / TLC-25). Runs after the
        # pending-state short-circuits (a rotation must not eat a y/n
        # redemption) and BEFORE the user message is persisted, so the
        # fresh session + handoff summary are in place when this turn's
        # prompt is assembled. `maybe_rotate` never raises; on rotation,
        # re-fetch the conversation so the cleared session id /
        # superseded summaries are what this turn sees.
        if self._rotator is not None:
            rotation = await self._rotator.maybe_rotate(
                conversation, now=datetime.now(timezone.utc)
            )
            if rotation.rotated:
                # Doc #60: a rotated session re-locks (the unlock was given
                # to *this* thread of work; a fresh session starts locked).
                if self._unlock_store is not None:
                    self._unlock_store.lock(msg.conversation_key)
                refreshed = await self._store.get_conversation(conversation.id)
                if refreshed is not None:
                    conversation = refreshed

        # Persist the user message. When the channel delivered file
        # uploads, append a stable manifest so the model sees the saved
        # paths as part of the user turn (BC-4 / BC-5). The manifest is
        # part of the persisted text so it survives reloads, shows up in
        # `/context`, and is included in compaction.
        persisted_text = _user_text_with_manifest(msg)
        # Temporal context (TC-1): stamp the turn before persisting so it lands
        # in the DB, the API-provider messages array, AND the claude_cli prompt
        # (which claude writes verbatim into its --resume session file → all
        # future reconstructed history carries the stamp, frozen). last_active
        # is the previous turn's end (touch_conversation runs at end-of-turn),
        # so now - last_active is the true silence gap.
        persisted_text = _timestamp_prefix(
            persisted_text,
            now=datetime.now(timezone.utc),
            previous=conversation.last_active,
        )
        user_message_id = await self._store.append_message(
            conversation.id,
            "user",
            persisted_text,
        )
        # From here on the user's text is durably in the store, so a gate
        # interrupt can safely discard this turn: the next turn rebuilds
        # history and picks the message up. Before this line it could not.
        mark_user_message_persisted()

        active = self._resolve_active_skill(conversation)

        stored = await self._store.recent_messages(conversation.id)
        history = [s.to_message() for s in stored]
        system = await self._assemble_system_block(conversation, active=active)
        # Per-turn tool overlay (FC-18 wiring A). In router mode with a
        # non-empty skill registry, build a fresh `UseSkillTool` carrying
        # the parent's just-persisted user_message_id and the parent's
        # conversation_key. The overlay is consulted before the global
        # tool_registry during dispatch — `use_skill` is no longer
        # globally registered.
        per_turn_tools = self._build_per_turn_tools(
            conversation=conversation,
            active=active,
            parent_message_id=user_message_id,
        )
        tools = self._tool_specs_for_active_skill(active, per_turn_tools)

        if self._parent_provider_override is not None:
            # Caller explicitly named a parent provider via `provider=`;
            # use it directly (the router stays for sub-agent dispatch).
            primary = ResolvedRoute(
                provider=self._parent_provider_override,
                model=None,
                route=self._parent_provider_override.name,
            )
        else:
            try:
                primary = self._router.resolve(conversation.provider_route)
            except ConfigError:
                available = self._router.names()
                if not available:
                    raise
                fallback_name = available[0]
                _log.warning(
                    "route %r not configured; using %r",
                    conversation.provider_route,
                    fallback_name,
                )
                # Surface the misconfig to the user. The silent fallback
                # has bitten before — a stale `provider_route` (e.g. a
                # typo'd /model set) routed turns to a text-only provider
                # that strips tools, so the model would politely say it
                # had no capabilities. Make the gap visible.
                await self._safe_send(
                    msg.conversation_key,
                    (
                        f"⚠️ route `{conversation.provider_route}` not configured; "
                        f"falling back to `{fallback_name}`. "
                        f"Fix with `/model action:set value:<valid>:<model>`."
                    ),
                    kind="status",
                )
                primary = self._router.resolve(fallback_name)

        # v16: ONE stream observer for the whole turn, shared by every
        # provider call (primary, session-retry, failover, dispatch loop),
        # so its posted-block count survives into the final-send remainder
        # math (UoW-32) and its activity notifications keep flowing.
        # Block streaming is gated here — discord + claude_cli +
        # `stream_text_blocks` (TLC-28); a siri key can't pass the channel
        # check, and the deepseek failover emits no assistant stream
        # events, so a mid-turn failover can't post blocks.
        stream_blocks = (
            msg.channel == "discord"
            and primary.provider.name == "claude_cli"
            and getattr(self._settings, "stream_text_blocks", True)
        )
        observer = self._make_stream_handler(
            msg.conversation_key,
            conversation.id,
            activity=activity,
            stream_blocks=stream_blocks,
        )

        result = None
        used_route = primary.route
        used_failover = False
        # The route that actually produced `result`. Stays `primary` unless
        # we fail over, in which case the tool-dispatch loop must continue on
        # the fallback too — not the original (rate-limited / auth-down)
        # provider.
        active_route: ResolvedRoute = primary

        try:
            await self._channel.set_typing(msg.conversation_key, on=True)
            result = await primary.provider.complete(
                system=system,
                messages=history,
                tools=tools,
                model=primary.model,
                session_id=conversation.claude_cli_session_id,
                stream_handler=observer,
            )
        except RateLimitError as e:
            if (
                conversation.auto_failover_on_rate_limit
                and self._router.has(_FAILOVER_ROUTE)
                and primary.provider.name != _FAILOVER_ROUTE
            ):
                _log.info("UoW-15 failover: %s → %s", primary.route, _FAILOVER_ROUTE)
                try:
                    fallback = self._router.resolve(_FAILOVER_ROUTE)
                    await self._safe_send(
                        msg.conversation_key,
                        f"🔁 `{primary.route}` rate-limited — falling back to "
                        f"`{fallback.route}` for this turn.",
                        kind="status",
                    )
                    result = await fallback.provider.complete(
                        system=system,
                        messages=history,
                        tools=tools,
                        model=fallback.model,
                        session_id=None,  # API doesn't share claude CLI's session
                        stream_handler=observer,
                    )
                    used_route = fallback.route
                    used_failover = True
                    active_route = fallback
                except Exception as e2:
                    await self._safe_send(
                        msg.conversation_key,
                        f"🪫 rate limited and failover also failed: {e2}",
                        kind="status",
                    )
                    return
            else:
                hint = (
                    " (toggle on with `/model action:failover value:on`)"
                    if not conversation.auto_failover_on_rate_limit
                    and self._router.has(_FAILOVER_ROUTE)
                    else ""
                )
                await self._safe_send(
                    msg.conversation_key,
                    f"🪫 rate limited: {e}{hint}",
                    kind="status",
                )
                return
        except SessionNotFoundError:
            # claude pruned (or a /data rebuild lost) the session jsonl we
            # tried to --resume. Drop the dead id and retry once on a fresh
            # session. claude-side context is gone, but the turn succeeds
            # instead of hard-failing — the alternative was a dead thread
            # that needed a manual DB edit to revive.
            _log.warning(
                "claude session %s missing for conversation %d; retrying fresh",
                conversation.claude_cli_session_id,
                conversation.id,
            )
            await self._store.update_claude_cli_session(conversation.id, None)
            try:
                result = await primary.provider.complete(
                    system=system,
                    messages=history,
                    tools=tools,
                    model=primary.model,
                    session_id=None,
                    stream_handler=observer,
                )
            except ProviderError as e:
                await self._safe_send(
                    msg.conversation_key,
                    f"⚠️ provider error: {e}",
                    kind="status",
                )
                return
        except AuthError as e:
            # An auth failure is a hard outage that won't self-resolve
            # (unlike a rate limit), so we fail over UNCONDITIONALLY — no
            # /model flag gate — to keep the bot answering. The claude_cli
            # subscription token expiring / refresh-racing is the case that
            # bit us; deepseek carries the conversation until a human
            # re-auths.
            failed_over = False
            if self._router.has(_FAILOVER_ROUTE) and primary.provider.name != _FAILOVER_ROUTE:
                _log.warning(
                    "auth failover: %s auth down → %s (%s)",
                    primary.route,
                    _FAILOVER_ROUTE,
                    e,
                )
                try:
                    fallback = self._router.resolve(_FAILOVER_ROUTE)
                    await self._safe_send(
                        msg.conversation_key,
                        f"🔐 `{primary.route}` auth is down — running on "
                        f"`{fallback.route}` until it's re-authed.",
                        kind="status",
                    )
                    result = await fallback.provider.complete(
                        system=system,
                        messages=history,
                        tools=tools,
                        model=fallback.model,
                        session_id=None,  # API doesn't share claude CLI's session
                        stream_handler=observer,
                    )
                    used_route = fallback.route
                    used_failover = True
                    active_route = fallback
                    failed_over = True
                except Exception as e2:
                    await self._safe_send(
                        msg.conversation_key,
                        f"🔒 auth down and failover also failed: {e2}",
                        kind="status",
                    )
            # Proactively ping (once per outage) when it's the claude_cli
            # subscription that's down — that's the credential a human must
            # renew. Fires whether or not the failover itself succeeded.
            if primary.provider.name == "claude_cli":
                await self._alert_claude_auth_down(e)
            if not failed_over:
                if primary.provider.name == "claude_cli":
                    await self._safe_send(
                        msg.conversation_key,
                        "🔒 claude auth is down and no fallback is available. "
                        "Re-auth on the server — copy and run the command in "
                        "the next message:",
                        kind="status",
                    )
                    await self._safe_send(
                        msg.conversation_key,
                        _CLAUDE_REAUTH_COMMAND,
                        kind="status",
                    )
                else:
                    await self._safe_send(
                        msg.conversation_key,
                        f"🔒 auth error: {e}",
                        kind="status",
                    )
                return
            # Failed over successfully — fall through to normal handling.
        except TransportError as e:
            await self._safe_send(
                msg.conversation_key,
                f"📡 transport error: {e}",
                kind="status",
            )
            return
        except ProviderError as e:
            await self._safe_send(
                msg.conversation_key,
                f"⚠️ provider error: {e}",
                kind="status",
            )
            return
        finally:
            await self._safe_set_typing(msg.conversation_key, on=False)

        assert result is not None  # all error branches return early

        # Claude auth proved healthy this turn — drop any standing auth-down
        # alert so a future outage re-alerts. Cheap: only writes when set.
        if primary.provider.name == "claude_cli" and not used_failover:
            await self._clear_claude_auth_alert()

        # In-process tool dispatch (FC-17). When the provider returns
        # tool_calls AND any of those calls is dispatchable (per-turn
        # overlay or global tool_registry), run the dispatch loop until
        # exhaustion. Bounded to prevent infinite loops.
        if (
            (self._tool_registry is not None or per_turn_tools)
            and result.tool_calls
            and any(self._tool_in_registry(tc.name, per_turn_tools) for tc in result.tool_calls)
        ):
            result = await self._run_tool_dispatch_loop(
                conversation=conversation,
                msg=msg,
                primary_provider=active_route.provider,
                primary_model=active_route.model,
                primary_route=used_route,
                used_failover=used_failover,
                system=system,
                history=history,
                first_result=result,
                tools=tools,
                per_turn_tools=per_turn_tools,
                stream_handler=observer,
            )
            if result is None:
                # Dispatch loop reported a fatal error and returned early
                # (already sent a status message). Just bail.
                return

        assistant_message_id = await self._store.append_message(
            conversation.id,
            "assistant",
            result.text,
            provider_route=used_route,
            usage=result.usage,
            # Bridge-executed calls (UoW-4 step 5) are audit-only: they
            # already ran provider-side and never reach the dispatch loop.
            tool_calls=[*result.executed_tool_calls, *result.tool_calls] or None,
            failover=used_failover,
        )
        if result.session_id is not None and not used_failover:
            await self._store.update_claude_cli_session(conversation.id, result.session_id)
        await self._store.touch_conversation(conversation.id)

        # v16 (TLC-28 / UoW-32): when the observer streamed text blocks
        # mid-turn, the final send carries only the unstreamed remainder —
        # `text_parts[posted:]` joined with the provider's pinned
        # separator, never character math — and is skipped entirely when
        # everything streamed. `text_parts is None` (deepseek failover,
        # any non-reporting provider) or zero posted blocks → the legacy
        # single send of `result.text`, byte-identical to pre-v16.
        posted_blocks = observer.posted_block_count
        all_native_ids: list[str] = list(observer.streamed_message_ids)
        if result.text_parts is not None and posted_blocks > 0:
            remainder = TEXT_PARTS_SEPARATOR.join(result.text_parts[posted_blocks:])
            if remainder:
                all_native_ids.extend(
                    await self._channel.send(msg.conversation_key, remainder, kind="final")
                )
        else:
            all_native_ids.extend(
                await self._channel.send(msg.conversation_key, result.text, kind="final")
            )
        # Map ALL native channel message ids — streamed blocks and the
        # final remainder alike — to this assistant row so a reaction on
        # any piece of the reply resolves to its `messages` row (UoW-20 /
        # FC-20, unchanged in spirit per TLC-28). Best-effort: a mapping
        # failure must not fail the turn.
        if all_native_ids:
            try:
                await self._store.map_channel_messages(
                    msg.channel, all_native_ids, assistant_message_id
                )
            except Exception:
                _log.exception("map_channel_messages failed; feedback on this reply won't resolve")

        # Post-turn context measurement (FC-28 / TLC-24). Providers that
        # measure (claude_cli) set `context_tokens`; everyone else leaves
        # it None and the stored value stays untouched. Failures are
        # non-fatal (same posture as the retired compactor hook).
        if result.context_tokens is not None:
            try:
                await self._store.update_last_context_tokens(conversation.id, result.context_tokens)
            except Exception:
                _log.exception("update_last_context_tokens failed; turn continues")

        # Post-turn profile-update proposal (UoW-8 / FC-8). Failures non-fatal.
        if self._proposer is not None:
            try:
                current_profile = (
                    self._identity.read_profile() if self._identity is not None else ""
                )
                proposal = await self._proposer.propose(
                    user_msg=msg.text, current_profile=current_profile
                )
                if proposal is not None:
                    payload = json.dumps(
                        {
                            "proposed_text": proposal.proposed_text,
                            "diff": proposal.diff,
                            "source_message_id": user_message_id,
                        }
                    )
                    await self._store.set_pending_proposal(conversation.id, payload)
                    status_text = (
                        "📝 Proposed profile update:\n```\n"
                        f"{proposal.diff}\n"
                        "```\n"
                        "Reply `y` / `n` / `edit <text>`."
                    )
                    await self._safe_send(msg.conversation_key, status_text, kind="status")
            except Exception:
                _log.exception("proposer failed; turn continues")

    # --- Helpers ----------------------------------------------------------

    def _tool_in_registry(
        self,
        name: str,
        per_turn_tools: dict[str, Tool] | None = None,
    ) -> bool:
        if per_turn_tools and name in per_turn_tools:
            return True
        if self._tool_registry is None:
            return False
        try:
            self._tool_registry.get(name)
            return True
        except KeyError:
            return False

    async def _dispatch_tool_call(
        self,
        name: str,
        args: dict[str, Any],
        per_turn_tools: dict[str, Tool] | None,
    ) -> ToolResult:
        """Dispatch a tool call, preferring the per-turn overlay over the
        global registry. The overlay always wins so per-turn instances
        carry conversation-scoped state without touching the global
        registry.
        """
        if per_turn_tools and name in per_turn_tools:
            tool = per_turn_tools[name]
            return await tool.call(args)
        assert self._tool_registry is not None  # caller's guard
        return await self._tool_registry.call_tool(name, args)

    # Backoff (seconds) before each retry of a transient tool failure.
    # Two retries (three attempts total) per the TransportError contract.
    _TOOL_RETRY_BACKOFF: tuple[float, ...] = (0.5, 1.5)

    async def _dispatch_tool_call_with_retry(
        self,
        name: str,
        args: dict[str, Any],
        per_turn_tools: dict[str, Tool] | None,
    ) -> ToolResult:
        """Dispatch a tool call, retrying transient failures with backoff.

        Tools signal a transient failure by raising ``ToolRetryable`` or
        ``TransportError`` (network blip, DNS hiccup, upstream 5xx). A brief
        outage in the shared egress/DNS path otherwise surfaces a single
        flake straight to the model as a hard error; absorbing it here keeps
        the agent from striking out on a recoverable glitch. Non-transient
        errors (and the final attempt) propagate to the caller unchanged.
        """
        for backoff in self._TOOL_RETRY_BACKOFF:
            try:
                return await self._dispatch_tool_call(name, args, per_turn_tools)
            except (ToolRetryable, TransportError) as e:
                _log.warning(
                    "tool %s transient failure (%s); retrying in %.1fs",
                    name,
                    e,
                    backoff,
                )
                await asyncio.sleep(backoff)
        # Last attempt: let success return and any error propagate.
        return await self._dispatch_tool_call(name, args, per_turn_tools)

    async def _run_tool_dispatch_loop(
        self,
        *,
        conversation: Conversation,
        msg: ChannelMessage,
        primary_provider: Provider,
        primary_model: str | None,
        primary_route: str,
        used_failover: bool,
        system: str,
        history: list[Any],
        first_result: Any,
        tools: list[ToolSpec] | None,
        per_turn_tools: dict[str, Tool] | None = None,
        stream_handler: StreamHandler | None = None,
    ) -> Any:
        """Dispatch tool calls returned by the provider, feed results
        back, and call the provider again until no tool calls remain or
        a turn budget is hit. Returns the final `CompletionResult`.

        Bounded to a small number of iterations to keep the parent loop
        from spinning. Fast-fails on unknown tools (returns the original
        result, leaving the agent's persistence path to handle it).
        """
        from ember.providers.base import Message  # local to avoid extra top-level import

        max_iterations = 8
        result = first_result
        for _ in range(max_iterations):
            tool_calls = result.tool_calls or []
            if not tool_calls:
                break
            # Persist the assistant turn that requested the tool calls.
            await self._store.append_message(
                conversation.id,
                "assistant",
                result.text or "",
                provider_route=primary_route,
                usage=result.usage,
                tool_calls=tool_calls,
                failover=used_failover,
            )
            # Append assistant turn to history so the next provider call
            # sees the tool-use exchange in the context window.
            history.append(
                Message(
                    role="assistant",
                    content=result.text or "",
                    tool_calls=list(tool_calls),
                )
            )

            for call in tool_calls:
                if not self._tool_in_registry(call.name, per_turn_tools):
                    # Unknown tool: surface as an error tool result and
                    # let the model handle it.
                    tool_text = json.dumps({"error": "unknown_tool", "tool": call.name})
                else:
                    try:
                        tool_result = await self._dispatch_tool_call_with_retry(
                            call.name, call.args, per_turn_tools
                        )
                        tool_text = self._serialize_tool_output(tool_result.output)
                        attachments = list(tool_result.metadata.get("attachments") or [])
                        if attachments:
                            try:
                                await self._channel.send_attachments(
                                    msg.conversation_key, attachments
                                )
                            except Exception:
                                _log.exception("channel.send_attachments failed for %s", call.name)
                    except Exception as e:
                        _log.exception("in-process tool dispatch raised")
                        tool_text = json.dumps(
                            {"error": f"{type(e).__name__}: {e}", "tool": call.name}
                        )

                # Persist tool-result row.
                await self._store.append_message(
                    conversation.id,
                    "tool",
                    tool_text,
                    tool_calls=[call],
                    failover=used_failover,
                )
                history.append(
                    Message(
                        role="tool",
                        content=tool_text,
                        tool_call_id=call.id,
                    )
                )

            # Re-invoke the provider with the tool results in history.
            try:
                result = await primary_provider.complete(
                    system=system,
                    messages=history,
                    tools=tools,
                    model=primary_model,
                    session_id=conversation.claude_cli_session_id,
                    stream_handler=stream_handler,
                )
            except (RateLimitError, AuthError, TransportError, ProviderError) as e:
                await self._safe_send(
                    msg.conversation_key,
                    f"⚠️ provider error during tool dispatch: {e}",
                    kind="status",
                )
                return None
        return result

    @staticmethod
    def _serialize_tool_output(output: Any) -> str:
        if isinstance(output, str):
            return output
        try:
            return json.dumps(output, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(output)

    async def _handle_proposal_decision(
        self,
        conversation: Conversation,
        pending_json: str,
        msg: ChannelMessage,
    ) -> bool:
        """Handle one of {y, n, edit <text>} replies to a pending profile
        proposal. Returns True if the message was a decision (turn
        short-circuits); False if the message is unrelated and the caller
        should proceed with a normal turn.

        Note: `consume_pending_proposal` was already called by the
        caller — the row is gone regardless of branch taken here. This
        is intentional ("next turn supersedes" per UoW-8).
        """
        try:
            data = json.loads(pending_json)
        except (ValueError, TypeError):
            _log.warning("malformed pending proposal JSON; ignoring")
            return False
        proposed_text = data.get("proposed_text", "")
        source_message_id = int(data.get("source_message_id", 0))

        text = msg.text or ""

        if _YES_RE.match(text):
            return await self._commit_proposal(
                conversation,
                user_text=text,
                proposed_text=proposed_text,
                source_message_id=source_message_id,
                conversation_key=msg.conversation_key,
            )

        if _NO_RE.match(text):
            await self._store.append_message(
                conversation.id,
                "user",
                text,
            )
            reply = "🗑️ profile update discarded"
            await self._store.append_message(
                conversation.id,
                "assistant",
                reply,
                provider_route="internal:profile_decision",
            )
            await self._safe_send(msg.conversation_key, reply, kind="final")
            return True

        edit_m = _EDIT_RE.match(text)
        if edit_m:
            new_text = edit_m.group(1).strip()
            return await self._commit_proposal(
                conversation,
                user_text=text,
                proposed_text=new_text,
                source_message_id=source_message_id,
                conversation_key=msg.conversation_key,
            )

        return False

    async def _commit_proposal(
        self,
        conversation: Conversation,
        *,
        user_text: str,
        proposed_text: str,
        source_message_id: int,
        conversation_key: str,
    ) -> bool:
        """Persist user msg, run writer.commit, persist + send synthetic
        assistant reply. Always returns True (we handled the turn)."""
        await self._store.append_message(
            conversation.id,
            "user",
            user_text,
        )
        assert self._writer is not None  # caller's guard
        try:
            result = await self._writer.commit(
                proposed_text,
                conversation_id=conversation.id,
                source_message_id=source_message_id,
            )
        except Exception as e:
            _log.exception("profile writer raised")
            reply = f"❌ profile update failed: {type(e).__name__}: {e}"
        else:
            if result.committed:
                reply = "✅ profile updated"
            else:
                reply = f"❌ profile update failed: {result.error or 'unknown error'}"

        await self._store.append_message(
            conversation.id,
            "assistant",
            reply,
            provider_route="internal:profile_decision",
        )
        await self._safe_send(conversation_key, reply, kind="final")
        return True

    def _totp_gated_tools(self) -> frozenset[str]:
        return frozenset(getattr(self._settings, "confirm_totp_tools", ()) or ())

    def _get_totp_verifier(self) -> TotpVerifier | None:
        """The process-wide verifier, or None when no secret is configured.

        A malformed secret is treated as absent — and, because gated tools
        fail closed, that means their confirmations are refused rather than
        silently falling back to a bare `y`.
        """
        if not self._totp_verifier_built:
            self._totp_verifier_built = True
            secret = getattr(self._settings, "confirm_totp_secret", None)
            raw = secret.get_secret_value() if secret is not None else ""
            if raw:
                try:
                    self._totp_verifier = TotpVerifier(raw)
                except TotpError:
                    _log.exception(
                        "CONFIRM_TOTP_SECRET is malformed; TOTP-gated tools "
                        "will refuse every confirmation until it is fixed"
                    )
                    self._totp_verifier = None
        return self._totp_verifier

    def _classify_confirmation_reply(
        self, text: str, tool_name: str, *, force_code: bool = False
    ) -> tuple[str, str]:
        """Classify a reply to a pending confirmation prompt.

        Returns `(decision, detail)` where decision is one of:
          - `"yes"`       — redeem the pending call
          - `"no"`        — discard it
          - `"refused"`   — a yes that failed the second factor; `detail`
                            is the user-facing reason
          - `"unrelated"` — not a decision; fall through to a normal turn

        Tools listed in `confirm_totp_tools` accept only `y <code>` with a
        currently-valid TOTP. Everything else keeps the historical bare `y`.
        """
        # Declining is never gated: refusing an action must always be at
        # least as easy as approving it.
        if _NO_RE.match(text):
            return ("no", "")

        if not force_code and tool_name not in self._totp_gated_tools():
            return ("yes", "") if _YES_RE.match(text) else ("unrelated", "")

        match = _YES_CODE_RE.match(text)
        is_bare_yes = bool(_YES_RE.match(text))
        verifier = self._get_totp_verifier()

        if verifier is None:
            # Fail closed. Listing a tool as gated and then losing the
            # secret must not reopen the bare-`y` path.
            if match or is_bare_yes:
                return (
                    "refused",
                    f"{tool_name} requires a confirmation code, but no valid "
                    "CONFIRM_TOTP_SECRET is configured.",
                )
            return ("unrelated", "")

        if match:
            if verifier.verify(match.group(1)):
                return ("yes", "")
            return ("refused", "Confirmation code rejected.")

        if is_bare_yes:
            return (
                "refused",
                f"{tool_name} needs your confirmation code — reply `y <code>`.",
            )
        return ("unrelated", "")

    async def _handle_tool_confirmation(
        self,
        conversation: Conversation,
        pending_json: str,
        msg: ChannelMessage,
    ) -> bool | ChannelMessage:
        """Handle a y/n reply to a pending tool-confirmation prompt
        (TLC-3). Returns True if the message was a yes/no decision (turn
        short-circuits); False if it's unrelated and the caller should
        proceed with a normal turn; a ChannelMessage when a confirmed call
        FAILED — the caller runs a normal turn on it so the model can
        follow up (the failure note replaces the user's `y`).

        Note: `consume_pending_tool_confirmation` was already called by
        the caller — the row is gone regardless of branch taken here.
        This is intentional ("next turn supersedes" — mirrors UoW-8).

        Spec carve-out: when `tool_registry is None`, this method is
        never reached; the caller skips the consume and falls through
        to a normal turn.
        """
        text = (msg.text or "").strip()

        # Parse the pending payload up front: whether a bare `y` is even an
        # acceptable redemption depends on which tool is pending (TOTP-gated
        # tools require `y <code>`).
        try:
            payload = json.loads(pending_json)
            calls = payload.get("calls") or [payload]
            calls = [{"tool_name": c["tool_name"], "args": c.get("args") or {}} for c in calls]
            if not calls:
                raise KeyError("calls")
        except (ValueError, TypeError, KeyError):
            _log.warning("malformed pending tool confirmation JSON; discarding")
            return False

        # One reply decides every call gated this turn. Classify against
        # the strictest of them: if any needs the second factor, the
        # reply must carry it.
        gated = self._totp_gated_tools()
        tool_name = next(
            (c["tool_name"] for c in calls if c["tool_name"] in gated), calls[0]["tool_name"]
        )
        # Doc #60: an `unlock` prompt opens a window instead of running the
        # pending call, and always needs the second factor.
        unlock_mode = payload.get("mode") == "unlock" and self._unlock_store is not None

        decision, detail = self._classify_confirmation_reply(
            text, tool_name, force_code=unlock_mode
        )

        # Second factor missing or wrong → refuse. The pending row was
        # already consumed by the caller, so there is no live prompt left to
        # guess against; the user re-asks and gets a fresh code window.
        if decision == "refused":
            _log.warning("TOTP gate refused a confirmation for %s: %s", tool_name, detail)
            await self._store.append_message(conversation.id, "user", msg.text)
            reply = f"\u274c {detail} Nothing was executed — ask again to retry."
            await self._store.append_message(
                conversation.id,
                "assistant",
                reply,
                provider_route="internal:tool_confirmation",
            )
            await self._safe_send(msg.conversation_key, reply, kind="final")
            await self._store.touch_conversation(conversation.id)
            return True

        # Yes → execute the pending call directly via call_tool with
        # allow_unconfirmed=True; provider is NOT invoked.
        if decision == "yes" and unlock_mode:
            assert self._unlock_store is not None
            until = self._unlock_store.unlock(msg.conversation_key)
            await self._store.append_message(conversation.id, "user", msg.text)
            reply = self._format_unlocked(tool_name, until)
            await self._store.append_message(
                conversation.id,
                "assistant",
                reply,
                provider_route="internal:tool_confirmation",
            )
            await self._safe_send(msg.conversation_key, reply, kind="status")
            await self._store.touch_conversation(conversation.id)
            _log.info(
                "unlock: %s until %s (%s)", msg.conversation_key, until.isoformat(), tool_name
            )
            # Hand the turn back to the model: it re-issues the calls it
            # was blocked on, which now run inside the window. The values
            # go to the model as tool results, not to the screen.
            actions = "; ".join(
                str(c.get("action") or c["tool_name"]) for c in (payload.get("calls") or [payload])
            )
            return self._unlock_followup(msg, tool_name, until, actions)

        if decision == "yes":
            assert self._tool_registry is not None  # caller's guard

            await self._store.append_message(
                conversation.id,
                "user",
                msg.text,
            )
            replies: list[str] = []
            failures: list[str] = []
            for call in calls:
                name, args = call["tool_name"], call["args"]
                try:
                    result = await self._tool_registry.call_tool(name, args, allow_unconfirmed=True)
                    replies.append(self._format_tool_confirmation_reply(name, result))
                except Exception as e:
                    _log.exception("tool confirmation execution failed")
                    line = f"❌ {name} failed: {type(e).__name__}: {e}"
                    replies.append(line)
                    failures.append(line)
            reply = "\n".join(replies)

            await self._store.append_message(
                conversation.id,
                "assistant",
                reply,
                provider_route="internal:tool_confirmation",
            )
            await self._safe_send(msg.conversation_key, reply, kind="final")
            await self._store.touch_conversation(conversation.id)
            if failures:
                return self._confirmation_failure_followup(msg, failures)
            return True

        # No → discard; persist user msg + cancel reply. Declining never
        # requires the second factor: refusing an action must always be at
        # least as easy as approving it.
        if decision == "no":
            await self._store.append_message(
                conversation.id,
                "user",
                msg.text,
            )
            reply = "🗑️ cancelled"
            await self._store.append_message(
                conversation.id,
                "assistant",
                reply,
                provider_route="internal:tool_confirmation",
            )
            await self._safe_send(msg.conversation_key, reply, kind="final")
            await self._store.touch_conversation(conversation.id)
            return True

        # Anything else → next turn supersedes (consume already cleared
        # the row); fall through to the normal turn flow.
        return False

    @staticmethod
    def _format_unlocked(tool_name: str, until: datetime) -> str:
        return f"🔓 {tool_name} unlocked in this thread until {until.astimezone():%H:%M %Z}. `/lock` ends it early."

    @staticmethod
    def _unlock_followup(
        msg: ChannelMessage, tool_name: str, until: datetime, actions: str
    ) -> ChannelMessage:
        """The synthetic user turn after an unlock: continue the work that
        was blocked. The user's reply (which carries the code) is not
        repeated."""
        note = (
            f"[Automatic follow-up] The user authenticated; {tool_name} is unlocked in "
            f"this conversation until {until.astimezone():%H:%M %Z}. Continue the task: "
            f"re-issue the call(s) you were blocked on ({actions}) — they will run now "
            "without prompting. Use credential values in your work; never print a "
            "secret value in chat unless the user explicitly asked to see it."
        )
        return dataclasses.replace(
            msg, text=note, message_id=f"{msg.message_id}:unlock-followup", attachments=()
        )

    async def _handle_proactive_unlock(
        self, conversation: Conversation, msg: ChannelMessage
    ) -> None:
        """`unlock <code>` with no pending prompt (doc #60 UoW-36 b)."""
        assert self._unlock_store is not None
        text = (msg.text or "").strip()
        match = _UNLOCK_RE.match(text)
        verifier = self._get_totp_verifier()
        if verifier is None:
            reply = "❌ unlock is not configured (no valid CONFIRM_TOTP_SECRET)."
        elif match and verifier.verify(match.group(1)):
            until = self._unlock_store.unlock(msg.conversation_key)
            reply = self._format_unlocked("this conversation", until)
            _log.info("unlock (proactive): %s until %s", msg.conversation_key, until.isoformat())
        else:
            reply = "❌ Confirmation code rejected. Nothing was unlocked."
            _log.warning("unlock (proactive) refused for %s", msg.conversation_key)
        await self._store.append_message(conversation.id, "user", msg.text)
        await self._store.append_message(
            conversation.id, "assistant", reply, provider_route="internal:tool_confirmation"
        )
        await self._safe_send(msg.conversation_key, reply, kind="final")
        await self._store.touch_conversation(conversation.id)

    @staticmethod
    def _confirmation_failure_followup(msg: ChannelMessage, failures: list[str]) -> ChannelMessage:
        """The synthetic user turn that follows a failed redemption. The
        model sees what was approved and why it failed, and is told to
        explain and propose the corrected step — not to re-issue the same
        call. The user's reply text (which may carry a TOTP code) is not
        repeated."""
        note = (
            "[Automatic follow-up] The user approved the pending action(s), "
            "but execution failed:\n"
            + "\n".join(failures)
            + "\nIn one or two sentences tell the user what went wrong and "
            "propose the corrected next step (e.g. the right item or field). "
            "Do not repeat the identical call."
        )
        return dataclasses.replace(
            msg, text=note, message_id=f"{msg.message_id}:confirm-followup", attachments=()
        )

    @staticmethod
    def _format_tool_confirmation_reply(tool_name: str, result: ToolResult) -> str:
        if result.is_error:
            return f"❌ {tool_name} failed: {result.output}"
        # If the tool returned a structured `message`, surface it. A
        # `noop=True` flag downgrades the success ✅ to a ⚠️ so the
        # user sees that nothing actually changed (e.g. profile_update
        # called with content identical to current).
        if isinstance(result.output, dict):
            msg = result.output.get("message")
            if isinstance(msg, str) and msg.strip():
                prefix = "⚠️" if result.output.get("noop") else "✅"
                return f"{prefix} {tool_name}: {msg}"
            # If the tool snapshotted a multi-line diff (profile_update,
            # any other write tool that follows the same convention),
            # echo it inside a fenced ```diff block so the user sees
            # exactly what was committed.
            diff = result.output.get("diff_summary")
            if isinstance(diff, str) and "\n" in diff:
                head, _, body = diff.partition("\n")
                return f"✅ {tool_name}: {head}\n```diff\n{body}\n```"
        return f"✅ {tool_name} done."

    async def _handle_schwab_reauth(
        self,
        conversation: Conversation,
        pending_json: str,
        msg: ChannelMessage,
    ) -> bool:
        """BR-FC-11 step 2 — redirect-URL paste detector.

        Returns True if the message was consumed by the re-auth flow
        (TTL-expired notice, redirect-URL exchange, or fail-closed
        error); False to fall through to the normal LLM turn.

        Design rules:
        - Peeks via ``get_pending_schwab_reauth`` (the caller already
          read it). We only call ``consume_*`` when we actually intercept
          the message — a non-redirect message must NOT clear the
          pending state, since the user might just be asking a question
          mid-flow ("wait, what was that command again?").
        - Fails closed: any exception during URL parse / state validate /
          OAuth exchange logs at WARNING, clears the pending state, and
          surfaces a clear user-facing error. We never proceed to the
          token exchange on a malformed input.
        """
        try:
            data = json.loads(pending_json)
            persisted_state = str(data.get("state") or "")
            callback_url = str(data.get("callback_url") or "")
            expires_at = int(data.get("expires_at") or 0)
        except (ValueError, TypeError, KeyError):
            _log.warning("malformed pending_schwab_reauth JSON; clearing")
            await self._store.consume_pending_schwab_reauth(conversation.id)
            return False

        # TTL expired — clear and inform. Always consumes (whether or
        # not the body is a redirect URL — the row was never going to
        # be useful again).
        if int(time.time()) > expires_at:
            await self._store.consume_pending_schwab_reauth(conversation.id)
            await self._store.append_message(conversation.id, "user", msg.text or "")
            reply = "⏱️ schwab re-auth expired; run `/schwab-reauth` again"
            await self._store.append_message(
                conversation.id,
                "assistant",
                reply,
                provider_route="internal:schwab_reauth",
            )
            await self._safe_send(msg.conversation_key, reply, kind="final")
            await self._store.touch_conversation(conversation.id)
            return True

        # Redirect-URL shape detector. The body must look plausibly like
        # a Schwab OAuth callback — same scheme/host/port, with both
        # `code` and `state` query params. If it doesn't, the user is
        # talking about something else and we leave the pending row in
        # place for their actual paste.
        text = (msg.text or "").strip()
        if not _looks_like_schwab_redirect_url(text):
            return False

        # From here on we are committed: parse, validate, exchange.
        # Any failure clears the pending state (fail-closed).
        try:
            parsed = urlsplit(text)
            qs = parse_qs(parsed.query)
            received_state = (qs.get("state") or [""])[0]
        except Exception as e:
            _log.warning("could not parse Schwab redirect URL: %s", type(e).__name__)
            await self._store.consume_pending_schwab_reauth(conversation.id)
            await self._store.append_message(conversation.id, "user", text)
            reply = "❌ couldn't parse that as a Schwab redirect URL. Run `/schwab-reauth` again."
            await self._store.append_message(
                conversation.id,
                "assistant",
                reply,
                provider_route="internal:schwab_reauth",
            )
            await self._safe_send(msg.conversation_key, reply, kind="final")
            await self._store.touch_conversation(conversation.id)
            return True

        if not received_state or received_state != persisted_state:
            _log.warning("Schwab redirect state mismatch")
            await self._store.consume_pending_schwab_reauth(conversation.id)
            await self._store.append_message(conversation.id, "user", text)
            reply = (
                "❌ schwab re-auth state mismatch — the URL doesn't match "
                "the in-flight handshake. Run `/schwab-reauth` again."
            )
            await self._store.append_message(
                conversation.id,
                "assistant",
                reply,
                provider_route="internal:schwab_reauth",
            )
            await self._safe_send(msg.conversation_key, reply, kind="final")
            await self._store.touch_conversation(conversation.id)
            return True

        # State validated. Run the OAuth code-exchange. We must clear
        # the pending row whether this succeeds or fails — a stale row
        # would block any retry from `/schwab-reauth`.
        await self._store.append_message(conversation.id, "user", text)
        try:
            reply = await self._exchange_schwab_code(callback_url, persisted_state, text)
        except Exception as e:
            _log.exception("schwab token exchange failed")
            reply = f"❌ schwab token exchange failed: {type(e).__name__}: {e}"
        finally:
            await self._store.consume_pending_schwab_reauth(conversation.id)

        await self._store.append_message(
            conversation.id,
            "assistant",
            reply,
            provider_route="internal:schwab_reauth",
        )
        await self._safe_send(msg.conversation_key, reply, kind="final")
        await self._store.touch_conversation(conversation.id)
        return True

    async def _exchange_schwab_code(
        self,
        callback_url: str,
        persisted_state: str,
        received_url: str,
    ) -> str:
        """Drive the schwab-py token-exchange path with the persisted
        state spliced back into a freshly-built ``auth_context``.

        This is the trick that makes the two-message handshake work
        across process boundaries: we don't need to pickle the
        ``auth_context`` object — ``get_auth_context`` is deterministic
        modulo the random state token, so we rebuild it from the same
        ``(api_key, callback_url)`` pair and overwrite ``.state`` with
        the value the slash command persisted. authlib's CSRF check
        compares strings, not object identity.
        """
        app_key_secret = self._settings.schwab_app_key
        app_secret_secret = self._settings.schwab_app_secret
        if app_key_secret is None or app_secret_secret is None:
            return (
                "❌ schwab is not configured. Set SCHWAB_APP_KEY and "
                "SCHWAB_APP_SECRET in `.env` and restart Ember."
            )
        api_key = app_key_secret.get_secret_value()
        app_secret = app_secret_secret.get_secret_value()

        # Lazy import — keeps schwab-py optional for installs that don't
        # use the brokerage feature, mirroring the tools module.
        import schwab.auth  # type: ignore

        token_path = self._settings.data_dir / "schwab" / ".schwab_token.json"
        token_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        def _token_write_func(t: Any) -> None:
            # schwab-py's wrapped_token_write_func already wraps the
            # token dict as {creation_timestamp, token}. Don't re-wrap
            # — that bug burned the earlier auth bootstrap driver.
            tmp_path = token_path.with_suffix(token_path.suffix + ".tmp")
            with open(tmp_path, "w") as f:
                json.dump(t, f)
            os.replace(tmp_path, token_path)
            try:
                os.chmod(token_path, 0o600)
            except OSError:
                _log.debug("chmod 0600 on token file failed", exc_info=True)

        def _do_exchange() -> Any:
            ctx = schwab.auth.get_auth_context(api_key, callback_url)
            # Splice the persisted state back in so the CSRF check
            # passes across the two-message boundary. AuthContext is a
            # NamedTuple (immutable) — use `_replace()` to build a copy
            # with the persisted state instead of attribute assignment.
            ctx = ctx._replace(state=persisted_state)
            return schwab.auth.client_from_received_url(
                api_key,
                app_secret,
                ctx,
                received_url,
                _token_write_func,
                asyncio=False,
                enforce_enums=True,
            )

        await asyncio.to_thread(_do_exchange)

        # Compute an approximate refresh-token expiry. Schwab refresh
        # tokens last ~7 days from issuance; without parsing the token
        # file (and we'd rather not log its contents) we surface an
        # approximate date.
        approx = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return f"✓ schwab token refreshed, valid for ~7 days (refreshed {approx} UTC)"

    async def _assemble_system_block(
        self,
        conversation: Conversation,
        *,
        active: Skill | None = None,
    ) -> str:
        """Soul + profile + (active skill block) + (pinned context) + (summaries).

        Sections joined by `IdentityStore.SECTION_SEPARATOR`. Byte-stable
        across consecutive turns when neither the on-disk identity files
        nor the conversation's pins/summaries change (TLC-12).

        Order (architecture doc § Prompt assembly order):
            soul+profile → active skill prompt → pinned context → summaries
        """
        sep = IdentityStore.SECTION_SEPARATOR
        sections: list[str] = []

        base = self._identity.system_prompt() if self._identity is not None else ""
        if base:
            sections.append(base)

        # Conversation timing (TC-1): teach the model how to read the per-turn
        # stamps (`_timestamp_prefix`). Static text → one-time cache
        # invalidation for existing conversations, then byte-stable again,
        # exactly like the lessons.md injection (TLC-12).
        sections.append(_TRANSCRIPT_TIME_NOTE)

        # History-recall nudge (FC-31): static, directly after the timing
        # note so the prefix ordering is fixed.
        sections.append(_HISTORY_RECALL_NOTE)

        # Sandbox facts (what `python3` in Bash can and cannot do): static,
        # directly after the recall note.
        sections.append(_SANDBOX_NOTE)

        # Voice-shaped output for the siri channel (doc #55 step 3): the
        # adapter drops markdown-hostile rendering entirely, so the model
        # must not produce it in the first place.
        if conversation.channel == "siri":
            sections.append(_SIRI_VOICE_NOTE)

        if active is not None:
            skill_prompt = getattr(active, "system_prompt", None)
            if skill_prompt:
                sections.append(skill_prompt)
        elif self._skill_registry is not None:
            # Router mode (no active skill). Append the deterministic
            # `## Available skills` block so the model knows what it can
            # dispatch to (FC-18). Empty registry → empty string → block
            # omitted entirely.
            skills_block = build_skills_block(self._skill_registry)
            if skills_block:
                sections.append(skills_block)
            # Advertise the full bridge tool catalog. The skills block
            # alone left the model denying it had integrations it actually
            # has (mealie, gmail, calendar, …) — it leaned on what it had
            # been told it could do rather than the live tool list.
            if self._tool_registry is not None:
                catalog_block = build_tools_catalog_block(self._tool_registry)
                if catalog_block:
                    sections.append(catalog_block)

        # Pinned context (user-elevated; before summaries).
        pins = await self._store.list_pins(conversation.id)
        if pins:
            pin_lines = "\n".join(f"- {p.content}" for p in pins)
            sections.append(f"## Pinned\n\n{pin_lines}")

        # Summaries of compacted history (chronological by start_message_id).
        summaries = await self._store.list_summaries(conversation.id)
        if summaries:
            summary_blocks = "\n\n".join(s.summary_text for s in summaries)
            # v16.2 framing: the condensed turns are THIS conversation's
            # own history — the earlier header ("Summary of earlier
            # conversation") plus the timestamp-gap note had the model
            # narrating rotated-away content as a "prior thread".
            sections.append(
                "## Earlier in this conversation (condensed)\n\n"
                "Older turns of THIS same conversation were condensed to the brief "
                "below. Treat it as your own earlier context, not a separate chat.\n\n"
                f"{summary_blocks}"
            )

        return sep.join(sections)

    def _resolve_active_skill(self, conversation: Conversation) -> Skill | None:
        """Return the active skill for this conversation, or None.

        TLC-7: an unknown active skill name (e.g. file deleted between
        activation and now) logs a warning and returns None — never
        raises out of the turn.
        """
        name = conversation.active_skill
        if not name or self._skill_registry is None:
            return None
        try:
            return self._skill_registry.get(name)
        except KeyError:
            _log.warning(
                "active skill %r not found in registry; treating as no skill active",
                name,
            )
            return None

    def _build_per_turn_tools(
        self,
        *,
        conversation: Conversation,
        active: Skill | None,
        parent_message_id: int,
    ) -> dict[str, Tool]:
        """Build a per-turn `{name: Tool}` overlay (FC-18 wiring A).

        In router mode (no active skill) AND with a non-empty skill
        registry, instantiate a fresh `UseSkillTool` carrying the
        parent's conversation_id, just-persisted user message id, and
        the parent's `channel_conversation_key`. Each turn gets its own
        instance — the global tool_registry does not carry `use_skill`.
        Also surfaces a small set of direct tools (see
        `_ROUTER_DIRECT_TOOL_NAMES`) so router mode can do simple media
        ops without dispatching into a skill.

        In pinned mode OR when the registry is empty, returns an empty
        overlay so `use_skill` is neither advertised nor dispatchable
        (pinned mode advertises the active skill's allowlist directly).

        Backward-compat: if `use_skill` is already pre-registered in the
        global `tool_registry` (older FC-17 test fixtures), defer to it
        instead of building a fresh per-turn instance — the pre-
        registered tool already carries its own `provider_router`.
        """
        if active is not None:
            return {}
        if self._tool_registry is None:
            return {}
        overlay: dict[str, Tool] = {}
        # Router-direct utilities: present iff registered globally.
        for name in _ROUTER_DIRECT_TOOL_NAMES:
            try:
                overlay[name] = self._tool_registry.get(name)
            except KeyError:
                continue
        # use_skill — only when there are skills to dispatch to.
        if self._skill_registry is None or not self._skill_registry.list():
            return overlay
        # FC-17 fixture compat: if `use_skill` is pre-registered in the
        # global registry, defer to it (skip per-turn instantiation).
        try:
            self._tool_registry.get("use_skill")
            return overlay
        except KeyError:
            pass
        # Local import: avoids a top-level cycle between agent ↔ use_skill.
        from ember.tools.use_skill import UseSkillTool

        use_skill = UseSkillTool(
            skill_registry=self._skill_registry,
            tool_registry=self._tool_registry,
            provider_router=self._router,
            channel=self._channel,
            store=self._store,
            conversation_id=conversation.id,
            parent_message_id=parent_message_id,
            parent_conversation_key=conversation.channel_conversation_key,
        )
        overlay[use_skill.name] = use_skill
        return overlay

    def _tool_specs_for_active_skill(
        self,
        active: Skill | None,
        per_turn_tools: dict[str, Tool] | None = None,
    ) -> list[ToolSpec] | None:
        """Filter the tool registry to the active skill's `tools` list.

        Router mode (active is None): the per-turn overlay carries the
        per-turn `use_skill` instance; advertise its spec when present.
        With an empty overlay AND no global tools registered, returns
        None (preserves pre-FC-17 behavior).

        Pinned mode (active is set): filter the global registry to the
        skill's allowlist. `use_skill` is excluded (TLC-17). Returns an
        empty list when the active skill lists zero tools (strict
        allowlist).
        """
        per_turn_tools = per_turn_tools or {}
        if active is None:
            # FC-18 / TLC-17: router mode advertises every tool in the
            # per-turn overlay — `use_skill` (when skills are registered)
            # plus the router-direct utilities like `generate_image` and
            # `http_request`. Other tools stay scoped to skills.
            specs: list[ToolSpec] = [
                ToolSpec(
                    name=t.name,
                    description=t.description,
                    input_schema=t.input_schema,
                )
                for t in per_turn_tools.values()
            ]
            if specs:
                return specs
            # Empty overlay — fall back to global `use_skill` if a test
            # fixture pre-registered it (FC-17 compat); otherwise return
            # None to preserve pre-FC-18 "no tools" behavior.
            if self._tool_registry is None:
                return None
            try:
                global_use_skill = self._tool_registry.get("use_skill")
            except KeyError:
                return None
            return [
                ToolSpec(
                    name=global_use_skill.name,
                    description=global_use_skill.description,
                    input_schema=global_use_skill.input_schema,
                )
            ]
        if self._tool_registry is None:
            return []
        wanted = set(getattr(active, "tools", ()) or ())
        if not wanted:
            return []
        out: list[ToolSpec] = []
        for tool in self._tool_registry.list_tools():
            if tool.name == "use_skill":
                # TLC-17: pinned mode never advertises use_skill.
                continue
            if tool.name in wanted:
                out.append(
                    ToolSpec(
                        name=tool.name,
                        description=tool.description,
                        input_schema=tool.input_schema,
                    )
                )
        return out

    def _make_stream_handler(
        self,
        conversation_key: str,
        conversation_id: int | None = None,
        *,
        activity: _TurnActivity | None = None,
        stream_blocks: bool = False,
    ) -> _StreamObserver:
        """Build the turn's stream observer: detects memory writes to
        record revert pointers + emit '📝 saved to memory' announcements
        (architecture decision #2, TLC-2), and (v11 / TLC-3) detects
        `confirmation_required` markers in tool_result events, persisting
        `pending_tool_confirmation` and emitting the "🔒 Confirm: ..."
        prompt.

        v16 (TLC-27/28): per-tool `🔧` status messages are gone — tool
        detail now feeds the turn's activity message via `activity`
        (gated there by `settings.show_tool_calls`), and with
        `stream_blocks` the observer posts completed assistant text
        blocks as they stream (FC-33). Memory-write announcements still
        emit (TLC-2 mandate, decision #2).
        """
        observer = _StreamObserver(
            channel=self._channel,
            conversation_key=conversation_key,
            memory_tracker=self._memory_tracker,
            show_tool_calls=getattr(self._settings, "show_tool_calls", True),
            store=self._store,
            conversation_id=conversation_id,
            totp_gated_tools=self._totp_gated_tools(),
            locked_tools=(
                frozenset(getattr(self._settings, "locked_tools", ()) or ())
                if self._unlock_store is not None
                else frozenset()
            ),
            unlock_ttl_label=(
                self._unlock_store.ttl_label() if self._unlock_store is not None else None
            ),
            activity=activity,
            stream_blocks=stream_blocks,
        )
        return observer

    async def _safe_send(self, conversation_key: str, content: str, *, kind) -> None:
        try:
            await self._channel.send(conversation_key, content, kind=kind)
        except Exception:
            _log.exception("failed to send error reply to channel")

    async def _safe_set_typing(self, conversation_key: str, *, on: bool) -> None:
        try:
            await self._channel.set_typing(conversation_key, on=on)
        except Exception:
            _log.debug("set_typing failed", exc_info=True)

    async def _alert_claude_auth_down(self, err: Exception) -> None:
        """Post a one-time 'claude auth is down' alert to the scheduler
        channel with the re-auth command. Debounced via runtime_config so a
        sustained outage doesn't spam; the flag is cleared on the next
        healthy claude_cli turn (`_clear_claude_auth_alert`). Best-effort —
        never raises into the turn."""
        try:
            if await self._store.get_runtime_config(_CLAUDE_AUTH_ALERT_KEY):
                return
            await self._store.set_runtime_config(_CLAUDE_AUTH_ALERT_KEY, "1")
            await self._channel.notify_scheduler(
                "🔐 **Claude auth is down.** Ember is answering on the "
                "`deepseek_api` fallback meanwhile. Re-auth on the server by "
                "copying and running the command in the next message "
                f"(error: {err}):"
            )
            # Send the command as its own bare-text message — no prose, no
            # code fence — so Discord's iOS "Copy Text" yields a clean,
            # paste-ready one-liner (a fenced multi-line block isn't
            # selectable in the mobile app).
            await self._channel.notify_scheduler(_CLAUDE_REAUTH_COMMAND)
        except Exception:
            _log.exception("failed to send claude-auth-down alert")

    async def _clear_claude_auth_alert(self) -> None:
        """Clear the auth-down alert flag after claude auth recovers, so a
        future outage alerts again. Only writes when the flag is actually
        set (keeps the healthy-turn path free of a DB write). Best-effort."""
        try:
            if await self._store.get_runtime_config(_CLAUDE_AUTH_ALERT_KEY):
                await self._store.set_runtime_config(_CLAUDE_AUTH_ALERT_KEY, "")
                _log.info("claude auth recovered; cleared auth-down alert flag")
        except Exception:
            _log.exception("failed to clear claude-auth-down alert flag")


class _StreamObserver:
    """Stream-event handler shared with the provider.

    Watches assistant tool_use blocks → activity-message tool detail
    (v16, TLC-27 — the per-call `🔧` status messages are consolidated
    into the turn's single activity message).
    Watches assistant text blocks → progressive block posts (v16,
    TLC-28 / UoW-32) when `stream_blocks` is set, tracking the posted
    count + native ids the agent's final send builds on.
    Watches user tool_result blocks → memory-write announcements +
    revert-tracker recording, and (v11 / TLC-3) confirmation-required
    markers → persists pending state and emits the user-facing
    "🔒 Confirm: ..." prompt.
    """

    def __init__(
        self,
        *,
        channel: Channel,
        conversation_key: str,
        memory_tracker: MemoryRevertTracker | None,
        show_tool_calls: bool = True,
        store: Store | None = None,
        conversation_id: int | None = None,
        totp_gated_tools: frozenset[str] | None = None,
        activity: _TurnActivity | None = None,
        stream_blocks: bool = False,
        locked_tools: frozenset[str] | None = None,
        unlock_ttl_label: str | None = None,
    ) -> None:
        self._channel = channel
        self._conversation_key = conversation_key
        self._tracker = memory_tracker
        self._show_tool_calls = show_tool_calls
        self._store = store
        self._conversation_id = conversation_id
        self._totp_gated_tools = frozenset(totp_gated_tools or ())
        # Doc #60: tools whose 🔒 prompt opens an unlock window instead of
        # approving one call. Empty when no UnlockStore is wired.
        self._locked_tools = frozenset(locked_tools or ())
        self._unlock_ttl_label = unlock_ttl_label
        self._activity = activity
        self._stream_blocks = stream_blocks
        # UoW-32: blocks successfully posted, in order. `posted_block_count`
        # is the agent's remainder index into `CompletionResult.text_parts`;
        # a single failed post latches `_stream_post_failed` so that block
        # AND every later one ride the final send (exactly-once by count —
        # a gap in the middle would make the count lie).
        self.posted_block_count: int = 0
        self.streamed_message_ids: list[str] = []
        self._stream_post_failed = False
        # tool_use_id → tool_name, for tool_uses we want to follow up on
        # (memory-write announcements).
        self._pending_memory_writes: dict[str, str] = {}
        # tool_use_id → (name, input) for ALL tool_use blocks. Lets the
        # tool_result handler reconstruct the tool call when it sees a
        # confirmation_required marker (TLC-3).
        self._tool_uses: dict[str, tuple[str, dict[str, Any]]] = {}
        # Every confirmation-gated call this turn, in order. The store keeps
        # ONE pending row per conversation, so a second gated call in the
        # same turn used to overwrite the first: the user saw two 🔒
        # prompts, and their single reply silently redeemed only the last.
        self._pending_calls: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "assistant":
            # Sidechain events (subagent streams, truthy parent_tool_use_id)
            # are not reply text — same predicate the claude_cli provider
            # applies when it builds `text_parts`, so counts stay aligned.
            sidechain = bool(event.get("parent_tool_use_id"))
            content = event.get("message", {}).get("content") or []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        await self._on_tool_use(block)
                    elif block.get("type") == "text" and not sidechain:
                        await self._on_text_block(block)
        elif etype == "user":
            content = event.get("message", {}).get("content") or []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        await self._on_tool_result(block)

    async def _on_tool_use(self, block: dict[str, Any]) -> None:
        name = block.get("name") or "?"
        args = block.get("input") or {}
        tu_id = block.get("id")
        # Record every tool_use so the matching tool_result can look up
        # the originating call (TLC-3 confirmation marker → pending state).
        if tu_id:
            self._tool_uses[tu_id] = (
                name,
                args if isinstance(args, dict) else {},
            )

        # v16 (TLC-27): tool detail no longer posts a separate `🔧` status
        # message — it feeds the turn's single activity message, which the
        # manager renders (and gates on `show_tool_calls`) itself. The
        # bridge prefix is stripped for display; args keep the historical
        # sorted-JSON + truncation shape.
        if self._activity is not None:
            display_name = name
            if display_name.startswith(self._BRIDGE_TOOL_PREFIX):
                display_name = display_name[len(self._BRIDGE_TOOL_PREFIX) :]
            try:
                args_str = json.dumps(args, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                args_str = str(args)
            if len(args_str) > _TOOL_STATUS_ARGS_LIMIT:
                args_str = args_str[: _TOOL_STATUS_ARGS_LIMIT - 3] + "..."
            self._activity.note_tool(display_name, args_str)

        if self._is_memory_store(name):
            if tu_id:
                self._pending_memory_writes[tu_id] = name
            text_preview = ""
            if isinstance(args, dict):
                text_preview = str(args.get("text") or args.get("content") or "")[:120]
            await self._safe_status(f"📝 saving to memory: {text_preview!r}")

    async def _on_text_block(self, block: dict[str, Any]) -> None:
        """UoW-32: post one completed assistant text block via the normal
        channel send (existing 2000-char chunking applies channel-side).

        Non-empty blocks only — the claude_cli provider records the same
        blocks under the same predicate into `text_parts`, so the posted
        count is a valid prefix index into it. A failed post latches the
        stream off for the rest of the turn: that block and all later
        ones ride the agent's final send instead (TLC-28 exactly-once).
        """
        if not self._stream_blocks or self._stream_post_failed:
            return
        text = str(block.get("text") or "")
        if not text:
            return
        try:
            ids = await self._channel.send(self._conversation_key, text, kind="final")
        except Exception:
            self._stream_post_failed = True
            _log.warning(
                "streamed block post failed; remaining blocks ride the final send",
                exc_info=True,
            )
            return
        self.posted_block_count += 1
        self.streamed_message_ids.extend(ids)

    async def _on_tool_result(self, block: dict[str, Any]) -> None:
        tu_id = block.get("tool_use_id")
        text = self._extract_result_text(block.get("content"))

        # Parse result content as JSON (best-effort) — used by both the
        # memory-write detector and the confirmation-marker detector.
        parsed: Any = None
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            parsed = None

        # TLC-3: confirmation_required marker. Runs in parallel with the
        # memory-write path — the two handlers don't conflict because a
        # given tool_use is either a memory-store or a confirmation-gated
        # tool, never both. Two recognized shapes:
        # - Legacy / in-process: `{"error": "confirmation_required", ...}`
        #   (registry's native marker, used by tests and direct callers).
        # - Bridge-reshaped: `{"status": "awaiting_user_confirmation", ...}`
        #   The MCP bridge rewrites the registry marker so the model's
        #   tool_result doesn't read as a failure (avoids "Same result"
        #   prose narration). The observer accepts either.
        is_confirmation_marker = isinstance(parsed, dict) and (
            parsed.get("error") == CONFIRMATION_REQUIRED_ERROR
            or parsed.get("status") == "awaiting_user_confirmation"
        )
        if is_confirmation_marker:
            await self._handle_confirmation_marker(tu_id, parsed)

        # Tool-error surfacing. The model has a documented bias toward
        # narrating tool errors as "permission denied" / "blocked" prose
        # even when the underlying error is a real upstream failure
        # (e.g. McpError from mcp_connect, HTTP 5xx from a REST tool).
        # Emit the raw error verbatim to the channel as a status so the
        # user sees the truth regardless of what the model writes next.
        # Skip the confirmation marker (it's already announced via 🔒).
        #
        # Exception: claude CLI's client-side "No such tool available"
        # rejection. The bridge tool genuinely exists (and is callable —
        # `bypassPermissions` means `--allowed-tools` doesn't gate); this
        # is a transient tool-resolution race where the model emits a
        # tool_use before the MCP `agent` server's tools are registered
        # for the turn. The call never reaches the bridge, the model
        # immediately retries (same or alternate tool) and succeeds. It's
        # self-correcting noise, not a failure the model misreports — and
        # surfacing it as ❌ wrongly implies the fetch failed. Swallow it.
        if (
            not is_confirmation_marker
            and self._tool_result_is_error(block, parsed)
            and not self._is_transient_tool_resolution_error(text)
        ):
            await self._announce_tool_error(tu_id, parsed, text)

        # Memory-write announcement (existing behavior).
        if not tu_id or tu_id not in self._pending_memory_writes:
            return
        self._pending_memory_writes.pop(tu_id, None)
        if self._tracker is None:
            return
        if not isinstance(parsed, dict):
            return
        memory_id = parsed.get("id") or parsed.get("memory_id")
        if memory_id is None:
            return
        try:
            self._tracker.record(int(memory_id), self._conversation_key)
        except (TypeError, ValueError):
            return

    # Bridge prefix that claude_cli uses for our in-process tools — see
    # bridge/server.py (FastMCP server `name="agent"`). The observer
    # records tool_use blocks by their claude-side name, but the redeem
    # path (registry.call_tool, _handle_tool_confirmation) looks tools up
    # by their bare registry name. Strip the prefix when persisting.
    _BRIDGE_TOOL_PREFIX = "mcp__agent__"

    async def _handle_confirmation_marker(
        self,
        tu_id: str | None,
        parsed: dict[str, Any],
    ) -> None:
        """Persist pending tool confirmation + announce the prompt.

        Silently no-ops if store / conversation_id wasn't wired (some
        tests construct the observer without those). The action string
        comes from the marker; tool_name + args come from the recorded
        tool_use block (looked up by tu_id).
        """
        action = parsed.get("action") or ""
        tool_name: str | None = None
        tool_args: dict[str, Any] = {}
        if tu_id is not None:
            recorded = self._tool_uses.get(tu_id)
            if recorded is not None:
                tool_name, tool_args = recorded

        # claude_cli reports the tool by its MCP-namespaced name
        # (e.g. `mcp__agent__profile_update`), but the registry stores it
        # under the bare name. Strip so the redeem-side lookup succeeds.
        if tool_name and tool_name.startswith(self._BRIDGE_TOOL_PREFIX):
            tool_name = tool_name[len(self._BRIDGE_TOOL_PREFIX) :]

        if self._store is not None and self._conversation_id is not None and tool_name:
            self._pending_calls.append(
                {"tool_name": tool_name, "args": tool_args, "action": action}
            )
            first = self._pending_calls[0]
            unlock = self._is_unlock_prompt(tool_name, parsed)
            # Top-level fields describe the first call (the shape every
            # reader before `calls` existed understands); `calls` carries
            # all of them, first included.
            payload = json.dumps(
                {
                    "tool_name": first["tool_name"],
                    "args": first["args"],
                    "action": first["action"],
                    "set_at_iso": datetime.now(timezone.utc).isoformat(),
                    "calls": list(self._pending_calls),
                    "mode": "unlock" if unlock else "confirm",
                }
            )
            try:
                await self._store.set_pending_tool_confirmation(self._conversation_id, payload)
            except Exception:
                _log.exception("failed to persist pending_tool_confirmation")

        if self._is_unlock_prompt(tool_name, parsed):
            if len(self._pending_calls) > 1:
                # One window covers every locked call of the turn; the
                # first prompt already asked for the code.
                return
            await self._safe_status(
                self._format_confirm_prompt(
                    action, needs_code=True, unlock_ttl=self._unlock_ttl_label or "a while"
                )
            )
            return

        prompt = self._format_confirm_prompt(action, needs_code=tool_name in self._totp_gated_tools)
        if len(self._pending_calls) > 1:
            prompt += f"\n(one reply decides all {len(self._pending_calls)} pending actions)"
        await self._safe_status(prompt)

    def _is_unlock_prompt(self, tool_name: str | None, parsed: dict[str, Any]) -> bool:
        """A locked-set tool's marker opens a window rather than approving
        one call — only when an UnlockStore is wired (locked set non-empty)."""
        if not self._locked_tools:
            return False
        return bool(parsed.get("unlock")) or (tool_name in self._locked_tools)

    @staticmethod
    def _format_confirm_prompt(
        action: str, *, needs_code: bool = False, unlock_ttl: str | None = None
    ) -> str:
        """Render the 🔒 confirm prompt. For multi-line actions, wrap the
        body in a code fence so it renders verbatim (no markdown
        re-interpretation of the text being approved) and the "Reply
        yes/no" prompt sits on its own line. A unified diff (profile_update)
        gets a ```diff fence for +/- coloring; anything else — an email
        body, a JSON args block — gets a plain fence, since ```diff would
        paint every "- bullet" line red. Single-line actions stay inline.

        The body is never truncated here: the channel's fence-aware
        chunker splits long prompts across messages, re-closing and
        re-opening the fence at each boundary.

        `needs_code` swaps in the second-factor instruction for TOTP-gated
        tools. The code itself is never echoed — the whole point is that it
        exists only on the user's other device."""
        action = action.rstrip()
        if unlock_ttl is not None:
            instruction = (
                f"Reply 'y <code>' with your authenticator code to unlock this for "
                f"{unlock_ttl} in this thread, or 'no'."
            )
        elif needs_code:
            instruction = "Reply 'y <code>' with your authenticator code, or 'no'."
        else:
            instruction = "Reply 'yes' or 'no'."
        if "\n" not in action:
            return f"🔒 Confirm: {action}. {instruction}"
        # Split off the first line as a header (e.g. "profile_update:")
        # and put the rest in a fenced block.
        head, _, body = action.partition("\n")
        lang = "diff" if _looks_like_unified_diff(body) else ""
        return f"🔒 Confirm: {head}\n```{lang}\n{body}\n```\n{instruction}"

    @staticmethod
    def _is_transient_tool_resolution_error(raw_text: str) -> bool:
        """True for claude CLI's client-side "No such tool available"
        rejection — a turn-start race where the MCP tool isn't registered
        yet, which the model self-corrects on retry. Not a real failure,
        so it shouldn't reach the user as ❌."""
        return "no such tool available" in (raw_text or "").lower()

    @staticmethod
    def _tool_result_is_error(block: dict[str, Any], parsed: Any) -> bool:
        """A tool_result is an error if the API-level `is_error` flag is
        set, OR the body parses as a dict whose `error` key is truthy
        (the in-process registry's convention)."""
        if block.get("is_error") is True:
            return True
        if isinstance(parsed, dict) and parsed.get("error"):
            return True
        return False

    async def _announce_tool_error(
        self,
        tu_id: str | None,
        parsed: Any,
        raw_text: str,
    ) -> None:
        """Surface a tool error as a visible status so the user sees the
        real failure even when the model misreports it."""
        tool_name = "?"
        if tu_id is not None:
            recorded = self._tool_uses.get(tu_id)
            if recorded is not None:
                tool_name = recorded[0] or "?"
        # Prefer the structured `message` (registry convention), fall
        # back to `error`, then to the raw text.
        msg = ""
        if isinstance(parsed, dict):
            msg = str(parsed.get("message") or parsed.get("error") or "")
        if not msg:
            msg = raw_text or ""
        msg = msg.strip()
        if len(msg) > 400:
            msg = msg[:397] + "..."
        # Backtick-wrap the tool name: bridge tools are namespaced
        # `mcp__agent__<tool>`, and Discord markdown reads the `__` pairs
        # as underline markers — rendering `mcp__agent__http_request` as
        # "mcpagenthttp_request". Inline code disables that.
        await self._safe_status(f"❌ `{tool_name}`: {msg}")

    @staticmethod
    def _is_memory_store(name: str | None) -> bool:
        if not name:
            return False
        # Match agent__memory__store_memory or mcp__agent__memory__store_memory
        return "memory__store_memory" in name

    @staticmethod
    def _extract_result_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(str(c.get("text") or ""))
                elif isinstance(c, str):
                    parts.append(c)
            return "".join(parts)
        return ""

    async def _safe_status(self, content: str) -> None:
        try:
            await self._channel.send(self._conversation_key, content, kind="status")
        except Exception:
            _log.debug("status emit failed", exc_info=True)


def _looks_like_unified_diff(body: str) -> bool:
    """True when `body` carries a unified-diff header (`--- a` / `+++ b`),
    i.e. it is a real diff and deserves Discord's ```diff coloring."""
    lines = body.split("\n", 4)
    return any(ln.startswith("--- ") for ln in lines) and any(ln.startswith("+++ ") for ln in lines)


def _format_elapsed(seconds: float) -> str:
    """Elapsed turn time for the activity message: "20s", "2m 40s",
    "1h 5m". Whole units — sub-second precision is noise here."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


class _TurnActivity:
    """Turn-scoped liveness driver (TLC-27 / UoW-31).

    One background task per Discord turn: triggers the native typing
    indicator every `TYPING_REFRESH_S`, posts the single activity status
    message once the turn passes `ACTIVITY_DELAY_S` (short turns show
    typing but never an activity message), then edits it in place — on
    observer events (`note_tool`) and on `ACTIVITY_EDIT_INTERVAL_S`
    ticks so the elapsed counter keeps moving. `finish()` runs under the
    turn's `finally` on every terminal path and deletes the message
    (retrying a failed delete once) — zero liveness residue.

    Every channel call is try/except-wrapped: liveness I/O is
    best-effort by contract and must never affect the turn. The FC-32
    timing constants are read off this module's globals at call time,
    so tests can compress the timeline by patching them.
    """

    def __init__(
        self,
        *,
        channel: Channel,
        conversation_key: str,
        show_tool_calls: bool,
    ) -> None:
        self._channel = channel
        self._key = conversation_key
        self._show_tool_calls = show_tool_calls
        self._task: asyncio.Task | None = None
        self._status_id: str | None = None
        self._tool_calls = 0
        self._last_tool: tuple[str, str] | None = None
        # `note_tool` sets both: the event wakes the loop promptly, the
        # flag records that an edit is owed (UoW-31 step 3: "on each
        # subsequent observer event or 5s tick"). The event is cleared
        # every wake; the flag only when an edit consumes it.
        self._wake = asyncio.Event()
        self._dirty = False
        self._started_at = time.monotonic()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="turn-activity")

    def note_tool(self, name: str, args_str: str) -> None:
        """Record a tool call for the activity message. Sync + non-raising:
        called from the stream observer's hot path."""
        self._tool_calls += 1
        self._last_tool = (name, args_str)
        self._dirty = True
        self._wake.set()

    async def finish(self) -> None:
        """Terminal-path teardown (UoW-31 step 4): cancel the keepalive,
        delete the activity message. Idempotent; never raises out —
        raising here would mask the turn's real outcome."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                _log.debug("turn-activity task ended noisily", exc_info=True)
        status_id, self._status_id = self._status_id, None
        if status_id is None:
            return
        # Shielded so a turn cancelled mid-teardown (interrupt gate) still
        # completes the delete in the background — the finally that called
        # us may itself be unwinding a CancelledError.
        delete = asyncio.ensure_future(self._delete_with_retry(status_id))
        try:
            await asyncio.shield(delete)
        except asyncio.CancelledError:
            raise  # `delete` keeps running; it swallows its own failures
        except Exception:
            _log.debug("activity delete raised", exc_info=True)

    # --- Internals --------------------------------------------------------

    async def _run(self) -> None:
        next_typing = 0.0
        # Backoff gate for post attempts: a failed (or None-returning)
        # post retries on the edit cadence, never in a tight loop — an
        # unresolvable thread must not turn into API-call spam.
        post_not_before = 0.0
        last_edit: float | None = None
        while True:
            now = time.monotonic() - self._started_at
            # Typing keepalive: first trigger fires immediately, so the
            # indicator shows within ~2s of turn start (TLC-27).
            if now >= next_typing:
                try:
                    await self._channel.typing_started(self._key)
                except Exception:
                    _log.debug("typing_started failed", exc_info=True)
                next_typing = now + TYPING_REFRESH_S
            if self._status_id is None and now >= ACTIVITY_DELAY_S and now >= post_not_before:
                try:
                    self._status_id = await self._channel.post_status(self._key, self._render(now))
                except Exception:
                    _log.debug("post_status failed", exc_info=True)
                if self._status_id is not None:
                    last_edit = now
                else:
                    # No id (surface has no status affordance, or the
                    # post failed) → retry after a backoff.
                    post_not_before = now + ACTIVITY_EDIT_INTERVAL_S
            elif self._status_id is not None and last_edit is not None:
                # Edit on an observer event (with a small floor so a tool
                # burst doesn't hammer Discord's edit rate limit) or on
                # the regular tick.
                due = (self._dirty and now - last_edit >= _ACTIVITY_MIN_EDIT_GAP_S) or (
                    now - last_edit >= ACTIVITY_EDIT_INTERVAL_S
                )
                if due:
                    try:
                        await self._channel.edit_status(
                            self._key, self._status_id, self._render(now)
                        )
                    except Exception:
                        _log.debug("edit_status failed", exc_info=True)
                    last_edit = now
                    self._dirty = False
            # Sleep until the earliest upcoming deadline, waking early on
            # observer events.
            deadlines = [next_typing]
            if self._status_id is None:
                deadlines.append(max(ACTIVITY_DELAY_S, post_not_before))
            elif last_edit is not None:
                deadlines.append(last_edit + ACTIVITY_EDIT_INTERVAL_S)
                if self._dirty:
                    deadlines.append(last_edit + _ACTIVITY_MIN_EDIT_GAP_S)
            now = time.monotonic() - self._started_at
            timeout = max(0.05, min(deadlines) - now)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except (TimeoutError, asyncio.TimeoutError):
                pass

    def _render(self, elapsed_s: float) -> str:
        """`⏳ working — 2m 40s · 6 tool calls · web_fetch({...})`.

        Tool detail (count + last tool with truncated args) appears only
        under `show_tool_calls` — TLC-27: the flag now governs tool
        detail inside the activity message, not separate messages."""
        text = f"⏳ working — {_format_elapsed(elapsed_s)}"
        if self._show_tool_calls and self._tool_calls:
            plural = "" if self._tool_calls == 1 else "s"
            text += f" · {self._tool_calls} tool call{plural}"
            if self._last_tool is not None:
                name, args_str = self._last_tool
                text += f" · {name}({args_str})"
        return text

    async def _delete_with_retry(self, status_id: str) -> None:
        """UoW-31 failure mode: a failed delete is retried once, then
        logged (debug — best-effort machinery, doc #57)."""
        for attempt in (1, 2):
            try:
                await self._channel.delete_status(self._key, status_id)
                return
            except Exception:
                if attempt == 1:
                    _log.debug("delete_status failed; retrying once", exc_info=True)
                    await asyncio.sleep(0.5)
                else:
                    _log.debug("delete_status retry failed; giving up", exc_info=True)
