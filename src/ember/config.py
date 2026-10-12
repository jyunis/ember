"""Configuration — single source for env-supplied settings (FC-13).

Loaded once at boot from `.env` + process environment. Required fields
without values cause a fast startup failure with a clear message.

Secret fields use `SecretStr` so accidental string-coercion of the
settings object does not leak them into logs (TLC-4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Discord -----------------------------------------------------------

    discord_bot_token: SecretStr
    discord_guild_id: int
    discord_conversations_channel_id: int
    discord_scheduler_channel_id: int
    discord_allowed_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # --- claude CLI provider ----------------------------------------------
    #
    # claude reads/writes auth from $HOME/.claude — set HOME=/data in the
    # container so auth state persists on the mounted volume. The
    # `claude_auth_dir` field below is kept as a documented expected
    # location; nothing in the agent process reads it directly.

    claude_binary_path: str = "claude"
    claude_auth_dir: Path = Path("/data/claude-auth")
    # Tier tag: `claude --model opus` resolves to the latest Opus, so the
    # default tracks releases without a code change. Bare tags work on
    # every auth path; it's the *dated* alias ("sonnet-4-6") that 404s under
    # a setup-token OAuth credential. Set CLAUDE_DEFAULT_MODEL to another tag
    # ("sonnet", "fable") or a full id to pin a specific version.
    claude_default_model: str = "opus"

    # --- anthropic_api provider (v2) -------------------------------------
    # Optional — when EMBER_ANTHROPIC_API_KEY is set, the anthropic_api
    # provider is registered. Used as a rate-limit failover for claude_cli
    # (UoW-15) and selectable per-conversation via /model.
    #
    # Why the EMBER_ prefix: claude CLI also reads ANTHROPIC_API_KEY from
    # the environment and silently swaps subscription auth for metered API
    # when it's set, routing /model:claude_cli traffic through anthropic.com
    # and hitting org rate limits. Namespacing the env var under EMBER_*
    # eliminates that collision — claude CLI never sees a key it recognizes,
    # so it falls back to subscription auth from ~/.claude/ as intended.
    ember_anthropic_api_key: SecretStr | None = None
    anthropic_default_model: str = "claude-sonnet-4-6"

    # --- deepseek_api provider --------------------------------------------
    # Optional. Registers when DEEPSEEK_API_KEY is set. OpenAI-compatible.
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_default_model: str = "deepseek-chat"

    # Model for the toolless `claude_cli_cheap` provider (see main.py). Same
    # tier-tag rules as claude_default_model.
    claude_cheap_model: str = "sonnet"

    # --- v9: cheap-tier routing ------------------------------------------
    # Used for internal tasks (session-rotation handoffs, retrospective
    # extraction, the eval tone judge). Empty string disables the cheap tier.
    #
    # Points at `claude_cli_cheap` — subscription-billed, so these internal
    # calls cost no metered dollars; the previous default
    # ("anthropic_api:claude-haiku-4-5") spent real money on the API for work
    # the subscription already covers. Note the tradeoff: the cheap tier now
    # shares the claude_cli rate-limit pool, so a subscription rate limit
    # takes rotation handoffs down with the main path (it degrades gracefully —
    # `skipped_reason="provider_error"`). Set this to an `anthropic_api:`
    # route to put internal work back on metered API capacity.
    cheap_tier_route: str = "claude_cli_cheap:sonnet"

    # When false, the post-turn profile-update proposer is not wired into
    # the agent — no classifier/extractor cheap-tier calls run, and no
    # "📝 Proposed profile update" prompts appear. The manual
    # `profile_update` tool is unaffected. Plumbing (Proposer class,
    # agent.py guard, writer) stays intact so this can be flipped back on
    # without code changes.
    profile_proposer_enabled: bool = False

    # --- v3: MCP outbound clients ----------------------------------------
    # Empty string disables the corresponding outbound client.

    memory_mcp_url: str = "http://localhost:9927/mcp"
    documents_mcp_url: str = "http://localhost:9929/mcp"

    # --- v3: vault skill --------------------------------------------------
    # Optional. The user-facing vault tools register only when both URL
    # and token are set.

    vault_broker_url: str = "http://localhost:9927"
    vault_broker_token: SecretStr | None = None

    # --- v5: mealie -------------------------------------------------------
    # Optional. Mealie tools register only when MEALIE_API_KEY is set.

    mealie_base_url: str = "http://mealie:9925"
    mealie_api_key: SecretStr | None = None

    # --- paperless / documents indexer (doc #59) --------------------------
    # Optional. The four docs_* tools and records.db open only when
    # PAPERLESS_API_TOKEN is set. paperless-ngx lives in its own compose
    # project on the shared home-proxy network, so the default base URL
    # resolves without an Ember compose change.
    paperless_base_url: str = "http://paperless:8000"
    paperless_api_token: SecretStr | None = None
    # Widens the docs_file extension allowlist to office formats
    # (docx/xlsx/…) — only flip on once the Tika + Gotenberg sidecars are
    # running next to paperless (Decision #12).
    paperless_tika_enabled: bool = False
    # Where result links point (the paperless web UI as the user reaches
    # it — LAN/tailnet only by design; inert to a remote attacker).
    docs_public_url: str = "http://localhost:9926"
    # Documents carrying this paperless tag never enter the index and are
    # removed within one sync cycle if already there (TLC-30). Matched
    # case-insensitively against resolved tag names.
    docs_exclude_tag: str = "ember-exclude"

    # --- inkframe display --------------------------------------------------
    # display_push targets the display-manager service on the shared
    # home-proxy network. Unauthenticated LAN-internal service — no key,
    # so the tool registers unconditionally.

    display_base_url: str = "http://display-manager:9928"

    # --- v8: gmail ---------------------------------------------------------
    # When both gmail_address + gmail_app_password are set, gmail tools register.
    # Use a Google account "App password" (not your regular password). Generate at
    # https://myaccount.google.com/apppasswords with 2FA on.
    gmail_address: str | None = None
    gmail_app_password: SecretStr | None = None
    gmail_imap_host: str = "imap.gmail.com"
    gmail_imap_port: int = 993
    gmail_smtp_host: str = "smtp.gmail.com"
    gmail_smtp_port: int = 587
    # Display name for the From header ("Alice Example <addr>"); bare address when unset.
    gmail_from_name: str | None = None
    # Outbound safety. The allowlist is the *auto-send* set: recipients
    # matching an exact address (case-insensitive) or a `@domain.tld` entry
    # send without a prompt. Anyone else triggers the human-approval flow
    # (a 🔒 confirmation prompt, like profile_update). An empty allowlist
    # means every send needs approval. Cap is rolling 24h and applies to
    # every send. Log lives at `<data_dir>/gmail_send_log.jsonl`.
    gmail_send_allowed_recipients: Annotated[list[str], NoDecode] = Field(default_factory=list)
    gmail_send_daily_cap: int = 20

    # --- v16: iCloud (Reminders + Calendar via CalDAV) -------------------
    # When both icloud_apple_id + icloud_app_password are set, icloud tools
    # register. Use an Apple ID app-specific password (NOT your regular
    # password). Generate at https://appleid.apple.com under "App-Specific
    # Passwords". One password covers both Reminders and Calendar — they
    # share the same CalDAV endpoint, distinguished by collection type
    # (VTODO vs VEVENT).
    icloud_apple_id: str | None = None
    icloud_app_password: SecretStr | None = None
    icloud_caldav_url: str = "https://caldav.icloud.com"

    # --- v10: web tools ---------------------------------------------------
    # When BRAVE_API_KEY is set, web_search and web_fetch tools register.
    brave_api_key: SecretStr | None = None

    # --- v10.5: browserless ----------------------------------------------
    # Optional. web_browse tool registers only when BROWSERLESS_TOKEN is set.
    browserless_url: str = "http://browserless:3000"
    browserless_token: SecretStr | None = None

    # --- v13: gemini image (nano banana) ----------------------------------
    # Optional. generate_image tool registers when GOOGLE_API_KEY is set.
    # Get a key at https://aistudio.google.com/app/apikey.
    google_api_key: SecretStr | None = None
    gemini_image_model: str = "gemini-2.5-flash-image"

    # --- Tiller ----------------------------------------------------------
    # tiller_query registers when TILLER_DB_PATH is set and the file exists.
    # The DB is the tiller-sync service's twice-daily SQLite snapshot of all
    # three Tiller spreadsheets, mounted read-only (see compose.yml).
    tiller_db_path: Path | None = None
    tiller_query_default_limit: int = 200
    tiller_query_max_limit: int = 2000

    # --- Schwab (steps 1-4) ----------------------------------------------
    # Tools register only when both SCHWAB_APP_KEY and SCHWAB_APP_SECRET
    # are set. Token bootstrap is a one-time browser auth performed on the
    # host (see docs/brokerage-contracts.md "Auth bootstrap"). Subsequent
    # token refreshes happen silently in-process; refresh tokens last ~7
    # days, after which the health-check job (FC-11) DMs the user to
    # re-run the bootstrap.
    schwab_app_key: SecretStr | None = None
    schwab_app_secret: SecretStr | None = None
    schwab_callback_url: str = "https://127.0.0.1:8182"
    # Last-4 → friendly name. Format: "1234=Individual Brokerage,5678=Roth"
    schwab_account_names: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)
    # Friendly names allowed for write operations. Empty = read-only on all,
    # write on none.
    schwab_allowed_accounts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Notional caps (USD).
    schwab_max_order_notional_usd: float = 20000.0
    schwab_max_batch_notional_usd: float = 75000.0
    schwab_max_daily_notional_usd: float = 100000.0
    # Conservative fallback when no quote/holdings price is available
    # for a market order during cap evaluation.
    schwab_fallback_price_usd: float = 1000.0
    # Holdings cache TTL.
    schwab_holdings_cache_seconds: int = 1800
    # Max age of the holdings snapshot a *market* order may be priced from
    # during cap evaluation. Beyond this the notional can't be trusted to
    # bound anything, so the order is refused and the caller must pass an
    # explicit limit price. Deliberately much tighter than the cache TTL:
    # that TTL trades staleness for latency on reads, this one guards money.
    schwab_market_price_max_age_seconds: int = 900

    # --- Confirmation second factor (TOTP) --------------------------------
    #
    # The in-band `y` that redeems a confirmation prompt is not an
    # authenticator: it arrives over the same conversation that proposed the
    # action, so whoever holds that conversation can produce it. Tools named
    # in `confirm_totp_tools` additionally require a rotating code from a
    # separate device (`y 418902`).
    #
    # Fail-closed: a tool listed here with no secret configured has its
    # confirmation REFUSED, never downgraded back to a bare `y` — otherwise
    # clearing one env var would silently remove the protection.
    confirm_totp_secret: SecretStr | None = None
    confirm_totp_tools: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Unlock windows (doc #60, TLC-31) --------------------------------
    #
    # Tools in `locked_tools` are gated per *conversation*, not per call:
    # one valid TOTP code (`y <code>` at the 🔒 prompt, or a bare
    # `unlock <code>`) opens the conversation for `unlock_ttl_seconds`,
    # during which those tools run without prompting. `/lock` ends the
    # window; a restart re-locks everything (state is in-memory). Reuses
    # `confirm_totp_secret`; fails closed without it. Default set is the
    # cleartext vault read (2026-09-27); doc #60's wider personal-data set
    # is opt-in via LOCKED_TOOLS until its seams are locked.
    unlock_ttl_seconds: int = 14400
    locked_tools: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["vault_get"])

    # `import_skill` installs executable Python into the hot-reloaded skills
    # directory, so it converts Discord access into code execution inside the
    # container — and thence to every credential in the environment. Default
    # off: the tool is not registered at all, and installing a skill is a
    # host-side action (edit .env, redeploy) rather than an in-band one.
    allow_skill_import: bool = False

    # --- v15: context lifecycle (doc #56) ---------------------------------

    # Bridge tool results whose serialized form exceeds this many UTF-8
    # bytes are persisted to the payload store and replaced by a small
    # deterministic envelope; the model re-fetches via `read_payload`
    # (FC-30 / TLC-26). The 60KB bridge hard cap remains as backstop.
    payload_store_threshold_bytes: int = 16384

    # When false (default), the claude_cli session-jsonl tool pruner
    # (TLC-18 / FC-19 / UoW-19) does not run — the pruner rewrites an
    # unsupported file format and is deprecated-pending-removal now that
    # oversize results become payload references at the source. Plumbing
    # stays intact so this can be flipped back on without code changes
    # (escape valve while rotation + payload store prove out).
    tool_pruner_enabled: bool = False

    # --- v16: live turn feedback (doc #57) ---------------------------------

    # When True (default), every Discord turn runs the liveness machinery
    # (TLC-27 / UoW-31): a typing keepalive for the whole turn plus, past
    # ~20s, a single activity status message edited in place (elapsed,
    # tool count, last tool) and always deleted at turn end. Kill-switch —
    # flipping it off restores a fully silent long turn.
    turn_activity_enabled: bool = True

    # When True (default), completed assistant text blocks on Discord
    # claude_cli turns are posted as they stream (TLC-28 / UoW-32); the
    # final send carries only the unstreamed remainder. Kill-switch —
    # flipping it off restores the single buffered reply.
    stream_text_blocks: bool = True

    # --- Storage / runtime -------------------------------------------------

    data_dir: Path = Path("/data")
    identity_dirname: str = "identity"
    skills_dirname: str = "skills"

    @property
    def identity_dir(self) -> Path:
        return self.data_dir / self.identity_dirname

    @property
    def skills_dir(self) -> Path:
        return self.data_dir / self.skills_dirname

    # --- v11: eval harness (learning loop Layer 2) ------------------------

    # Cases live under `<data_dir>/evals/cases/*.toml`. Human-authored; the
    # harness only READS them (agent-forbidden write set, TLC-19).
    @property
    def evals_cases_dir(self) -> Path:
        return self.data_dir / "evals" / "cases"

    # The provider route routing/tool_flow/tone completions execute on. Default
    # is the production route so evals measure the real model (doc #36 Decisions
    # — recording-bridge fidelity capture). The tone JUDGE always runs on the
    # cheap tier (`cheap_tier_route`), independent of this.
    eval_route: str = "claude_cli"

    # Dedicated port for the eval-only MCP bridge — separate from the live
    # bridge (`bridge_port`) so eval runs never disturb the running bot. The
    # eval bridge serves the RecordingRegistry (fake tools; zero side effects).
    eval_bridge_port: int = 9932

    @property
    def claude_session_jsonl_dir(self) -> Path:
        """Where claude CLI writes its per-session jsonl logs.

        With `HOME=/data` (set in the Dockerfile), this is
        `/data/.claude/projects/-app`. The tool-output pruner reads and
        rewrites these files between turns to bound prompt-cache cost
        across long-running conversations (TLC-18 / UoW-19).
        """
        return self.data_dir / ".claude" / "projects" / "-app"

    # --- v6: scheduler -----------------------------------------------------

    scheduler_default_timeout_s: int = 300
    scheduler_default_misfire_grace_s: int = 3600
    skills_watch_debounce_ms: int = 500

    # --- Inbound turn gate (channels/gate.py) -----------------------------
    # Turns are always serialized per conversation. These bound the
    # coalescing window that folds quick follow-up messages ("oops, and
    # also…") into a single turn. Set the debounce to 0 to start each
    # turn immediately — serialization still applies, so a follow-up
    # simply queues behind the running turn.
    turn_debounce_s: float = 1.5
    turn_max_debounce_s: float = 6.0
    # When True, a message arriving while a turn is already running
    # cancels that turn and starts a fresh one carrying both messages,
    # rather than queueing behind it. Tool side effects that already
    # happened are NOT undone — the cancelled turn just stops talking —
    # so the user gets a "⏹️ interrupted" status when it fires. Set
    # False to keep strict queueing.
    turn_interrupt_enabled: bool = True

    # --- MCP bridge (v0.5) ------------------------------------------------

    bridge_host: str = "127.0.0.1"
    bridge_port: int = 9931
    bridge_config_filename: str = "agent-mcp.json"

    @property
    def bridge_config_path(self) -> Path:
        return self.data_dir / self.bridge_config_filename

    # --- Siri voice channel (doc #55) -------------------------------------
    # HTTP ingress for Apple Shortcuts over the tailnet. Default off; the
    # adapter is only constructed when enabled, and construction fails
    # closed on a blank token (SIRI-TLC-4). The container listens on
    # `siri_bind:siri_port`; network exposure is decided in compose.yml,
    # which publishes the port bound to the host's tailscale IP only.
    siri_enabled: bool = False
    siri_api_token: SecretStr = SecretStr("")
    siri_bind: str = "0.0.0.0"
    siri_port: int = 9940
    siri_deadline_s: float = 25.0
    siri_session_window_s: int = 600
    # Attribution only (messages.user_id for siri turns) — auth is the
    # bearer token, not this.
    siri_user_id: str = "siri"

    # --- Logging -----------------------------------------------------------

    log_level: str = "INFO"

    # --- Tool-call visibility (decision #3 / TLC-2) -----------------------
    # When True (default), every tool call emits an inline status message
    # like "🔧 tool_name({args})" — useful for debugging / audit. Set False
    # for a quieter chat surface; memory-write announcements, profile-update
    # prompts, errors, and confirmation-required tools still surface.
    show_tool_calls: bool = True

    # --- v12: mcp_connector bounds ---------------------------------------
    # Optional CSV of allowed host patterns for `mcp_connect`. Empty list
    # = unrestricted (matches gmail_send: trust the model on what,
    # bound the blast radius via audit + visibility). Patterns match
    # exactly OR `*.example.com` matches `example.com` and any subdomain.
    mcp_connect_allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Validators --------------------------------------------------------

    @field_validator("discord_allowed_user_ids", mode="before")
    @classmethod
    def _parse_allowed_user_ids(cls, value: Any) -> list[int]:
        """Accept a comma-separated string (env-style) or a JSON list."""
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [int(part.strip()) for part in value.split(",") if part.strip()]
        return value

    @field_validator("confirm_totp_tools", "locked_tools", mode="before")
    @classmethod
    def _parse_confirm_totp_tools(cls, value: Any) -> list[str]:
        """Accept a comma-separated string (env-style) or a JSON list."""
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("gmail_send_allowed_recipients", mode="before")
    @classmethod
    def _parse_gmail_allowed(cls, value: Any) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [part.strip().lower() for part in value.split(",") if part.strip()]
        return [str(v).strip().lower() for v in value]

    @field_validator("mcp_connect_allowed_hosts", mode="before")
    @classmethod
    def _parse_mcp_hosts(cls, value: Any) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [part.strip().lower() for part in value.split(",") if part.strip()]
        return [str(v).strip().lower() for v in value]

    @field_validator("schwab_account_names", mode="before")
    @classmethod
    def _parse_schwab_account_names(cls, value: Any) -> dict[str, str]:
        """Accept a comma-separated string of `last4=name` pairs.

        Format: ``1234=Individual Brokerage,5678=Roth Contributory IRA``.
        Whitespace around keys/values is stripped. Empty / missing →
        empty dict (no accounts known).
        """
        if value is None or value == "":
            return {}
        if isinstance(value, dict):
            return {str(k).strip(): str(v).strip() for k, v in value.items() if str(k).strip()}
        if isinstance(value, str):
            mapping: dict[str, str] = {}
            for pair in value.split(","):
                pair = pair.strip()
                if not pair or "=" not in pair:
                    continue
                last4, name = pair.split("=", 1)
                last4 = last4.strip()
                name = name.strip()
                if last4 and name:
                    mapping[last4] = name
            return mapping
        return {}

    @field_validator("schwab_allowed_accounts", mode="before")
    @classmethod
    def _parse_schwab_allowed_accounts(cls, value: Any) -> list[str]:
        """Accept a comma-separated string of friendly names.

        Whitespace is stripped. Order is preserved; duplicates kept.
        Case is preserved here (the resolver lowercases at compare time
        so users can write either "Individual Brokerage" or "individual
        brokerage" in env without surprise).
        """
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return [str(v).strip() for v in value if str(v).strip()]

    # --- Derived paths -----------------------------------------------------

    @property
    def conversations_db_path(self) -> Path:
        return self.data_dir / "conversations.db"

    # doc #59 Decision #3: the records data plane is its own SQLite file
    # with its own schema ladder — never a set of tables in conversations.db.
    @property
    def records_db_path(self) -> Path:
        return self.data_dir / "records.db"


def load_settings() -> Settings:
    """Construct settings from env. Raises ValidationError on any
    required field missing — surfaced at startup."""
    return Settings()  # type: ignore[call-arg]
