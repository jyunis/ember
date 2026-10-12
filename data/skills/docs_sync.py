"""docs_sync — scheduled shim that refreshes the documents index (doc #59 UoW-33).

Committed under `scripts/skills/` and copied into `/data/skills/` at ship.
The body is a single `ctx.tools.call_tool("docs_sync", {})` — no LLM, no
`ctx.router` (TLC-29). Raises on an error result so the UoW-5 run records
`error` and the scheduler channel is notified once per fire; when the
docs tools are unregistered (no PAPERLESS_API_TOKEN) the call itself
fails and the same path applies.
"""

from __future__ import annotations

name = "docs_sync"
description = (
    "Every 15 minutes, mirrors new/changed paperless documents into Ember's "
    "searchable documents index (docs_search) and drops trashed/excluded ones."
)
tools = ["docs_sync"]
scheduled_runs = [
    {
        "cron": "*/15 * * * *",
        "timezone": "America/Chicago",
        "function": "sync",
        "name": "sync",
        "timeout_s": 240,
        "misfire_grace_s": 600,
        "coalesce": True,
    }
]


async def sync(ctx) -> dict:
    result = await ctx.tools.call_tool("docs_sync", {})
    if result.is_error:
        raise RuntimeError(str(result.output))
    return result.output
