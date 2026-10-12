"""Ember entrypoint."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal

from ember.agent import Agent
from ember.bridge.server import Bridge
from ember.channels.base import Channel
from ember.channels.discord import DiscordChannel
from ember.channels.gate import TurnGate
from ember.channels.mux import ChannelMux
from ember.channels.siri import SiriChannel
from ember.commands.clear import ClearCommand
from ember.commands.compact import CompactCommand, SummarizeCommand
from ember.commands.context import ContextCommand
from ember.commands.eval import EvalCommand
from ember.evals.service import EvalService
from ember.commands.help import HelpCommand
from ember.commands.memory import MemoryRevertCommand, MemoryRevertTracker
from ember.commands.model import ModelCommand
from ember.commands.pin import PinCommand
from ember.commands.proposal import ProposalCommand
from ember.commands.lock import LockCommand
from ember.commands.registry import CommandRegistry
from ember.commands.run import RunCommand
from ember.commands.runs import RunsCommand
from ember.commands.schedule import ScheduleCommand
from ember.commands.schwab_reauth import SchwabReauthCommand
from ember.commands.skill import SkillCommand
from ember.commands.trim import TrimCommand
from ember.context.rotation import SessionRotator
from ember.config import Settings, load_settings
from ember.errors import SkillLoadError
from ember.identity.lessons import LessonsWriter
from ember.identity.proposer import Proposer
from ember.identity.store import IdentityStore
from ember.identity.writer import ProfileWriter
from ember.logging import configure_logging
from ember.providers.anthropic_api import AnthropicApiProvider
from ember.providers.deepseek_api import DeepseekApiProvider
from ember.providers.base import Provider
from ember.providers.claude_cli import ClaudeCliProvider
from ember.providers.routing import ProviderRouter
from ember.scheduler.notifier import ChannelRunNotifier
from ember.scheduler.recorder import StoreRunRecorder
from ember.scheduler.service import EmberScheduler
from ember.skills.loader import DirectorySkillLoader
from ember.skills.registry import InMemorySkillRegistry
from ember.store.repo import Store
from ember.tools.display import DisplayPushTool
from ember.tools.documents import register_documents_tools
from ember.tools.echo import EchoTool
from ember.tools.gemini_image import GenerateImageTool
from ember.tools.gmail import (
    GmailDraftTool,
    GmailListTool,
    GmailReadTool,
    GmailSearchTool,
    GmailSendTool,
)
from ember.records.indexer import DocumentIndexer
from ember.records.paperless import PaperlessClient
from ember.records.store import RecordsStore
from ember.tools.docs import DocsFetchTool, DocsFileTool, DocsSearchTool, DocsSyncTool
from ember.tools.history import HistoryFetchTool, HistorySearchTool
from ember.tools.schwab import (
    SchwabCancelOrderTool,
    SchwabHoldingsTool,
    SchwabOpenOrdersTool,
    SchwabPlaceOrdersTool,
    SchwabQuoteTool,
    SchwabTransactionsTool,
)
from ember.tools.tiller import TillerQueryTool
from ember.tools.icloud_calendar import (
    CalendarAddEventTool,
    CalendarListCalendarsTool,
    CalendarListEventsTool,
)
from ember.tools.icloud_reminders import (
    RemindersAddTool,
    RemindersCompleteTool,
    RemindersListTool,
)
from ember.tools.import_skill import ImportSkillTool
from ember.tools.mcp_client import McpOutboundClient
from ember.tools.mcp_connector import (
    McpConnectTool,
    McpDisconnectTool,
    McpListConnectedTool,
    live_clients as mcp_connector_live_clients,
)
from ember.tools.skill_read import SkillReadTool
from ember.tools.mealie import (
    MealieAddRecipeTool,
    MealieCreateRecipeTool,
    MealieGetRecipeTool,
    MealieRandomTool,
    MealieSearchTool,
    MealieUpdateRecipeTool,
)
from ember.tools.memory import register_memory_tools
from ember.tools.payload_store import PayloadStore
from ember.tools.profile import ProfileReadTool, ProfileUpdateTool
from ember.tools.read_payload import ReadPayloadTool
from ember.tools.send_file import SendFileTool
from ember.tools.soul import SoulReadTool
from ember.tools.registry import Registry
from ember.tools.skill_author import (
    SkillDraftPythonTool,
    SkillListKnownToolsTool,
    SkillSaveManifestTool,
)
from ember.tools.vault import VaultGetTool, VaultListTool
from ember.unlock import UnlockStore
from ember.bridge.active import active_conversation_key
from ember.tools.web import (
    HttpRequestTool,
    RssFetchTool,
    WebBrowseTool,
    WebFetchTool,
    WebSearchTool,
)

_log = logging.getLogger("ember.main")

# Built-in tools we keep disabled on the claude_cli provider. v14: we
# re-enable the filesystem/search built-ins (Read/Bash/Grep/Glob/Edit/
# Write/NotebookEdit) so the model can read user-uploaded files saved
# under `data_dir/uploads/` and explore them when needed (BC-6 / BC-8).
# WebFetch/WebSearch/Task stay off — duplicates of our MCP-side tools
# that would fragment behavior. The remainder belong to other agents
# (Claude Code itself) and don't fit Ember's surface area.
_DEFAULT_DISALLOWED_BUILTIN_TOOLS = [
    "WebFetch",
    "WebSearch",
    "Task",
    "ToolSearch",
    "Skill",
    "TodoWrite",
    "BashOutput",
    "KillShell",
    "ScheduleWakeup",
    "AskUserQuestion",
    "ExitPlanMode",
    "EnterPlanMode",
    "EnterWorktree",
    "ExitWorktree",
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
    "Monitor",
    "RemoteTrigger",
    "PushNotification",
    "CronCreate",
    "CronDelete",
    "CronList",
    # Added for claude CLI 2.1.x: orchestration/task built-ins that leak
    # into the model's tool list (probe showed 78 tools with these 6
    # stray). They make no sense inside Ember's subprocess and are a
    # footgun (the model could spawn workflows / task records).
    "Workflow",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskUpdate",
]

# claude built-ins we re-enable for the claude_cli provider so the
# model can interact with user-uploaded files saved under
# `data_dir/uploads/` (BC-6 / BC-8). These names must also be added to
# the `--allowed-tools` allowlist (alongside `mcp__agent__*`); dropping
# them from `--disallowed-tools` alone is not sufficient because
# claude CLI gates on the intersection of allowlist + denylist.
_DEFAULT_EXTRA_ALLOWED_BUILTIN_TOOLS = [
    "Read",
    "Bash",
    "Grep",
    "Glob",
    "Edit",
    "Write",
    "NotebookEdit",
]


async def _amain(settings: Settings) -> None:
    store = Store(settings.conversations_db_path)
    await store.open()

    # doc #59: the records data plane (documents index) is a separate
    # SQLite file with its own ladder, opened only when paperless is
    # configured — no token, no records.db, no docs tools.
    records_store: RecordsStore | None = None
    if settings.paperless_api_token is not None:
        records_store = RecordsStore(settings.records_db_path)
        await records_store.open()

    # Identity (soul + profile) — read-only in v1.
    identity = IdentityStore(settings.identity_dir)
    await identity.ensure_initialized()

    # Tool registry — wire echo + (best-effort) memory/documents/vault.
    tool_registry = Registry()
    # Doc #60 unlock windows: locked-set tools run without a per-call prompt
    # while the conversation driving the turn holds a live window.
    unlock_store = UnlockStore(settings.unlock_ttl_seconds)
    tool_registry.configure_unlock(
        settings.locked_tools,
        lambda: unlock_store.is_unlocked(active_conversation_key()),
    )
    tool_registry.register(EchoTool())

    # send_file — attach any file under data_dir to the conversation.
    # Registered unconditionally (no config/creds); rides the same
    # `metadata["attachments"]` path generate_image already uses.
    tool_registry.register(SendFileTool(data_dir=settings.data_dir))

    # v=14: history_search + history_fetch (UoW-27 / FC-27) — always
    # registered, read-only, cross-conversation search over the archive.
    # Constructed with the process Store (mcp_connector precedent).
    tool_registry.register(HistorySearchTool(store=store))
    tool_registry.register(HistoryFetchTool(store=store))
    _log.info("history_search + history_fetch tools registered")

    mcp_clients: list[McpOutboundClient] = []
    await _maybe_register_mcp_outbound(
        tool_registry, mcp_clients, settings.memory_mcp_url, "memory", register_memory_tools
    )
    await _maybe_register_mcp_outbound(
        tool_registry,
        mcp_clients,
        settings.documents_mcp_url,
        "documents",
        register_documents_tools,
    )

    # v=7: replay runtime-mounted outbound MCPs (mcp_connector). The live
    # client map is shared with the connector tools so disconnect /
    # list see the boot-replayed connections.
    await _replay_connected_mcps(tool_registry, store)

    # mcp_connector tools — registered once, regardless of replay
    # outcome. Even with zero replayed connections, the model still
    # needs `mcp_connect` to mount new ones at runtime.
    tool_registry.register(
        McpConnectTool(
            registry=tool_registry,
            store=store,
            allowed_hosts=settings.mcp_connect_allowed_hosts,
            log_path=settings.data_dir / "mcp_connect_log.jsonl",
        )
    )
    tool_registry.register(McpDisconnectTool(registry=tool_registry, store=store))
    tool_registry.register(McpListConnectedTool(registry=tool_registry, store=store))
    _log.info(
        "mcp_connector tools registered (allowlist=%d hosts, audit=%s)",
        len(settings.mcp_connect_allowed_hosts),
        settings.data_dir / "mcp_connect_log.jsonl",
    )

    _maybe_register_vault_tools(tool_registry, settings)
    _maybe_register_mealie_tools(tool_registry, settings)
    _maybe_register_gmail_tools(tool_registry, settings)
    _maybe_register_tiller_query_tool(tool_registry, settings)
    _maybe_register_schwab_tools(tool_registry, settings)
    _maybe_register_icloud_tools(tool_registry, settings)
    _maybe_register_web_tools(tool_registry, settings)
    _maybe_register_browser_tool(tool_registry, settings)
    _maybe_register_gemini_image(tool_registry, settings)
    _maybe_register_display_tool(tool_registry, settings)
    _maybe_register_docs_tools(tool_registry, settings, records_store)

    # v7: skill_author meta-skill tools — registered here so the bridge
    # exposes them and so /skill-author can call them via the agent loop.
    tool_registry.register(
        SkillSaveManifestTool(registry=tool_registry, skills_dir=settings.skills_dir)
    )
    tool_registry.register(
        SkillDraftPythonTool(registry=tool_registry, skills_dir=settings.skills_dir)
    )
    tool_registry.register(SkillListKnownToolsTool(registry=tool_registry))

    # Profile-update tool — wraps ProfileWriter for direct user-driven
    # modifications (distinct from the auto-proposer's classifier-gated
    # flow). Constructed here so the bridge enumerates it on start.
    profile_writer = ProfileWriter(identity=identity)
    tool_registry.register(ProfileReadTool(identity=identity))
    tool_registry.register(ProfileUpdateTool(writer=profile_writer, identity=identity))
    _log.info("profile_read + profile_update tools registered")

    # soul_read only. Per FC-8, soul.md is read-only to the agent — manual
    # edits via sudo on the host. The earlier soul_update tool was tried
    # and reverted: claude consistently asks-permission-first instead of
    # calling the tool, breaking the TLC-3 gate contract; the friction
    # of sudo-editing soul.md once a month is worth the explicit
    # human-in-the-loop boundary on agent self-modification.
    tool_registry.register(SoulReadTool(identity=identity))
    _log.info("soul_read tool registered (soul is human-edit-only per FC-8)")

    # Skill registry created early so skill_read can be registered before
    # the bridge snapshots the tool list (Bridge._register_tools runs in
    # __init__). The registry holds skills by reference, so skills loaded
    # later still surface via skill_read at call time.
    skill_registry = InMemorySkillRegistry()
    tool_registry.register(SkillReadTool(skill_registry=skill_registry))
    _log.info("skill_read tool registered")

    # v14: skills loader constructed here (ahead of the bridge snapshot)
    # so `import_skill` can register pre-bridge with a wired loader. Skills
    # are still loaded later (after the channel/scheduler are built) — this
    # is just construction.
    skill_loader = DirectorySkillLoader(
        directory=settings.skills_dir,
        registry=skill_registry,
        debounce_s=settings.skills_watch_debounce_ms / 1000.0,
    )
    settings.skills_dir.mkdir(parents=True, exist_ok=True)
    # `import_skill` installs executable Python into the hot-reloaded skills
    # directory, which turns "can talk to Ember" into "can run code in the
    # container" — and from there into every credential in the environment.
    # The in-band y/n gate does not stand between those two, because the
    # same conversation that proposes the install also supplies the `y`.
    # Default off; flipping it on is a host-side action (edit .env, redeploy)
    # and the tool is simply absent from the registry otherwise.
    if settings.allow_skill_import:
        tool_registry.register(
            ImportSkillTool(
                loader=skill_loader,
                data_dir=settings.data_dir,
                skills_dir=settings.skills_dir,
            )
        )
        _log.warning(
            "import_skill tool registered (ALLOW_SKILL_IMPORT=1) — in-band "
            "skill installation is enabled"
        )
    else:
        _log.info("import_skill tool NOT registered (ALLOW_SKILL_IMPORT is off)")

    # v15 (FC-30 / TLC-26): payload store — oversize bridge tool results
    # become content-addressed references under <data_dir>/payloads,
    # retrievable via read_payload. Registered before the bridge so the
    # boot-time tool snapshot includes it.
    payload_store = PayloadStore(
        settings.data_dir / "payloads",
        threshold_bytes=settings.payload_store_threshold_bytes,
    )
    tool_registry.register(ReadPayloadTool(payload_store=payload_store))
    _log.info(
        "payload store at %s (threshold=%d bytes); read_payload registered",
        settings.data_dir / "payloads",
        settings.payload_store_threshold_bytes,
    )

    # MCP bridge — exposes the entire registry to claude.
    bridge = Bridge(
        tool_registry,
        host=settings.bridge_host,
        port=settings.bridge_port,
        payload_store=payload_store,
    )
    await bridge.start()
    _write_mcp_config(settings, bridge)

    # Build available providers based on env. claude_cli always (subscription
    # auth via ~/.claude/, set up by `claude login` once per container);
    # anthropic_api only if EMBER_ANTHROPIC_API_KEY is supplied (so v0/v0.5
    # deployments without the key still boot cleanly).
    providers: dict[str, Provider] = {}
    providers["claude_cli"] = ClaudeCliProvider(
        binary=settings.claude_binary_path,
        default_model=settings.claude_default_model,
        mcp_config_paths=[str(settings.bridge_config_path)],
        allowed_tools="mcp__agent__*",
        disallowed_tools=_DEFAULT_DISALLOWED_BUILTIN_TOOLS,
        extra_allowed_tools=_DEFAULT_EXTRA_ALLOWED_BUILTIN_TOOLS,
        session_jsonl_dir=settings.claude_session_jsonl_dir,
        # v15 (FC-31): pruner gated off by default — deprecated-pending-
        # removal now that the payload store bounds tool output at the
        # source; TOOL_PRUNER_ENABLED=1 is the escape valve.
        tool_pruner_enabled=settings.tool_pruner_enabled,
    )
    _log.info(
        "provider claude_cli: subscription auth via %s (model: %s)",
        settings.claude_auth_dir,
        settings.claude_default_model,
    )
    # Cheap-tier sibling: same subscription auth, deliberately TOOLLESS.
    #
    # Cheap-tier callers (session rotator, tone judge, retrospective extraction)
    # invoke `complete(..., tools=None)`, and `tools=None` means "fall back to
    # the constructor's tool config" — on the main claude_cli instance that is
    # the whole `mcp__agent__*` surface plus the filesystem built-ins, under
    # `--permission-mode bypassPermissions`. Pointing the cheap tier at that
    # instance would give a *summarizer* live gmail_send / Schwab / Bash
    # access. Passing `tools=[]` is not enough either: the empty-list branch
    # still emits `_extra_allowed_tools` (Read/Bash/Write/...) in the
    # allowlist.
    #
    # So: no mcp_config_paths, no allowed_tools, no extra_allowed_tools. That
    # takes `_build_args`' final else-branch and emits `--tools ""` — the v0
    # blanket disable. Text in, text out, nothing executable.
    providers["claude_cli_cheap"] = ClaudeCliProvider(
        binary=settings.claude_binary_path,
        default_model=settings.claude_cheap_model,
    )
    _log.info(
        "provider claude_cli_cheap: subscription auth, tools disabled (model: %s)",
        settings.claude_cheap_model,
    )
    if settings.ember_anthropic_api_key is not None:
        providers["anthropic_api"] = AnthropicApiProvider(
            api_key=settings.ember_anthropic_api_key.get_secret_value(),
            default_model=settings.anthropic_default_model,
        )
        _log.info(
            "provider anthropic_api: enabled (model: %s, key: EMBER_ANTHROPIC_API_KEY)",
            settings.anthropic_default_model,
        )
    else:
        _log.info("provider anthropic_api: disabled (EMBER_ANTHROPIC_API_KEY not set)")

    if settings.deepseek_api_key is not None:
        providers["deepseek_api"] = DeepseekApiProvider(
            api_key=settings.deepseek_api_key.get_secret_value(),
            base_url=settings.deepseek_base_url,
            default_model=settings.deepseek_default_model,
        )
        _log.info(
            "provider deepseek_api: enabled (model: %s, base: %s)",
            settings.deepseek_default_model,
            settings.deepseek_base_url,
        )
    else:
        _log.info("provider deepseek_api: disabled (DEEPSEEK_API_KEY not set)")

    # v9: cheap-tier routing for compaction / profile extraction.
    # Boot precedence: runtime_config row (on-the-fly override) > settings
    # > default. If the configured cheap-tier provider isn't registered
    # (e.g. EMBER_ANTHROPIC_API_KEY missing), log + disable rather than fail boot.
    cheap_route: str | None = await store.get_runtime_config("cheap_tier_route")
    if cheap_route is None:
        cheap_route = settings.cheap_tier_route or None
    if cheap_route:
        provider_name = cheap_route.split(":", 1)[0].strip()
        if provider_name not in providers:
            _log.warning(
                "cheap_tier_route %r references unregistered provider %r; disabling cheap-tier",
                cheap_route,
                provider_name,
            )
            cheap_route = None

    router = ProviderRouter(providers, cheap_tier_route=cheap_route)
    _log.info("cheap_tier: %s", cheap_route or "disabled")

    discord_channel = DiscordChannel(
        settings,
        command_registry=None,  # set below after command_registry is built
        store=store,
        router=router,
    )
    # Siri voice channel (doc #55 step 4): HTTP ingress for Apple
    # Shortcuts over the tailnet. Async-fallback replies and undeliverable
    # attachments go to the Discord scheduler channel (decision #3), so
    # the fallback is the discord adapter's notify_scheduler directly.
    adapters: dict[str, Channel] = {"discord": discord_channel}
    if settings.siri_enabled:
        adapters["siri"] = SiriChannel(
            token=settings.siri_api_token.get_secret_value(),
            store=store,
            fallback_notify=discord_channel.notify_scheduler,
            deadline_s=settings.siri_deadline_s,
            session_window_s=settings.siri_session_window_s,
            bind=settings.siri_bind,
            port=settings.siri_port,
            user_id=settings.siri_user_id,
        )
        _log.info(
            "siri channel enabled on %s:%d (deadline %.0fs, window %ds)",
            settings.siri_bind,
            settings.siri_port,
            settings.siri_deadline_s,
            settings.siri_session_window_s,
        )
    else:
        _log.info("siri channel: disabled")

    # Everything downstream (agent, scheduler, notifiers) talks to the mux,
    # never a concrete adapter — the agent loop stays single-channel
    # (TLC-14) while outbound calls route by conversation-key namespace
    # (SIRI-TLC-3). Discord's bare numeric thread IDs are the un-namespaced
    # default; "siri:" keys route to the siri adapter when enabled.
    channel: Channel = ChannelMux(adapters=adapters, default="discord")

    # Schwab write alerts. The tools are registered before the channel
    # exists, so the notifier is attached here rather than at construction.
    # Alerts go to the SCHEDULER channel, not the conversation the order was
    # placed from — an order placed without the user's knowledge should
    # surface somewhere other than the thread that placed it.
    for _write_tool in ("schwab_place_orders", "schwab_cancel_order"):
        if tool_registry.has(_write_tool):
            tool_registry.get(_write_tool).notifier = channel.notify_scheduler  # type: ignore[attr-defined]
            _log.info("schwab write alerts wired for %s", _write_tool)

    # v6: skills loader was constructed pre-bridge (so import_skill could
    # be registered with a live loader). Populate the registry now —
    # skills load before the scheduler boots so refresh() sees the
    # populated source; disabled-state restored from `scheduled_jobs_state`
    # after start.
    await _load_skills_best_effort(skill_loader)

    recorder = StoreRunRecorder(store)
    scheduler = EmberScheduler(
        store=store,
        skill_source=skill_registry,
        channel=channel,
        settings=settings,
        identity=identity,
        tools=tool_registry,
        router=router,
    )

    # Slash command registry — v1: /context + /help; v2: /model;
    # v3: /memory_revert; v6: /runs, /run, /schedule; v7: /skill;
    # v9: /clear, /compact, /summarize, /trim, /pin.
    memory_revert_tracker = MemoryRevertTracker()
    # v15 (FC-29 / TLC-25): session rotation replaces the auto-compactor.
    # Wired into the agent (turn-start auto check) and /compact //summarize
    # (forced rotation).
    rotator = SessionRotator(store=store, router=router)
    command_registry = CommandRegistry()
    command_registry.register(ContextCommand())
    command_registry.register(HelpCommand())
    command_registry.register(ModelCommand())
    command_registry.register(
        MemoryRevertCommand(
            tracker=memory_revert_tracker,
            tool_registry=tool_registry,
        )
    )
    command_registry.register(RunsCommand(recorder=recorder, scheduler=scheduler))
    command_registry.register(RunCommand(scheduler=scheduler, skill_registry=skill_registry))
    command_registry.register(ScheduleCommand(scheduler=scheduler))
    command_registry.register(SkillCommand(skill_registry=skill_registry, store=store))
    command_registry.register(ClearCommand())
    command_registry.register(CompactCommand(rotator=rotator))
    command_registry.register(SummarizeCommand(rotator=rotator))
    command_registry.register(TrimCommand())
    command_registry.register(PinCommand())
    command_registry.register(LockCommand(unlock_store=unlock_store))
    command_registry.register(SchwabReauthCommand())
    # v11/v13: eval harness. The EvalService owns the single-port recording
    # bridge + a serial job queue shared by `/eval run` and the v13 auto-eval
    # fired on proposal apply (FC-25). Posts run summaries to the scheduler
    # channel via the same notifier surface scheduled runs use.
    eval_service = EvalService(
        store=store,
        router=router,
        identity=identity,
        skill_registry=skill_registry,
        tool_registry=tool_registry,
        settings=settings,
        notify=ChannelRunNotifier(channel).notify,
    )
    command_registry.register(EvalCommand(store=store, service=eval_service, settings=settings))
    # v12.5: /proposal approve|reject|edit — the human-gated resolution half of
    # the learning loop. Its profile applier renders the update at approve-time
    # via a dedicated Proposer (NOT the PROFILE_PROPOSER_ENABLED-gated auto
    # proposer — that gate governs the nagging per-turn suggestion flow, which
    # is unrelated to a user-initiated approval).
    command_registry.register(
        ProposalCommand(
            identity=identity,
            profile_writer=profile_writer,
            lessons_writer=LessonsWriter(identity=identity, store=store),
            proposer=Proposer(router=router),
            # v13: approving a profile/lesson proposal auto-fires a scoped eval
            # on the shared serial queue; the delta shows in the next digest.
            eval_service=eval_service,
            notify=ChannelRunNotifier(channel).notify,
        )
    )

    # The DiscordChannel registers slash commands on bot ready; wire the
    # registry now that it's complete. Slash commands are a Discord-native
    # surface, so this stays on the adapter, not the mux.
    discord_channel._command_registry = command_registry

    # v9: profile proposer. The writer was already constructed earlier
    # (alongside the profile_update tool registration) — reuse it.
    # Proposer is gated by PROFILE_PROPOSER_ENABLED (default off) — the
    # auto-suggest flow consistently produced unwanted prompts; the manual
    # `profile_update` tool covers the use case.
    proposer = Proposer(router=router) if settings.profile_proposer_enabled else None
    writer = profile_writer

    agent = Agent(
        store=store,
        router=router,
        channel=channel,
        settings=settings,
        identity=identity,
        memory_tracker=memory_revert_tracker,
        skill_registry=skill_registry,
        tool_registry=tool_registry,
        rotator=rotator,
        proposer=proposer,
        writer=writer,
        unlock_store=unlock_store,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _on_signal() -> None:
        _log.info("signal received; stopping")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass  # Windows / restricted env

    # Inbound messages reach the agent through the gate, never directly:
    # it serializes turns per conversation (discord.py dispatches each
    # on_message in its own task) and coalesces quick follow-ups.
    async def _announce_interrupt(conversation_key: str) -> None:
        # The cancelled turn stops mid-flight; anything it already did
        # (a sent email, a written memory) stands. Say so rather than
        # letting the reply just vanish.
        await channel.send(
            conversation_key,
            "⏹️ interrupted — restarting with your new message. "
            "Any tool calls already made still stand.",
            kind="status",
        )

    gate = TurnGate(
        agent.handle_turn,
        debounce_s=settings.turn_debounce_s,
        max_debounce_s=settings.turn_max_debounce_s,
        interrupt=settings.turn_interrupt_enabled,
        on_interrupt=_announce_interrupt,
        # SIRI-TLC-1: one turn in flight process-wide, across all channels
        # and conversations — `bridge/active.py` is a module-global handle
        # that concurrent turns would race (cross-posted attachments).
        # Not configurable: the invariant holds until the bridge grows
        # per-turn conversation context (doc #55 Deferrals).
        serialize_conversations=True,
    )
    channel_task = asyncio.create_task(channel.start(gate.submit), name="channels")
    stop_task = asyncio.create_task(stop_event.wait(), name="stop")

    # Scheduler starts after the channel coroutine is launched so that
    # boot-time missed-fire notifications have a place to land. Skill
    # watcher starts after the scheduler so the first refresh runs
    # against the boot-time skill snapshot, not a partially-reloaded one.
    await scheduler.start()
    await _restore_disabled_jobs(scheduler, skill_registry, store)
    await skill_loader.start_watching()

    try:
        done, _pending = await asyncio.wait(
            {channel_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if task is channel_task and task.exception() is not None:
                _log.error("channel exited unexpectedly", exc_info=task.exception())
    finally:
        # Reverse-order shutdown: watcher → scheduler → channel → bridge → store.
        await skill_loader.stop()
        await scheduler.stop()
        await channel.stop()
        if not channel_task.done():
            channel_task.cancel()
            try:
                await channel_task
            except (asyncio.CancelledError, Exception):
                pass
        if not stop_task.done():
            stop_task.cancel()
        await bridge.stop()
        for client in mcp_clients:
            try:
                await client.disconnect()
            except Exception:
                _log.debug("disconnect %s noisy", client.name, exc_info=True)
        # mcp_connector live clients (boot-replayed + runtime-mounted).
        # Snapshot via list() because disconnect() is allowed to mutate.
        connector_clients = list(mcp_connector_live_clients().values())
        for client in connector_clients:
            try:
                await client.disconnect()
            except Exception:
                _log.debug("disconnect connector client %s noisy", client.name, exc_info=True)
        mcp_connector_live_clients().clear()
        if records_store is not None:
            await records_store.close()
        await store.close()


async def _load_skills_best_effort(loader: DirectorySkillLoader) -> None:
    """Attempt `loader.load_all()`; log + continue on per-skill failures.

    The loader currently raises on the first failing module, which would
    be wrong for production startup (one bad skill must not gate the
    rest). We iterate the directory ourselves so a single skill failure
    is contained.
    """
    paths = loader._enumerate_skill_files()  # noqa: SLF001 — loader's own helper
    for path in paths:
        try:
            await loader._reload_one(path)  # noqa: SLF001
        except SkillLoadError as e:
            _log.error("skill %s failed to load: %s", path.name, e)
        except Exception:
            _log.exception("unexpected failure loading %s", path)
    if paths:
        _log.info("loaded %d skill(s) from %s", len(paths), loader._directory)
    else:
        _log.info("no skills found in %s", loader._directory)


async def _restore_disabled_jobs(
    scheduler: EmberScheduler,
    registry: InMemorySkillRegistry,
    store: Store,
) -> None:
    """Re-pause jobs that were disabled before the last shutdown.

    `scheduled_jobs_state` survives restarts; APScheduler's MemoryJobStore
    does not, so this is the bridge.
    """
    for sf in registry.list_schedules():
        if not await store.is_job_enabled(sf.skill, sf.spec.name):
            try:
                await scheduler.set_enabled(sf.skill, sf.spec.name, False)
                _log.info("restored disabled state for %s:%s", sf.skill, sf.spec.name)
            except Exception:
                _log.exception("failed to restore disabled state for %s:%s", sf.skill, sf.spec.name)


async def _replay_connected_mcps(registry: Registry, store) -> None:
    """Boot-time replay of `connected_mcps` (v=7). For each persisted
    row, open an `McpOutboundClient`, run prefix discovery, and stash
    the live client in the connector module's shared map.

    Failures are logged and swallowed: a stale row (e.g. expired JWT,
    deleted upstream MCP) must not block the rest of the agent from
    booting. The model can re-mount via `mcp_connect` on the next turn.
    """
    try:
        rows = await store.list_connected_mcps()
    except Exception:
        _log.exception("connected_mcps replay: failed to read store")
        return

    live = mcp_connector_live_clients()
    for row in rows:
        name = row["name"]
        url = row["url"]
        headers = row["headers"]
        transport = row.get("transport") or "streamableHttp"
        client = McpOutboundClient(name=name, url=url, headers=headers, transport=transport)
        try:
            await client.connect()
        except Exception as e:
            _log.warning(
                "connected_mcps replay: %s (%s) skipped — %s; model can re-mount via mcp_connect",
                name,
                url,
                e,
            )
            continue
        try:
            registered = await _register_prefixed_for_replay(registry, client)
        except Exception:
            _log.exception("connected_mcps replay: %s tool registration failed", name)
            try:
                await client.disconnect()
            except Exception:
                _log.debug("disconnect after replay failure noisy", exc_info=True)
            continue
        live[name] = client
        _log.info(
            "connected_mcps replay: %s connected (%d tools: %s)",
            name,
            len(registered),
            registered,
        )


async def _register_prefixed_for_replay(registry: Registry, client: McpOutboundClient) -> list[str]:
    """Thin indirection so this file doesn't import `_register_prefixed`
    directly from `tools/memory.py` (a leading-underscore name)."""
    from ember.tools.memory import _register_prefixed

    return await _register_prefixed(registry, client)


async def _maybe_register_mcp_outbound(
    registry: Registry,
    clients: list[McpOutboundClient],
    url: str,
    name: str,
    register_fn,
) -> None:
    """Best-effort: connect to an upstream MCP, register its tools.
    Empty URL or connection failure logs and continues — the agent boots
    even if memory/documents are temporarily unavailable."""
    if not url:
        _log.info("%s MCP url empty; skipping", name)
        return
    client = McpOutboundClient(name=name, url=url)
    try:
        await client.connect()
    except Exception as e:
        _log.warning("%s MCP outbound disabled: %s", name, e)
        return
    try:
        registered = await register_fn(registry, client)
    except Exception:
        _log.exception("%s MCP outbound tool registration failed", name)
        await client.disconnect()
        return
    clients.append(client)
    _log.info("%s MCP outbound: %d tools (%s)", name, len(registered), registered)


def _maybe_register_vault_tools(registry: Registry, settings: Settings) -> None:
    if settings.vault_broker_token is None:
        _log.info("VAULT_BROKER_TOKEN not set; vault tools disabled")
        return
    token = settings.vault_broker_token.get_secret_value()
    registry.register(VaultListTool(broker_url=settings.vault_broker_url, token=token))
    registry.register(VaultGetTool(broker_url=settings.vault_broker_url, token=token))
    _log.info("vault tools registered against %s", settings.vault_broker_url)


def _maybe_register_mealie_tools(registry: Registry, settings: Settings) -> None:
    if settings.mealie_api_key is None:
        _log.info("MEALIE_API_KEY not set; mealie tools disabled")
        return
    api_key = settings.mealie_api_key.get_secret_value()
    base = settings.mealie_base_url
    for cls in (
        MealieSearchTool,
        MealieRandomTool,
        MealieGetRecipeTool,
        MealieAddRecipeTool,
        MealieCreateRecipeTool,
        MealieUpdateRecipeTool,
    ):
        registry.register(cls(base_url=base, api_key=api_key))
    _log.info("mealie tools registered against %s", base)


def _maybe_register_docs_tools(
    registry: Registry, settings: Settings, records_store: RecordsStore | None
) -> None:
    """doc #59 FC-36: docs_search / docs_fetch / docs_file / docs_sync,
    registered only when PAPERLESS_API_TOKEN is set (and hence
    `records_store` was opened)."""
    if settings.paperless_api_token is None or records_store is None:
        _log.info("PAPERLESS_API_TOKEN not set; docs tools disabled")
        return
    client = PaperlessClient(
        base_url=settings.paperless_base_url,
        token=settings.paperless_api_token.get_secret_value(),
    )
    indexer = DocumentIndexer(
        client=client, store=records_store, exclude_tag=settings.docs_exclude_tag
    )
    public_url = settings.docs_public_url
    registry.register(DocsSearchTool(store=records_store, public_url=public_url))
    registry.register(DocsFetchTool(store=records_store, public_url=public_url))
    registry.register(
        DocsFileTool(
            client=client,
            indexer=indexer,
            data_dir=settings.data_dir,
            public_url=public_url,
            tika_enabled=settings.paperless_tika_enabled,
        )
    )
    registry.register(DocsSyncTool(indexer=indexer))
    _log.info(
        "docs tools registered against %s (index=%s, exclude_tag=%r)",
        settings.paperless_base_url,
        settings.records_db_path,
        settings.docs_exclude_tag,
    )


def _maybe_register_display_tool(registry: Registry, settings: Settings) -> None:
    """Register display_push against the inkframe display-manager.

    No secret to gate on (unauthenticated LAN-internal service), so this
    always registers — the helper shape is kept for consistency with the
    other _maybe_register_* wiring.
    """
    registry.register(DisplayPushTool(base_url=settings.display_base_url))
    _log.info("display_push registered against %s", settings.display_base_url)


def _maybe_register_gmail_tools(registry: Registry, settings: Settings) -> None:
    if settings.gmail_address is None or settings.gmail_app_password is None:
        _log.info("GMAIL_ADDRESS or GMAIL_APP_PASSWORD not set; gmail tools disabled")
        return
    pw = settings.gmail_app_password.get_secret_value()
    common = {
        "address": settings.gmail_address,
        "app_password": pw,
        "imap_host": settings.gmail_imap_host,
        "imap_port": settings.gmail_imap_port,
        "smtp_host": settings.gmail_smtp_host,
        "smtp_port": settings.gmail_smtp_port,
        "from_name": settings.gmail_from_name,
    }
    registry.register(GmailListTool(**common))
    registry.register(GmailSearchTool(**common))
    registry.register(GmailReadTool(**common))
    registry.register(GmailDraftTool(**common))
    registry.register(
        GmailSendTool(
            **common,
            allowed_recipients=settings.gmail_send_allowed_recipients,
            daily_cap=settings.gmail_send_daily_cap,
            log_path=settings.data_dir / "gmail_send_log.jsonl",
        )
    )
    _log.info(
        "gmail tools registered for %s (send auto-allow=%d recipients, "
        "others require approval, cap=%d/24h)",
        settings.gmail_address,
        len(settings.gmail_send_allowed_recipients),
        settings.gmail_send_daily_cap,
    )


def _maybe_register_tiller_query_tool(registry: Registry, settings: Settings) -> None:
    """Register tiller_query over the tiller-sync SQLite snapshot.

    Independent of the live-sheet tiller tools — it needs only the DB
    file (mounted read-only from /srv/tiller-sync/data).
    """
    if settings.tiller_db_path is None:
        _log.info("TILLER_DB_PATH not set; tiller_query disabled")
        return
    if not settings.tiller_db_path.exists():
        _log.warning("tiller DB not found at %s; tiller_query disabled", settings.tiller_db_path)
        return
    registry.register(
        TillerQueryTool(
            db_path=settings.tiller_db_path,
            default_limit=settings.tiller_query_default_limit,
            max_limit=settings.tiller_query_max_limit,
        )
    )
    _log.info("tiller_query registered for %s", settings.tiller_db_path)


def _maybe_register_schwab_tools(registry: Registry, settings: Settings) -> None:
    """Register the six Schwab brokerage tools (BR-TLC-8).

    No-op when either app key or secret is unset (read-only or
    unconfigured deploys). Idempotent at the no-op boundary; calling
    twice with full settings raises ValueError from the registry per
    its invariant — that's intended.
    """
    if settings.schwab_app_key is None or settings.schwab_app_secret is None:
        _log.info("SCHWAB_APP_KEY/SECRET not set; schwab tools disabled")
        return

    data_dir = settings.data_dir / "schwab"
    data_dir.mkdir(mode=0o700, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
    except OSError:
        pass

    common = {
        "settings": settings,
        "data_dir": data_dir,
    }

    registry.register(SchwabHoldingsTool(**common))
    registry.register(SchwabQuoteTool(**common))
    registry.register(SchwabOpenOrdersTool(**common))
    registry.register(SchwabTransactionsTool(**common))
    registry.register(SchwabPlaceOrdersTool(**common))
    registry.register(SchwabCancelOrderTool(**common))

    _log.info(
        "schwab tools registered (accounts=%d, allowed_writes=%d)",
        len(settings.schwab_account_names),
        len(settings.schwab_allowed_accounts),
    )


def _maybe_register_icloud_tools(registry: Registry, settings: Settings) -> None:
    if settings.icloud_apple_id is None or settings.icloud_app_password is None:
        _log.info("ICLOUD_APPLE_ID or ICLOUD_APP_PASSWORD not set; icloud tools disabled")
        return
    pw = settings.icloud_app_password.get_secret_value()
    common = {
        "apple_id": settings.icloud_apple_id,
        "app_password": pw,
        "caldav_url": settings.icloud_caldav_url,
    }
    registry.register(RemindersListTool(**common))
    registry.register(RemindersAddTool(**common))
    registry.register(RemindersCompleteTool(**common))
    registry.register(CalendarListCalendarsTool(**common))
    registry.register(CalendarListEventsTool(**common))
    registry.register(CalendarAddEventTool(**common))
    _log.info(
        "icloud tools registered for %s (caldav=%s)",
        settings.icloud_apple_id,
        settings.icloud_caldav_url,
    )


def _maybe_register_web_tools(registry: Registry, settings: Settings) -> None:
    # rss_fetch + http_request need no API key; register unconditionally.
    registry.register(RssFetchTool())
    registry.register(HttpRequestTool())
    if settings.brave_api_key is None:
        _log.info(
            "BRAVE_API_KEY not set; web_search/web_fetch disabled "
            "(rss_fetch + http_request still registered)"
        )
        return
    token = settings.brave_api_key.get_secret_value()
    registry.register(WebSearchTool(api_key=token))
    registry.register(WebFetchTool())
    _log.info("web tools registered (Brave Search + web_fetch + rss_fetch + http_request)")


def _maybe_register_browser_tool(registry: Registry, settings: Settings) -> None:
    if settings.browserless_token is None:
        _log.info("BROWSERLESS_TOKEN not set; web_browse tool disabled")
        return
    token = settings.browserless_token.get_secret_value()
    registry.register(WebBrowseTool(base_url=settings.browserless_url, token=token))
    _log.info("web_browse tool registered against %s", settings.browserless_url)


def _maybe_register_gemini_image(registry: Registry, settings: Settings) -> None:
    if settings.google_api_key is None:
        _log.info("GOOGLE_API_KEY not set; generate_image tool disabled")
        return
    registry.register(
        GenerateImageTool(
            api_key=settings.google_api_key.get_secret_value(),
            model=settings.gemini_image_model,
            data_dir=settings.data_dir,
        )
    )
    _log.info("generate_image tool registered (model=%s)", settings.gemini_image_model)


def _write_mcp_config(settings: Settings, bridge: Bridge) -> None:
    """Write the agent-mcp.json that claude reads on each turn."""
    path = settings.bridge_config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bridge.mcp_config_dict(), indent=2))
    _log.info("wrote MCP config to %s", path)


def run() -> None:
    """Synchronous entrypoint used by the `ember` console script."""
    settings = load_settings()
    configure_logging(
        settings.log_level,
        secrets=[settings.discord_bot_token.get_secret_value()],
    )
    _log.info("ember starting; data_dir=%s", settings.data_dir)
    asyncio.run(_amain(settings))


if __name__ == "__main__":
    run()
