"""retrospective — nightly learning-loop Layer 3 (UoW-22 / FC-22, v12).

Accumulate-only. Once a night it looks back over the last 24h and proposes
durable improvements — it NEVER applies them (application lands in v12.5).
Every proposal is `status='pending'` and carries machine-readable evidence
that points ONLY at user-authored messages, live feedback, and recorded
failures (TLC-20); agent output is never evidence.

Shape (deterministic; LLM output is data, schema-validated, never control
flow — TLC-13):

1. Failed scheduled runs in the window → `finding` proposals. Pure code,
   no LLM: a failed run is already structured. (Cheapest structure first —
   "learn into structure".)
   1b. Silent tool errors (tool calls recorded with `status='error'` inside
   agent turns) → `finding` proposals, grouped by tool (+ leading program
   for Bash). Also pure code. Before this source the loop only noticed a
   tool failure when the USER complained about it; a photo-crop that
   thrashed for six Bash calls hunting for PIL never surfaced. Evidence is
   the user turn that preceded each failing call — the assistant row that
   carries the call is agent output and never evidence (TLC-20).
2. Per conversation active in the last 24h → ONE bounded cheap-tier
   extraction over that conversation's USER messages (+ its 👎 feedback as
   context). The retrospective surfaces ONLY agent-facing signal — where the
   assistant fell short and what capability it is missing. It does NOT record
   user-profile facts (that lane was retired 2026-07-15; it drowned the digest
   in inane trivia — "owns a cat", "~173 lbs" — that no bar in the prompt could
   distinguish from signal). The schema-validated result routes into proposals:
     - friction_events   → finding (correction / tool_failure / thumbs_down):
                           a place the ASSISTANT failed the user
     - recurring         → trigger | skill | lesson (by proposed_structure):
                           a missing capability to grow
   Malformed extraction → that conversation is skipped and counted; never
   retried with looser validation (TLC-13).

LLM budget: at most one call per active conversation (TLC-21c: ≤ active
convos + 1; the failed-run pass adds zero). Re-running the same night is
safe — `record_proposal` dedups by (kind, payload_hash), reinforcing an
open duplicate instead of inserting a new row (UoW-22 idempotency).

Installed like any imperative skill (UoW-11): this file is authored into
`_drafts/`; the user reviews and moves it into `skills/` to arm the cron.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Any

from ember.learning.digest import AppliedEval, format_applied_since, format_digest
from ember.providers.base import Message

_log = logging.getLogger("ember.skills.retrospective")

name = "retrospective"
description = "Nightly retrospective + weekly approval digest (learning loop v12/v12.5)."
# No tools: the retrospective reads evidence via ctx.learning and calls the
# cheap tier directly. It touches no external side-effect surface (FC-22).
tools: tuple[str, ...] = ()

scheduled_runs = [
    {
        # 02:30 America/Chicago nightly (UoW-22 default). DST-safe via the
        # per-spec timezone. The window is the trailing 24h from ctx.now().
        "cron": "30 2 * * *",
        "timezone": "America/Chicago",
        "function": "retrospective",
        "name": "nightly",
        # One bounded LLM call per active conversation; 10 min covers a busy
        # day's worth of conversations without masking a genuine hang.
        "timeout_s": 600,
        "misfire_grace_s": 3600,
        "coalesce": True,
    },
    {
        # Weekly approval digest (UoW-23). Sunday 16:00 America/Chicago. Expires
        # proposals older than 21 days (silent, TLC-21d), then posts the pending
        # queue to the scheduler channel for `/proposal approve|reject|edit`.
        "cron": "0 16 * * 0",
        "timezone": "America/Chicago",
        "function": "weekly_digest",
        "name": "weekly",
        "timeout_s": 120,
        "misfire_grace_s": 3600,
        "coalesce": True,
    },
]

# Trailing window the run looks back over.
_WINDOW = timedelta(hours=24)
# Proposals unresolved this long are expired by the weekly digest (TLC-21d).
_PROPOSAL_TTL = timedelta(days=21)
# v13 (seam ③): the digest reports the eval delta for changes applied in the
# trailing 7 days — matches the Sunday cadence, tracks no extra state.
_DELTA_WINDOW = timedelta(days=7)
# v13 (C): a live lesson not reinforced in this long becomes a prune candidate
# in the weekly digest (doc #36 § lessons.md).
_LESSON_STALE = timedelta(days=45)

# Friction kinds the extractor may emit; anything else is dropped. `rephrase`
# was retired 2026-07-15 — the cheap tier abused it to log ordinary user
# life-friction ("expressed hunger") as if it were an assistant failure.
_FRICTION_KINDS = {"correction", "tool_failure", "thumbs_down"}
# Structures a `recurring` finding may compile into, mapped to proposal kind.
# "cheapest structure first": trigger < skill < lesson (lesson is the capped
# residual for what can't compile into structure). The `profile` structure was
# retired 2026-07-15 — the retrospective no longer proposes user-profile facts.
_STRUCTURE_TO_KIND = {
    "trigger": "trigger",
    "skill": "skill",
    "lesson": "lesson",
}

_EXTRACTION_SYSTEM = """\
You review ONE conversation's USER messages (each tagged with a numeric id) and
extract ONLY signal about how the ASSISTANT should improve — where it fell
short, and what capability it is missing. You are given ONLY the user's own
messages — never the assistant's replies. Base every finding solely on what the
user actually wrote.

You are NOT building a profile of the user. Do NOT record facts about the
user's life, body, health, diet, pets, home, possessions, tastes, finances, or
personality. Those are noise here and will be discarded. You are looking for
exactly two things:

1. friction_events — a specific place the ASSISTANT failed the user in THIS
   conversation: it gave a wrong answer and got corrected, it gave no answer or
   an incomplete one, it lacked information/context/visibility it should have
   had, or a tool or scheduled task it relies on errored. The user simply
   having a hard time in their own life (being hungry, stressed, a broken
   appliance) is NOT assistant friction. If the exchange went fine, there is no
   friction — do not invent it from an ordinary question.

2. recurring — a request or need that RECURS and reveals a missing capability
   the assistant should grow (a stable trigger phrase, a new skill, or a
   durable behavioral rule). Only flag it if it genuinely recurs AND would
   concretely change how the assistant handles that need next time.

Be extremely conservative. MOST conversations contain NOTHING worth recording,
and empty lists are the correct, expected, common answer. When in doubt, omit.
Noise is what makes the user distrust and ignore this whole system.

Return STRICT JSON (no prose, no code fences) with exactly this shape:

{
  "friction_events": [
    {"kind": "correction" | "tool_failure" | "thumbs_down",
     "summary": "<how the ASSISTANT fell short, concretely>",
     "evidence_message_ids": [<ids of the user messages showing it>]}
  ],
  "recurring": [
    {"pattern": "<a recurring need the assistant should handle better>",
     "occurrences": <int>,
     "proposed_structure": "trigger" | "skill" | "lesson",
     "evidence_message_ids": [<ids of the user messages exhibiting it>]}
  ]
}

Rules:
- Every id in every evidence_message_ids MUST be one of the ids provided below.
  Never invent an id. A finding with no supporting user-message id is invalid —
  omit it.
- friction_events kinds are ONLY: "correction" (user corrected a wrong
  assistant answer), "tool_failure" (a tool or task the assistant relies on
  failed), "thumbs_down" (the user reacted 👎). Nothing else.
- recurring: prefer the cheapest structure — a stable phrasing is a trigger, a
  request needing logic is a skill, a durable behavioral rule is a lesson.
- Report assistant failures and missing capabilities ONLY. If you are tempted
  to write "the user is / has / likes / owns / weighs …", STOP — that is exactly
  the profile noise this system must never produce.
"""


# Bash is the one tool whose failures span unrelated causes, so its finding
# key includes the leading program (`python3`, `pip`, `curl`, …) after any
# `cd … &&`, `VAR=… ` or `sudo`/`env` prefix. Other tools key on name alone.
_SHELL_PREFIX_RE = re.compile(
    r"^(?:cd\s+\S+\s*(?:&&|;)\s*|[A-Za-z_][A-Za-z0-9_]*=\S*\s+|sudo\s+|env\s+)*"
)


def _tool_error_signature(tool: str, args: dict[str, Any]) -> str | None:
    """Coarse, deterministic sub-key for a tool error (part of the payload,
    hence of the dedup hash): the leading program of a Bash command, None
    for every other tool."""
    if tool != "Bash":
        return None
    cmd = _SHELL_PREFIX_RE.sub("", str(args.get("command") or "").strip())
    head = cmd.split(None, 1)[0] if cmd else ""
    head = head.rsplit("/", 1)[-1].strip("\"'")
    return head[:40] or None


async def retrospective(ctx: Any) -> dict[str, Any]:
    """Nightly run entrypoint (scheduled function `retrospective`)."""
    learning = getattr(ctx, "learning", None)
    if learning is None:
        # No store wired (bare/test scheduler path). Nothing to do.
        return {"status": "no_learning_context"}

    now = ctx.now()
    since = now - _WINDOW
    until = now

    result: dict[str, Any] = {
        "window_start": since.isoformat(),
        "window_end": until.isoformat(),
        "active_conversations": 0,
        "extractions_ok": 0,
        "conversations_skipped": 0,
        "proposals_created": 0,
        "proposals_reinforced": 0,
        "lessons_reinforced": 0,
        "llm_calls": 0,
        "failed_runs_seen": 0,
        "tool_errors_seen": 0,
        "by_kind": {},
    }

    def _tally(kind_written: tuple[int, bool] | None, kind: str) -> None:
        if kind_written is None:
            return
        _pid, reinforced = kind_written
        if reinforced:
            result["proposals_reinforced"] += 1
        else:
            result["proposals_created"] += 1
            result["by_kind"][kind] = result["by_kind"].get(kind, 0) + 1

    def _note_lesson_reinforced() -> None:
        # v13 (C): a re-derived lesson matched a LIVE lesson row (not a pending
        # proposal) — its freshness was bumped instead of proposing a duplicate.
        result["lessons_reinforced"] += 1

    # --- 1. Failed scheduled runs → finding proposals (deterministic) -------
    try:
        failed = await learning.failed_runs(since, until)
    except Exception:
        _log.exception("retrospective: failed_runs read errored")
        failed = []
    result["failed_runs_seen"] = len(failed)
    for run in failed:
        # Payload excludes the specific run id so repeated failures of the
        # same job across nights dedup into one reinforced finding; the run
        # id lives in evidence, which unions on reinforcement.
        payload = {
            "type": "scheduled_failure",
            "skill": run.skill,
            "function": run.function,
            "run_name": run.run_name,
            "status": run.status,
        }
        summary = f"scheduled run {run.skill}:{run.run_name} → {run.status}"
        try:
            written = await learning.record_proposal(
                kind="finding",
                summary=summary,
                payload=payload,
                evidence={"runs": [run.id]},
            )
            _tally(written, "finding")
        except Exception:
            _log.exception("retrospective: record finding for run %s failed", run.id)

    # --- 1b. Silent tool errors → finding proposals (deterministic) ---------
    try:
        tool_errors = await learning.tool_errors(since, until)
    except Exception:
        _log.exception("retrospective: tool_errors read errored")
        tool_errors = []
    result["tool_errors_seen"] = len(tool_errors)
    grouped: dict[tuple[str, str | None], list[Any]] = {}
    for te in tool_errors:
        grouped.setdefault((te.tool, _tool_error_signature(te.tool, te.args)), []).append(te)
    for (tool, signature), rows in grouped.items():
        # Evidence = the user turns that preceded the failing calls. A call
        # with no earlier user turn (bare scheduled context) has nothing
        # evidence-eligible to cite and is skipped rather than faked.
        evidence_ids = sorted({r.user_message_id for r in rows if r.user_message_id is not None})
        if not evidence_ids:
            continue
        # Payload keyed on tool (+ Bash program) only, so the same tool
        # failing on successive nights reinforces one finding; the count in
        # the summary is the first night's, `reinforced_count` carries the
        # recurrence.
        payload = {"type": "tool_error", "tool": tool, "signature": signature}
        label = f"{tool} {signature}" if signature else tool
        summary = (
            f"silent tool error: `{label}` failed {len(rows)}× in the window "
            "(no user complaint — from tool status alone)"
        )
        try:
            written = await learning.record_proposal(
                kind="finding",
                summary=summary,
                payload=payload,
                evidence={"messages": evidence_ids},
            )
            _tally(written, "finding")
        except Exception:
            _log.exception("retrospective: record finding for tool error %s failed", label)

    # --- 2. Per-conversation extraction -------------------------------------
    try:
        convos = await learning.active_conversations(since)
    except Exception:
        _log.exception("retrospective: active_conversations read errored")
        convos = []
    result["active_conversations"] = len(convos)

    # Feedback for the window, grouped by conversation (used as 👎 context and
    # as evidence for thumbs_down findings — feedback ids, not message ids,
    # since a 👎 sits on an agent message which is never message-evidence).
    try:
        all_feedback = await learning.feedback(since, until)
    except Exception:
        _log.exception("retrospective: feedback read errored")
        all_feedback = []
    neg_by_convo: dict[int, list[int]] = {}
    for fb in all_feedback:
        if fb.sentiment == -1:
            neg_by_convo.setdefault(fb.conversation_id, []).append(fb.id)

    for convo in convos:
        try:
            user_msgs = await learning.user_messages(convo.id, since, until)
        except Exception:
            _log.exception("retrospective: user_messages read errored for %s", convo.id)
            result["conversations_skipped"] += 1
            continue
        neg_feedback_ids = neg_by_convo.get(convo.id, [])
        if not user_msgs and not neg_feedback_ids:
            # No user-authored signal at all — nothing eligible to learn from.
            continue

        valid_ids = {m.id for m in user_msgs}
        extraction = await _extract(ctx, user_msgs, neg_feedback_ids)
        result["llm_calls"] += 1
        if extraction is None:
            result["conversations_skipped"] += 1
            continue
        result["extractions_ok"] += 1

        await _route(
            learning, extraction, valid_ids, neg_feedback_ids, _tally, now, _note_lesson_reinforced
        )

    return result


async def weekly_digest(ctx: Any) -> dict[str, Any]:
    """Weekly approval digest (UoW-23, scheduled function `weekly_digest`).

    Deterministic, LLM-free (TLC-21c: spends zero of its budgeted +1 call):
    expire proposals older than 21 days (silent, TLC-21d), then post the
    pending queue to the scheduler channel via `ctx.notify`. An empty queue
    still posts one line and stops (UoW-23). Resolution itself is the
    `/proposal` slash command, not this run — the digest only surfaces work.
    """
    learning = getattr(ctx, "learning", None)
    if learning is None:
        return {"status": "no_learning_context"}

    now = ctx.now()
    cutoff = now - _PROPOSAL_TTL

    expired: list[int] = []
    try:
        expired = await learning.expire_stale_proposals(older_than=cutoff, now=now)
    except Exception:
        _log.exception("weekly_digest: expiry pass failed")

    # v13 (C): live lessons unreinforced for >45d become `lesson` prune proposals
    # (payload {op:'prune', lesson_id}). Approval-gated like every mutation — no
    # auto-prune. The lesson's original provenance is carried forward so TLC-20
    # holds; a lesson with no evidence to carry is skipped (rare).
    prune_created = 0
    try:
        stale = await learning.stale_lessons(older_than=now - _LESSON_STALE)
        for lesson in stale:
            try:
                ev = json.loads(lesson.evidence_json)
            except (TypeError, ValueError):
                ev = {}
            if not isinstance(ev, dict) or not (
                ev.get("messages") or ev.get("feedback") or ev.get("runs")
            ):
                _log.info("weekly_digest: stale lesson #%s has no evidence; skip prune", lesson.id)
                continue
            try:
                _pid, reinforced = await learning.record_proposal(
                    kind="lesson",
                    summary=f"🗑️ prune stale lesson: {lesson.text[:70]}",
                    payload={"op": "prune", "lesson_id": lesson.id, "lesson_text": lesson.text},
                    evidence=ev,
                )
                if not reinforced:
                    prune_created += 1
            except Exception:
                _log.exception("weekly_digest: prune proposal for lesson #%s failed", lesson.id)
    except Exception:
        _log.exception("weekly_digest: stale-lesson scan failed")

    try:
        pending = await learning.pending_proposals()
    except Exception:
        _log.exception("weekly_digest: pending read failed")
        pending = []

    # v13 (B): the "📈 Applied since last digest" delta section. For each change
    # applied in the trailing 7 days that carries an auto-eval link, diff its run
    # against the previous comparable run. Deterministic, no LLM, and defensive —
    # a read failure drops the section but never breaks the digest.
    applied_section = ""
    applied_count = 0
    try:
        applied = await learning.applied_proposals_since(now - _DELTA_WINDOW)
        entries: list[AppliedEval] = []
        for p in applied:
            if p.applied_eval_run_id is None:
                continue  # applied without an eval (e.g. no cases / no service)
            run = await learning.eval_run(p.applied_eval_run_id)
            prev = (
                await learning.previous_eval_run(run.id, run.selector) if run is not None else None
            )
            entries.append(
                AppliedEval(proposal_id=p.id, kind=p.kind, summary=p.summary, run=run, prev=prev)
            )
        applied_count = len(entries)
        applied_section = format_applied_since(entries)
    except Exception:
        _log.exception("weekly_digest: applied-delta section failed")

    body = format_digest(pending)
    if applied_section:
        body = body + "\n\n" + applied_section
    posted = False
    try:
        await ctx.notify(body)
        posted = True
    except Exception:
        _log.exception("weekly_digest: notify failed")

    return {
        "expired": len(expired),
        "expired_ids": expired,
        "pending": len(pending),
        "applied_measured": applied_count,
        "prune_candidates": prune_created,
        "posted": posted,
    }


async def _extract(
    ctx: Any, user_msgs: list[Any], neg_feedback_ids: list[int]
) -> dict[str, Any] | None:
    """One bounded cheap-tier extraction. Returns the schema-validated dict,
    or None if the tier is unavailable, the call fails, or the output is
    malformed (→ caller skips + counts; never retried looser, TLC-13)."""
    try:
        resolved = ctx.router.resolve_cheap_tier()
    except Exception as e:
        _log.info("retrospective: cheap tier unavailable: %s", e)
        return None

    lines = [f"[{m.id}] {m.content}" for m in user_msgs]
    transcript = "\n".join(lines) if lines else "(no user messages this window)"
    note = ""
    if neg_feedback_ids:
        note = (
            f"\n\nThe user reacted 👎 to {len(neg_feedback_ids)} assistant "
            "message(s) in this window — treat that as a thumbs_down friction "
            "signal even though the reacted message text is not shown."
        )
    user_content = "## User messages (id-tagged)\n\n" + transcript + note

    try:
        completion = await resolved.provider.complete(
            system=_EXTRACTION_SYSTEM,
            messages=[Message(role="user", content=user_content)],
            tools=None,
            model=resolved.model,
            session_id=None,
            stream_handler=None,
        )
    except Exception as e:
        _log.warning("retrospective: extraction call failed: %s: %s", type(e).__name__, e)
        return None

    return _validate_extraction(completion.text)


def _validate_extraction(text: str | None) -> dict[str, Any] | None:
    """Strict schema check. Malformed → None (skip the conversation). Never
    coerced or re-parsed loosely (TLC-13). Individual malformed items inside
    otherwise-valid lists are dropped defensively."""
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        # Defensive fence-strip; the schema still has to validate.
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None

    def _clean_ids(v: Any) -> list[int]:
        if not isinstance(v, list):
            return []
        out: list[int] = []
        for x in v:
            if isinstance(x, bool):
                continue
            if isinstance(x, int):
                out.append(x)
        return out

    friction: list[dict[str, Any]] = []
    for item in obj.get("friction_events", []) or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        summary = item.get("summary")
        ids = _clean_ids(item.get("evidence_message_ids"))
        if kind in _FRICTION_KINDS and isinstance(summary, str) and summary.strip():
            friction.append({"kind": kind, "summary": summary.strip(), "evidence_message_ids": ids})

    recurring: list[dict[str, Any]] = []
    for item in obj.get("recurring", []) or []:
        if not isinstance(item, dict):
            continue
        pattern = item.get("pattern")
        structure = item.get("proposed_structure")
        ids = _clean_ids(item.get("evidence_message_ids"))
        occ = item.get("occurrences")
        if isinstance(pattern, str) and pattern.strip() and structure in _STRUCTURE_TO_KIND and ids:
            recurring.append(
                {
                    "pattern": pattern.strip(),
                    "occurrences": occ if isinstance(occ, int) and not isinstance(occ, bool) else 0,
                    "proposed_structure": structure,
                    "evidence_message_ids": ids,
                }
            )

    return {"friction_events": friction, "recurring": recurring}


async def _route(
    learning: Any,
    extraction: dict[str, Any],
    valid_ids: set[int],
    neg_feedback_ids: list[int],
    tally: Any,
    now: Any,
    note_lesson_reinforced: Any,
) -> None:
    """Route a validated extraction into `proposals`. Every message id is
    intersected with the ids actually fed to the extractor, so a model that
    cites an out-of-window id contributes no false provenance; `record_proposal`
    re-gates against TLC-20 regardless. A finding with no surviving evidence is
    dropped (can't persist without provenance).

    v13 (C): a re-derived `lesson` whose normalized text matches a LIVE lesson
    row bumps that row's freshness (`reinforce_lesson`) instead of proposing a
    duplicate — this is how an applied lesson stays out of the prune window."""

    def _kept(ids: list[int]) -> list[int]:
        return sorted({i for i in ids if i in valid_ids})

    async def _record(
        kind: str, summary: str, payload: dict[str, Any], evidence: dict[str, list[int]]
    ) -> None:
        if not (evidence.get("messages") or evidence.get("feedback") or evidence.get("runs")):
            return
        try:
            written = await learning.record_proposal(
                kind=kind, summary=summary, payload=payload, evidence=evidence
            )
            tally(written, kind)
        except Exception:
            _log.exception("retrospective: record_proposal(%s) failed", kind)

    # recurring → trigger | skill | lesson (profile structure retired 2026-07-15)
    for r in extraction["recurring"]:
        kind = _STRUCTURE_TO_KIND[r["proposed_structure"]]
        ids = _kept(r["evidence_message_ids"])
        # v13 (C): if this lesson is already live, reinforce it instead of
        # proposing a duplicate (keeps applied lessons out of the prune window).
        if kind == "lesson":
            try:
                if await learning.reinforce_lesson(r["pattern"], now=now):
                    note_lesson_reinforced()
                    continue
            except Exception:
                _log.exception("retrospective: reinforce_lesson failed")
        await _record(
            kind,
            f"recurring ({kind}): {r['pattern'][:80]}",
            {
                "type": "recurring",
                "pattern": r["pattern"],
                "occurrences": r["occurrences"],
                "proposed_structure": r["proposed_structure"],
            },
            {"messages": ids},
        )

    # friction_events → finding. thumbs_down uses feedback-id evidence (the
    # reacted message is agent-authored and thus never message-evidence).
    for e in extraction["friction_events"]:
        ids = _kept(e["evidence_message_ids"])
        evidence: dict[str, list[int]] = {}
        if ids:
            evidence["messages"] = ids
        if e["kind"] == "thumbs_down" and neg_feedback_ids:
            evidence["feedback"] = sorted(set(neg_feedback_ids))
        await _record(
            "finding",
            f"friction ({e['kind']}): {e['summary'][:80]}",
            {"type": "friction", "kind": e["kind"], "summary": e["summary"]},
            evidence,
        )
