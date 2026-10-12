"""Weekly approval digest formatting (UoW-23).

Pure, deterministic rendering of the pending-proposal queue for the scheduler
channel. No clock, no LLM (TLC-21c budgets one optional digest-assembly call;
v12.5 spends zero — deterministic assembly is testable and cheap, and the
`reinforced_count` surfaced per line already exposes the cheap-tier
near-duplicates the digest was meant to help triage). If richer semantic
clustering is ever wanted, it slots in as the reserved "+1" call ahead of this
formatter; this function stays the deterministic fallback.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ember.store.types import EvalRun, Proposal

# Kind → (emoji, human label), in the order the digest groups them:
# cheapest-structure-first, findings (informational) last.
_KIND_ORDER: list[tuple[str, str, str]] = [
    ("profile", "👤", "Profile"),
    ("trigger", "🎯", "Triggers"),
    ("skill", "🛠️", "Skills"),
    ("lesson", "📘", "Lessons"),
    ("finding", "🔎", "Findings"),
]

_EMPTY = "🗒️ Weekly digest: no pending proposals. Nothing to review."

# Per-line text cap. The stored `summary` is truncated to 80 chars at
# accumulation (a label), but the FULL text lives in `payload_json`; the digest
# renders that so the reviewer can actually read the suggestion. Capped only to
# keep a large queue under Discord's message limit.
_TEXT_CAP = 300


def _full_text(proposal: "Proposal") -> str:
    """The proposal's full human text, pulled from the payload (`fact` /
    `pattern` / `summary` / `text`) rather than the 80-char `summary` label, so
    the digest shows the actual suggestion (not a truncated fragment). Falls
    back to `summary` for payloads with no prose field (e.g. scheduled-failure
    findings)."""
    try:
        payload = json.loads(proposal.payload_json)
    except (TypeError, ValueError):
        payload = {}
    text = proposal.summary
    if isinstance(payload, dict):
        for key in ("fact", "pattern", "summary", "text"):
            v = payload.get(key)
            if isinstance(v, str) and v.strip():
                text = v.strip()
                break
    text = " ".join(text.split())  # collapse newlines/whitespace for one line
    if len(text) > _TEXT_CAP:
        text = text[: _TEXT_CAP - 1].rstrip() + "…"
    return text


def _evidence_oneliner(proposal: "Proposal") -> str:
    """A compact evidence pointer for one proposal line (TLC-20 provenance,
    surfaced so the reviewer can trace any proposal to its source)."""
    try:
        ev = json.loads(proposal.evidence_json)
    except (TypeError, ValueError):
        ev = {}
    parts: list[str] = []
    for label, key in (("msgs", "messages"), ("feedback", "feedback"), ("runs", "runs")):
        ids = ev.get(key) or []
        if ids:
            shown = ", ".join(str(i) for i in ids[:4])
            more = "…" if len(ids) > 4 else ""
            parts.append(f"{label} {shown}{more}")
    return "; ".join(parts) if parts else "no evidence"


def format_digest(pending: list["Proposal"]) -> str:
    """Render the weekly digest body. Empty queue → a single line. Otherwise a
    header plus, grouped by kind, one numbered line per proposal carrying its
    id, summary, evidence one-liner, and (when >0) reinforcement count.

    Deterministic: same input → same output (proposals are rendered in the
    order given within each kind group; the caller passes newest-first from
    `list_proposals`)."""
    if not pending:
        return _EMPTY

    by_kind: dict[str, list[Proposal]] = {}
    for p in pending:
        by_kind.setdefault(p.kind, []).append(p)

    n = len(pending)
    lines = [f"🗒️ **Weekly digest** — {n} pending proposal{'s' if n != 1 else ''} to review:"]
    lines.append("Resolve with `/proposal approve <id>` · `reject <id>` · `edit <id> <text>`.")

    idx = 0
    for kind, _emoji, label in _KIND_ORDER:
        group = by_kind.get(kind)
        if not group:
            continue
        lines.append("")
        lines.append(f"{_emoji} __{label}__")
        for p in group:
            idx += 1
            rc = f" · reinforced {p.reinforced_count}×" if p.reinforced_count else ""
            lines.append(
                f"{idx}. #{p.id} — {_full_text(p)}  _(evidence: {_evidence_oneliner(p)}{rc})_"
            )

    # Any unknown kinds (schema is CHECK-constrained, so this is defensive).
    known = {k for k, _e, _l in _KIND_ORDER}
    for kind, group in by_kind.items():
        if kind in known:
            continue
        lines.append("")
        lines.append(f"• __{kind}__")
        for p in group:
            idx += 1
            lines.append(f"{idx}. #{p.id} — {_full_text(p)}")

    # Bulk-reject hint. Discord options are named, so these are field values,
    # not positional args: `action:reject-all` clears everything in one pick;
    # `action:reject id:profile` clears a kind; `action:reject id:3,5,7` a list.
    lines.append("")
    lines.append(
        "_Bulk clear: `action:reject-all` · a kind: `action:reject id:profile` · "
        "a list: `action:reject id:3,5,7`._"
    )

    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class AppliedEval:
    """One applied proposal + its linked eval run + the previous comparable run
    (same selector). Assembled by the weekly digest (which owns DB access);
    `format_applied_since` renders it deterministically with no further reads."""

    proposal_id: int
    kind: str
    summary: str
    run: "EvalRun | None"
    prev: "EvalRun | None"


def _run_delta(prev: "EvalRun", cur: "EvalRun") -> str:
    """Pass/fail/error deltas of `cur` vs `prev` + which fingerprint axes moved
    (mirrors `EvalCommand._delta`, kept self-contained so the digest is pure)."""

    def d(a: int, b: int) -> str:
        n = b - a
        return f"{n:+d}" if n else "±0"

    changed = []
    if prev.identity_sha != cur.identity_sha:
        changed.append("identity")
    if prev.skills_hash != cur.skills_hash:
        changed.append("skills")
    if prev.provider_route != cur.provider_route:
        changed.append("route")
    if prev.image_version != cur.image_version:
        changed.append("image")
    what = ", ".join(changed) if changed else "nothing (same fingerprint axes)"
    return (
        f"run #{cur.id} vs #{prev.id}: pass {d(prev.passed, cur.passed)} · "
        f"fail {d(prev.failed, cur.failed)} · error {d(prev.errored, cur.errored)} "
        f"— changed: {what}"
    )


def format_applied_since(entries: list[AppliedEval]) -> str:
    """The v13 '📈 Applied since last digest' section (B). Deterministic, no
    LLM, no DB. Empty input → "" (the caller omits the section). Per applied
    change: the before/after eval delta, or 'measuring…' if the run is still in
    flight, 'errored' if every case errored, or 'baseline' when there's no prior
    comparable run to diff against."""
    if not entries:
        return ""
    lines = ["📈 **Applied since last digest** — measured change:"]
    for e in entries:
        head = f"#{e.proposal_id} ({e.kind}) — {e.summary}"
        run = e.run
        if run is None:
            lines.append(f"• {head}: _eval link missing._")
        elif run.finished_at is None:
            lines.append(f"• {head}: ⏳ measuring… (run #{run.id} in flight).")
        elif run.total_cases and run.errored == run.total_cases:
            lines.append(f"• {head}: ⚠️ eval errored (run #{run.id}); no delta.")
        elif e.prev is None:
            lines.append(
                f"• {head}: run #{run.id} {run.passed}✅ {run.failed}❌ {run.errored}⚠️ "
                "_(baseline — no prior run of this selector to diff)._"
            )
        else:
            lines.append(f"• {head}: {_run_delta(e.prev, run)}")
    return "\n".join(lines)
