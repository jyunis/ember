"""`/context` — show token + cost + turn aggregates for the current
conversation.

Per architecture doc § Token tracker + auto-compactor: tokens used,
% of budget, $ cost (None for subscription), subscription turn count,
oldest message timestamp. v1 has no auto-compaction yet — this is
purely observational.

TLC-5 (cost transparency): subscription turns and metered $ cost are
reported separately, never combined into one number.

v15 (FC-28 / UoW-28 observability): also shows the honest context
measurement — `conversations.last_context_tokens` against the rotation
threshold — and whether a handoff summary is in place. The historical
in/out/cached aggregate stays, but it is bookkeeping, not a
context-size proxy (TLC-24).
"""

from __future__ import annotations

from ember.commands.base import CommandContext, CommandOption
from ember.context.rotation import SessionRotator

_OUT_OF_THREAD = (
    "ℹ️ `/context` must be invoked in a conversation thread (forum thread under `conversations`)."
)


class ContextCommand:
    name: str = "context"
    description: str = "Show token usage and cost for this conversation."
    options: list[CommandOption] = []

    async def run(self, ctx: CommandContext) -> str:
        if ctx.conversation_key is None:
            return _OUT_OF_THREAD

        conv = await ctx.store.get_or_create_conversation("discord", ctx.conversation_key)
        # Pull every message — v1 conversations are small enough for
        # in-memory aggregation. Replace with a SQL aggregator when
        # auto-compaction lands and DBs grow.
        messages = await ctx.store.recent_messages(conv.id, limit=1_000_000)

        total_in = 0
        total_out = 0
        total_cached = 0
        sub_turns = 0
        metered_cost = 0.0
        api_failovers = 0
        for m in messages:
            if m.failover:
                api_failovers += 1
            if m.usage is None:
                continue
            total_in += m.usage.input_tokens
            total_out += m.usage.output_tokens
            total_cached += m.usage.cached_tokens
            if m.usage.subscription:
                sub_turns += 1
            elif m.usage.cost_usd is not None:
                metered_cost += m.usage.cost_usd

        pos, neg, other = await ctx.store.feedback_counts(conv.id)

        # v16 closeout: the old `used / token_budget` percentage is gone —
        # it divided a cumulative billing sum by the retired compactor's
        # budget column, a number with no relationship to context size
        # (TLC-24) and doubly meaningless against a 1M-window model. The
        # honest gauge is the Context line below.
        # v15: measured context vs the rotation threshold (runtime_config
        # override, else the code default — same resolution the rotator
        # applies per check).
        async def _config_number(key: str, default, parse):
            raw = await ctx.store.get_runtime_config(key)
            if raw:
                try:
                    return parse(raw)
                except (TypeError, ValueError):
                    pass  # rotator warns about this; the default is shown here
            return default

        threshold = await _config_number(
            "rotation_context_tokens", SessionRotator.DEFAULT_CONTEXT_TOKENS, int
        )
        cold_tokens = await _config_number(
            "rotation_cold_cache_tokens", SessionRotator.DEFAULT_COLD_CACHE_TOKENS, int
        )
        cold_minutes = await _config_number(
            "rotation_cold_cache_minutes", SessionRotator.DEFAULT_COLD_CACHE_MINUTES, float
        )
        rotates = f"rotates at {threshold:,}, or {cold_tokens:,} after {cold_minutes:g}m idle"
        if conv.last_context_tokens is not None:
            context_line = f"Context: {conv.last_context_tokens:,} tokens ({rotates})"
        else:
            context_line = f"Context: not yet measured ({rotates})"
        summaries = await ctx.store.list_summaries(conv.id)
        handoff_line = f"Handoff summary: {'yes' if summaries else 'none'}"

        lines = [
            "**Conversation context**",
            f"Route: `{conv.provider_route}`"
            + ("  (failover: on)" if conv.auto_failover_on_rate_limit else "  (failover: off)"),
            f"Messages: {len(messages)}",
            context_line,
            handoff_line,
            f"Lifetime tokens: in {total_in:,}, out {total_out:,}, cached {total_cached:,}",
            f"Subscription turns: {sub_turns}",
            f"API failovers: {api_failovers}",
            f"Metered cost: ${metered_cost:.4f}",
            f"Feedback: 👍 {pos}  👎 {neg}  other {other}",
        ]
        if messages:
            lines.append(f"Oldest message: {messages[0].created_at.isoformat()}")
        return "\n".join(lines)
