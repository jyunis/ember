"""Router-mode system-prompt assembly (FC-18).

When `conversations.active_skill IS NULL` (router mode), the agent's
system prompt is augmented with a deterministic "## Available skills"
block enumerating every loaded skill. The block is built from the
registry once per turn and is byte-stable across consecutive turns when
the registry is unchanged (TLC-12).

This module ships a single pure function: `build_skills_block`. It
returns the empty string for an empty registry; callers omit the block
in that case so the system prompt doesn't carry a dangling header.

Per-skill rendering uses only the first line of the description (the
same convention `/skill list` follows) and, when the skill carries
`suggest_when` triggers, appends a `Triggers:` line with the trigger
phrases quoted. Imperative skills (no `suggest_when` field) render
without a triggers line — `getattr(skill, "suggest_when", ())` keeps the
renderer defensive.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember.skills.base import SkillRegistry

if TYPE_CHECKING:
    from ember.tools.registry import Registry


_HEADER = "## Available skills"

_CATALOG_HEADER = "## Your tools"

_CATALOG_GUIDANCE = (
    "These are tools you can call directly in this chat — you DO have "
    "them. Never tell the user you lack an integration when a tool above "
    "covers the request (e.g. `mealie_add_recipe` to save a recipe, "
    "`gmail_send` to send mail, `gmail_draft` to save a draft, "
    "`calendar_list_events` to check the "
    "calendar). To look something up on the web, use `web_search` then "
    "`web_fetch`; reach for `web_browse` only when a page needs JS "
    "rendering. Prefer `use_skill` for multi-step workflows, but for a "
    "single action just call the tool."
)

# Registry tools the router catalog omits: internal plumbing, skill
# authoring, identity, and MCP-mount helpers. These are either covered by
# the routing guidance above or not user-facing actions, so listing them
# would only dilute the catalog. Everything else (mealie/gmail/calendar/
# vault/tiller/schwab/documents/memory/web/...) is advertised.
DEFAULT_CATALOG_EXCLUDE = frozenset(
    {
        "echo",
        "use_skill",
        "skill_read",
        "skill_draft_python",
        "skill_save_manifest",
        "skill_list_known_tools",
        "import_skill",
        "mcp_connect",
        "mcp_disconnect",
        "mcp_list_connected",
        # v15 (FC-30): read_payload is plumbing the stored-output
        # envelope's note points at; the catalog would only dilute.
        "read_payload",
        # doc #59 (FC-36): docs_sync is plumbing behind the scheduled shim
        # skill; still bridge-callable for an on-demand refresh.
        "docs_sync",
        "soul_read",
        "profile_read",
        "profile_update",
    }
)

_ROUTING_GUIDANCE = (
    "Routing guidance:\n"
    "- If the user's request matches a skill, prefer "
    "`use_skill(skill=..., query=...)` over answering directly.\n"
    "- For chitchat, simple questions, memory operations, or scheduling, "
    "answer directly without invoking a skill.\n"
    "- Use `skill_read(name)` (and `skill_read(name, file=...)`) to load "
    "a skill's body and companion files. Use `mcp_connect` to mount any "
    "external MCP a skill declares; check `mcp_list_connected` first."
)


def _first_line(text: str | None) -> str:
    if not text:
        return ""
    return text.splitlines()[0]


def _format_triggers(suggest_when: tuple[str, ...]) -> str:
    """Render a `Triggers:` line. Caller filters out the empty case."""
    quoted = ", ".join(f'"{phrase}"' for phrase in suggest_when)
    return f"  Triggers: {quoted}"


def build_skills_block(skill_registry: SkillRegistry) -> str:
    """Build the `## Available skills` block for router mode.

    Returns the empty string for an empty registry — callers must omit
    the block in that case (no dangling header). Otherwise returns a
    deterministic, name-sorted markdown block listing every loaded skill
    with its first-line description and (when present) a `Triggers:`
    line carrying its `suggest_when` phrases.

    Output is byte-stable across consecutive calls with the same
    registry contents (TLC-12) — no timestamps, no random ids, no
    insertion-order leakage. `InMemorySkillRegistry.list()` is already
    name-sorted, so the renderer relies on that.
    """
    skills = list(skill_registry.list())
    if not skills:
        return ""

    lines: list[str] = [_HEADER, ""]
    for skill in skills:
        desc = _first_line(getattr(skill, "description", "") or "")
        if desc:
            lines.append(f"- `{skill.name}` — {desc}")
        else:
            lines.append(f"- `{skill.name}`")
        suggest_when = tuple(getattr(skill, "suggest_when", ()) or ())
        if suggest_when:
            lines.append(_format_triggers(suggest_when))

    lines.append("")
    lines.append(_ROUTING_GUIDANCE)
    return "\n".join(lines)


def build_tools_catalog_block(
    tool_registry: "Registry",
    exclude: frozenset[str] = DEFAULT_CATALOG_EXCLUDE,
) -> str:
    """Build the `## Your tools` catalog block for router mode.

    Lists every registered tool (name + first-line description) except
    those in `exclude`, so the model knows its full action surface. The
    router prompt otherwise advertises only *skills*, which left the
    model denying it had integrations it actually has (mealie, gmail,
    calendar, …) — it would scan what it had been told it could do, not
    the live tool list.

    Name-sorted for byte-stability (TLC-12). Returns "" when nothing
    resolves so the caller can omit a dangling header.
    """
    rendered: list[str] = []
    for tool in sorted(tool_registry.list_tools(), key=lambda t: t.name):
        if tool.name in exclude:
            continue
        desc = _first_line(getattr(tool, "description", "") or "")
        rendered.append(f"- `{tool.name}` — {desc}" if desc else f"- `{tool.name}`")
    if not rendered:
        return ""
    return "\n".join([_CATALOG_HEADER, "", *rendered, "", _CATALOG_GUIDANCE])
