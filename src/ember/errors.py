"""Ember error taxonomy.

These types are part of the public contract surface — provider/tool/channel
implementations raise these specific subclasses so the agent loop can branch
on them. Never raise raw library exceptions across module boundaries.

See contracts doc § FC-2 (Provider), FC-3 (Tool), FC-1 (Channel).
"""

from __future__ import annotations


class EmberError(Exception):
    """Base for all Ember-raised errors."""


# --- Configuration / startup ---------------------------------------------


class ConfigError(EmberError):
    """Required config or env var is missing or malformed."""


# --- Provider errors (FC-2) ----------------------------------------------


class ProviderError(EmberError):
    """Catch-all for provider failures the agent loop can recover from."""


class RateLimitError(ProviderError):
    """Provider refused for rate-limit reasons.

    Carries an optional `reset_at` (epoch seconds) when the provider
    surfaces one. Triggers the rate-limit failover flow (UoW-15).
    """

    def __init__(self, message: str = "rate limited", reset_at: float | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class AuthError(ProviderError):
    """Provider rejected auth (missing/invalid key, expired CLI session)."""


class SessionNotFoundError(ProviderError):
    """A `--resume <id>` referenced a claude CLI session that no longer
    exists. Happens when claude's transcript cleanup (default
    `cleanupPeriodDays=30`) prunes the jsonl of a conversation idle
    longer than the retention window, or after a `/data` rebuild. The
    agent loop recovers by clearing the dead session id and retrying
    once with a fresh session (claude-side context is lost; Ember's own
    message history is retained)."""


class TransportError(ProviderError):
    """Network/transport failure. Retryable up to N=2 with backoff (FC-2)."""


# --- Store errors --------------------------------------------------------


class StoreError(EmberError):
    """SQLite store failure (schema mismatch, FK violation, IO)."""


class SchemaVersionError(StoreError):
    """Store refuses to start against an unrecognized schema (FC-7)."""


class RecordsStoreError(EmberError):
    """`records.db` failure (doc #59 FC-35): schema newer than this build
    knows, FTS5 missing at open, IO, or a MATCH expression FTS5 rejected
    even after the quoted retry. Sibling of `StoreError`, not a subclass —
    the records data plane is a distinct concern (Decision #3) and callers
    that catch conversation-store failures must not silently swallow index
    failures."""


class PaperlessEnumerationError(EmberError):
    """`PaperlessClient.list_document_ids` could not produce a COMPLETE id
    list (transport error, non-2xx page, or collected count != reported
    `count`). Reconciliation deletes only against a complete enumeration
    (TLC-30 fail-safe), so the indexer catches this, deletes nothing, and
    reports `reconciliation_skipped=True`."""


class ProposalStateError(StoreError):
    """Illegal proposal state transition, or a transition targeting an unknown
    or wrong-status proposal id (FC-23). The proposal state machine is
    `pending → approved → applied`, `pending → rejected`, `pending → expired`;
    every other edge — and any transition on a terminal (applied/rejected/
    expired) row — raises this. The `/proposal` command catches it and replies
    with the current pending ids."""


# --- Channel errors (FC-1) -----------------------------------------------


class ChannelError(EmberError):
    """Channel adapter failure."""


class ChannelDisconnected(ChannelError):
    """Permanent channel disconnect; let the process exit and restart."""


# --- Tool errors (FC-3) — forward-looking; v0 has no tools but the
#     taxonomy lands now so the registry can wire them in v0.5 without
#     reshuffling other modules.


class ToolError(EmberError):
    """Tool failure; do not retry. Surfaced to the model as a tool result."""


class ToolRetryable(ToolError):
    """Transient tool failure; agent loop may retry per its policy."""


class ToolValidationError(ToolError):
    """Args failed input_schema validation (TLC-6). Tool was not invoked."""


# --- Turn-bound errors (TLC-15) ------------------------------------------


class TurnBoundExceeded(EmberError):
    """A per-turn bound (wall-clock, provider calls, tool calls, output
    tokens) was exceeded. Caught by the agent loop and surfaced as a
    graceful stop — never raised out of the loop itself."""

    def __init__(self, bound: str, limit: int | float) -> None:
        super().__init__(f"turn bound exceeded: {bound}={limit}")
        self.bound = bound
        self.limit = limit


# --- Skill / scheduler errors (FC-5, FC-6, TLC-13) -----------------------


class SkillLoadError(EmberError):
    """A skill module failed validation at load time.

    Raised for: missing required attributes, sync functions in
    `scheduled_runs` slots, unknown function references, declarative
    manifests carrying `scheduled_runs` (TLC-13), or import errors.
    """


class SchedulerError(EmberError):
    """Scheduler service failure (job-store IO, malformed cron, etc.)."""


class UnknownScheduleError(SchedulerError):
    """Slash command referenced a `(skill, run_name)` not in the loaded
    set. Surfaced to the user as a typed error."""
