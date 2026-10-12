"""Session rotation (v15, UoW-28 / FC-29 / TLC-25).

Rotation replaces in-context compaction: Ember never summarizes into a
live context mid-conversation. When a conversation goes idle past the
threshold, its measured context (TLC-24) grows past the token
threshold, or it resumes on a cold prompt cache while already large,
the NEXT turn starts on a fresh claude session carrying a short
cheap-tier handoff brief instead of the accumulated context. The
old compactor (FC-10 / UoW-12) is retired; `/compact` and `/summarize`
are now manual forced rotation.

All failure modes are non-fatal (advisory posture): `maybe_rotate` /
`rotate` never raise out — the turn proceeds on the old session and
rotation retries at the next turn start. Reasons surface as
`RotationResult.skipped_reason`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from ember.errors import ConfigError
from ember.providers.base import Message
from ember.providers.routing import ProviderRouter
from ember.store.repo import Conversation, Store

logger = logging.getLogger("ember.context.rotation")


# Fixed handoff prompt (UoW-28 step 2). A module constant — never
# interpolated — so the cheap-tier call is reproducible and the summary
# shape is stable across rotations.
HANDOFF_PROMPT = """\
You are condensing the earlier portion of one ongoing chat so the SAME
conversation can continue with a smaller context. This is not a new
chat and not a different thread — write the brief as the assistant's
own earlier context, in-conversation (e.g. "Earlier in this
conversation we decided ..."). You may also be given the brief written
at the previous condensation; it covers older turns that are not in the
transcript and is otherwise lost, so carry forward anything from it that
still matters. From the brief and the transcript, capture only what is
needed to continue seamlessly:
- open threads (anything in progress or awaiting an answer)
- decisions already made
- the user's stated preferences
- unresolved questions
At most 350 words. Output the brief only — no preamble, no headers, no
meta-commentary, and never describe the transcript as a prior or
separate conversation.
"""

# Cap on the persisted handoff text. Applied deterministically (plain
# character slice) after the empty-summary check.
_SUMMARY_MAX_CHARS = 3000

# Per-message cap when inlining the transcript into the handoff call
# (v16.2). Deterministic plain slice; long tool-heavy replies carry
# their point in the head.
_TRANSCRIPT_MSG_MAX_CHARS = 1500

# Budget for the inlined handoff transcript (~30k tokens). Filled
# newest-first from the WHOLE non-compacted range, so a long stretch
# between rotations is condensed in full rather than just its tail;
# messages that don't fit are replaced by an omission marker.
_HANDOFF_TRANSCRIPT_MAX_CHARS = 120_000

# Effectively-unlimited cap on the non-compacted message listing
# (`Store.recent_messages` requires a limit; compactor precedent).
_LIST_LIMIT = 10_000

# runtime_config keys for the tunable thresholds (locked decision #2).
_IDLE_HOURS_KEY = "rotation_idle_hours"
_CONTEXT_TOKENS_KEY = "rotation_context_tokens"
_COLD_CACHE_MINUTES_KEY = "rotation_cold_cache_minutes"
_COLD_CACHE_TOKENS_KEY = "rotation_cold_cache_tokens"


def _serialize_transcript(messages) -> str:
    """Deterministic plain-text rendering of the handoff input (v16.2).

    Pure function of the message rows: role-labelled lines, each body
    sliced at `_TRANSCRIPT_MSG_MAX_CHARS`, joined by blank lines, kept
    newest-first up to `_HANDOFF_TRANSCRIPT_MAX_CHARS` (older overflow
    becomes one omission marker). No clocks, no ids — the same rows
    always yield the same bytes."""
    parts: list[str] = []
    used = 0
    for m in reversed(messages):
        label = "User" if m.role == "user" else "Assistant"
        body = (m.content or "").strip()
        if len(body) > _TRANSCRIPT_MSG_MAX_CHARS:
            body = body[:_TRANSCRIPT_MSG_MAX_CHARS] + " […truncated]"
        line = f"{label}: {body}"
        if parts and used + len(line) > _HANDOFF_TRANSCRIPT_MAX_CHARS:
            break
        parts.append(line)
        used += len(line) + 2
    omitted = len(messages) - len(parts)
    if omitted:
        parts.append(f"[… {omitted} older messages omitted]")
    return "\n\n".join(reversed(parts))


@dataclass(frozen=True, slots=True)
class RotationResult:
    """Outcome of one `rotate` / `maybe_rotate` call.

    `rotated` is True only when the full transition landed (summary row
    inserted, messages compacted, session + context cleared). `reason`
    records what triggered the attempt; `skipped_reason` is set on every
    non-success path, drawn from the UoW-28 vocabulary:
    {under_threshold, nothing_to_rotate, cheap_tier_unavailable,
    provider_error: <T>, empty_summary, mark_compacted_failed: <T>}.
    """

    rotated: bool
    reason: str | None  # "idle_gap" | "cold_cache" | "context_size" | "forced" | None
    summary_id: int | None
    skipped_reason: str | None


class SessionRotator:
    """Turn-start session rotation (UoW-28).

    One instance per agent process; `maybe_rotate` is called by the
    agent loop at turn start (before the user message is persisted),
    `rotate(reason="forced")` by `/compact` / `/summarize`. Stateless
    aside from the wired store/router — every decision is recomputed
    from current DB state. All SQL goes through `Store` (FC-7).
    """

    DEFAULT_IDLE_HOURS = 72
    DEFAULT_CONTEXT_TOKENS = 60_000
    # Cold-cache rotation: the prompt cache (1h TTL on the CLI) is gone
    # after this much silence, so the next turn re-writes the whole
    # context at cache-write price (2x input) — measured as ~45% of the
    # quota draw on long chats. Past the token floor, rotating then
    # writes ~55k instead of ~100k+, and the size rotation that was due
    # within hours anyway happens while it is nearly free.
    DEFAULT_COLD_CACHE_MINUTES = 60
    DEFAULT_COLD_CACHE_TOKENS = 95_000
    # Churn guard (v16.2): the measured context includes the ~65k fixed
    # prefix plus this-turn tool payloads, so one heavy research turn can
    # re-cross the threshold immediately after a rotation. Rotating a
    # conversation with only a handful of active messages trades its
    # whole working memory for a 200-word brief and buys almost nothing —
    # require a real accumulation before a context-size rotation fires.
    # Applies to both size triggers (context_size, cold_cache).
    # (Idle-gap and forced rotation are unaffected; the CLI's own
    # auto-compact remains the in-session backstop.)
    MIN_CONTEXT_ROTATION_MESSAGES = 8

    def __init__(self, *, store: Store, router: ProviderRouter) -> None:
        self._store = store
        self._router = router

    # --- Trigger -----------------------------------------------------------

    async def should_rotate(
        self,
        conversation: Conversation,
        *,
        now: datetime,
    ) -> str | None:
        """Auto-rotation precondition check. Returns the trigger reason
        ("idle_gap" | "cold_cache" | "context_size") or None.

        Preconditions (UoW-28): a claude session id must be set (nothing
        to rotate away from otherwise), and either the idle gap or the
        measured context (TLC-24) exceeds its threshold, or both the
        cold-cache gap and the cold-cache token floor are exceeded.
        Thresholds are read from `runtime_config` on every check so they
        are tunable
        without a restart; unset falls back to the code defaults,
        invalid falls back with a warning.
        """
        if conversation.claude_cli_session_id is None:
            return None

        idle_hours = await self._config_number(
            _IDLE_HOURS_KEY, float(self.DEFAULT_IDLE_HOURS), float
        )
        gap = now - conversation.last_active
        if gap > timedelta(hours=idle_hours):
            return "idle_gap"

        context = conversation.last_context_tokens
        if context is None:
            return None

        cold_minutes = await self._config_number(
            _COLD_CACHE_MINUTES_KEY, float(self.DEFAULT_COLD_CACHE_MINUTES), float
        )
        cold_tokens = await self._config_number(
            _COLD_CACHE_TOKENS_KEY, self.DEFAULT_COLD_CACHE_TOKENS, int
        )
        context_threshold = await self._config_number(
            _CONTEXT_TOKENS_KEY, self.DEFAULT_CONTEXT_TOKENS, int
        )
        if gap > timedelta(minutes=cold_minutes) and context > cold_tokens:
            reason = "cold_cache"
        elif context > context_threshold:
            reason = "context_size"
        else:
            return None

        # Churn guard (v16.2): only rotate on context size when a real
        # message accumulation exists to condense. The count query runs
        # only when a size threshold has already tripped.
        sample = await self._store.recent_messages(
            conversation.id,
            limit=self.MIN_CONTEXT_ROTATION_MESSAGES,
            include_compacted=False,
        )
        if len(sample) >= self.MIN_CONTEXT_ROTATION_MESSAGES:
            return reason
        logger.debug(
            "rotation: %s tripped at context %d but only %d active "
            "messages (< %d); churn guard holds",
            reason,
            context,
            len(sample),
            self.MIN_CONTEXT_ROTATION_MESSAGES,
        )
        return None

    async def _config_number(self, key: str, default, parse):
        """Read a numeric threshold from runtime_config. Unset → default
        (silently — that is the normal state); unparseable → default plus
        a warning so a typo'd tune-up doesn't silently change behavior."""
        raw = await self._store.get_runtime_config(key)
        if raw is None or raw == "":
            return default
        try:
            return parse(raw)
        except (TypeError, ValueError):
            logger.warning(
                "rotation: runtime_config %s=%r is not a number; using default %s",
                key,
                raw,
                default,
            )
            return default

    # --- Rotation ----------------------------------------------------------

    async def maybe_rotate(
        self,
        conversation: Conversation,
        *,
        now: datetime,
    ) -> RotationResult:
        """Run one auto-rotation check; rotate iff a trigger fires.
        Never raises out."""
        try:
            reason = await self.should_rotate(conversation, now=now)
        except Exception as e:
            logger.exception("rotation: should_rotate failed; skipping")
            return RotationResult(
                rotated=False,
                reason=None,
                summary_id=None,
                skipped_reason=f"provider_error: {type(e).__name__}",
            )
        if reason is None:
            return RotationResult(
                rotated=False,
                reason=None,
                summary_id=None,
                skipped_reason="under_threshold",
            )
        return await self.rotate(conversation, reason=reason)

    async def rotate(
        self,
        conversation: Conversation,
        *,
        reason: str,
    ) -> RotationResult:
        """Perform one rotation for `reason`. Never raises out.

        Forced rotation (`reason="forced"`, the `/compact` path) skips
        the auto preconditions but still requires ≥2 non-compacted
        messages — rotating a one-line conversation is pure loss.

        Steps (UoW-28): list non-compacted messages → cheap-tier handoff
        over the prior brief (if any) plus the transcript (newest-first
        within `_HANDOFF_TRANSCRIPT_MAX_CHARS`) → delete this
        conversation's existing summaries → insert the one handoff row
        (span = full non-compacted range) → mark ALL listed messages
        compacted → clear the claude session id → clear
        last_context_tokens. A `mark_messages_compacted` failure rolls
        back the just-inserted summary (all-or-nothing, UoW-12
        precedent); cheap-tier failures leave zero writes.
        """
        try:
            return await self._rotate_inner(conversation, reason=reason)
        except Exception as e:
            # Advisory posture: an unexpected failure (store I/O, a bug)
            # must not take the turn down. Surfaced under the generic
            # provider_error reason (closed vocabulary).
            logger.exception("rotation: unexpected failure; skipping")
            return RotationResult(
                rotated=False,
                reason=reason,
                summary_id=None,
                skipped_reason=f"provider_error: {type(e).__name__}",
            )

    async def _rotate_inner(
        self,
        conversation: Conversation,
        *,
        reason: str,
    ) -> RotationResult:
        # 1. List the non-compacted record; it feeds the handoff (within
        # the transcript budget) and the FULL range is compacted/spanned.
        active = await self._store.recent_messages(
            conversation.id,
            limit=_LIST_LIMIT,
            include_compacted=False,
        )
        if not active or (reason == "forced" and len(active) < 2):
            # Idempotency: an immediately repeated forced rotation finds
            # everything already compacted and lands here.
            return RotationResult(
                rotated=False,
                reason=reason,
                summary_id=None,
                skipped_reason="nothing_to_rotate",
            )

        # 2. Cheap-tier handoff brief (FC-10 routing precedent).
        try:
            resolved = self._router.resolve_cheap_tier()
        except ConfigError as e:
            logger.warning("rotation: cheap-tier unavailable: %s", e)
            return RotationResult(
                rotated=False,
                reason=reason,
                summary_id=None,
                skipped_reason="cheap_tier_unavailable",
            )

        # The previous brief is the only record of turns condensed at the
        # last rotation; feed it forward so the chain is cumulative
        # instead of each brief forgetting everything before its window.
        prior = await self._store.list_summaries(conversation.id)
        prior_brief = "\n\n".join(row.summary_text for row in prior).strip()

        # v16.2: the transcript is INLINED into a single user message.
        # The compactor-precedent shape (raw message list + trailing
        # instruction) silently lost the entire conversation on
        # `claude_cli_cheap`, whose fresh-call prompt is only the
        # trailing user run — the summarizer saw just the instruction
        # line and wrote content-free meta-briefs. Inlining is
        # provider-agnostic and deterministic.
        transcript = _serialize_transcript(active)
        if prior_brief:
            preamble = (
                f"Brief from the previous condensation (older turns):\n\n{prior_brief}\n\n"
                "Transcript of the conversation since then:\n\n"
            )
        else:
            preamble = "Transcript of the conversation so far:\n\n"
        msgs: list[Message] = [
            Message(
                role="user",
                content=(
                    f"{preamble}{transcript}\n\nWrite the handoff brief for the conversation above."
                ),
            ),
        ]
        try:
            result = await resolved.provider.complete(
                system=HANDOFF_PROMPT,
                messages=msgs,
                tools=None,
                model=resolved.model,
                session_id=None,
                stream_handler=None,
            )
        except Exception as e:
            logger.warning(
                "rotation: cheap-tier provider error: %s: %s",
                type(e).__name__,
                e,
            )
            return RotationResult(
                rotated=False,
                reason=reason,
                summary_id=None,
                skipped_reason=f"provider_error: {type(e).__name__}",
            )

        summary_text = result.text.strip()
        if not summary_text:
            logger.warning("rotation: cheap-tier returned empty handoff")
            return RotationResult(
                rotated=False,
                reason=reason,
                summary_id=None,
                skipped_reason="empty_summary",
            )
        # Deterministic truncation — a runaway summary must not become
        # the new permanent prefix.
        summary_text = summary_text[:_SUMMARY_MAX_CHARS]

        # 3. Supersede: drop existing summaries, insert the one handoff
        # row spanning the full non-compacted range.
        await self._store.delete_summaries_for_conversation(conversation.id)
        summary_id = await self._store.insert_summary(
            conversation.id,
            start_message_id=active[0].id,
            end_message_id=active[-1].id,
            summary_text=summary_text,
        )

        # 4. Compact ALL listed messages; roll the summary back on
        # failure so the observable transition is all-or-nothing.
        ids = [m.id for m in active]
        try:
            await self._store.mark_messages_compacted(ids)
        except Exception as e:
            logger.warning(
                "rotation: mark_messages_compacted failed (%s); rolling back summary %d",
                type(e).__name__,
                summary_id,
            )
            try:
                await self._store.delete_summary(summary_id)
            except Exception as cleanup_err:
                # Cleanup itself failed — log loudly; the row leaks.
                logger.error(
                    "rotation: cleanup of summary %d failed: %s",
                    summary_id,
                    cleanup_err,
                )
            return RotationResult(
                rotated=False,
                reason=reason,
                summary_id=None,
                skipped_reason=f"mark_compacted_failed: {type(e).__name__}",
            )

        # 5 + 6. Fresh session next turn; stale context measurement gone.
        await self._store.update_claude_cli_session(conversation.id, None)
        await self._store.update_last_context_tokens(conversation.id, None)

        logger.info(
            "rotation: conversation=%d reason=%s messages=%d summary=%d",
            conversation.id,
            reason,
            len(ids),
            summary_id,
        )
        return RotationResult(
            rotated=True,
            reason=reason,
            summary_id=summary_id,
            skipped_reason=None,
        )
