"""Router prompt assembly — pure function (FC-18).

When `conversations.active_skill IS NULL` (router mode), the agent's
system prompt is augmented with a deterministic "## Available skills"
block built from the registry. This file tests the assembly function
*in isolation* — no Agent, no Store, no Provider. The implementer is
expected to factor a small pure builder, e.g.:

    def build_skills_block(skill_registry: SkillRegistry) -> str: ...

We import it lazily so collection succeeds even when the implementer
hasn't shipped it yet.

Module-path guess: `ember.skills.router_prompt`. Other plausible
landings: `ember.agent_routing`, `ember.prompts.router`,
`ember.skills.prompts`. **Flagged in the report** — implementer may
need to adjust either the implementation module name or this import.

Spec ambiguities baked into assumptions (flagged in report):

  - Empty registry: spec says the block is "omitted" entirely. We
    assert the builder returns the empty string `""`. If the
    implementer chose to return `None`, this test will fail and
    surface the gap.

  - For declarative skills with no `suggest_when`, the line should
    not include a "Triggers:" suffix.

  - Imperative skills don't carry `suggest_when` (see `skills/base.py`)
    so the renderer needs to handle that absence cleanly. We assert
    they render without raising and without a triggers section.

  - Routing guidance: spec example uses "use_skill" and "If the
    user's request matches a skill". We don't pin exact wording — we
    only assert that one of those substrings is present somewhere in
    the block.
"""

from __future__ import annotations


from ember.skills.base import DeclarativeSkill, ImperativeSkill
from ember.skills.registry import InMemorySkillRegistry


# --- Lazy import -------------------------------------------------------------


def _import_build_skills_block():
    """Import the implementer's pure builder.

    Module-path guess: `ember.skills.router_prompt`.
    Function-name guess: `build_skills_block`.

    Lazy import means a clean ModuleNotFoundError at the baseline and
    a useful failure when the implementer ships under a different name.
    """
    from ember.skills.router_prompt import build_skills_block

    return build_skills_block


# --- Static skill builders ---------------------------------------------------


def _decl(
    name: str,
    description: str,
    *,
    tools: tuple[str, ...] = (),
    system_prompt: str | None = None,
    suggest_when: tuple[str, ...] = (),
) -> DeclarativeSkill:
    return DeclarativeSkill(
        name=name,
        description=description,
        tools=tools,
        system_prompt=system_prompt,
        suggest_when=suggest_when,
    )


def _imp(
    name: str,
    description: str,
    *,
    tools: tuple[str, ...] = (),
    system_prompt: str | None = None,
) -> ImperativeSkill:
    return ImperativeSkill(
        name=name,
        description=description,
        tools=tools,
        system_prompt=system_prompt,
        scheduled_runs=(),
        functions={},
    )


# --- Empty registry ----------------------------------------------------------


class TestEmptyRegistry:
    def test_empty_registry_returns_empty_string(self) -> None:
        """Spec: empty registry → block is omitted. Builder returns "".

        ASSUMPTION: empty string. Flagged in report — implementer may
        instead return None. The integration round-trip in
        test_router_prompt_in_agent_loop.py covers the observable
        ("## Available skills" not in system prompt) regardless.
        """
        build = _import_build_skills_block()
        sr = InMemorySkillRegistry()
        out = build(sr)
        assert out == ""


# --- Single skill ------------------------------------------------------------


class TestSingleSkill:
    def test_block_starts_with_header(self) -> None:
        build = _import_build_skills_block()
        sr = InMemorySkillRegistry()
        sr.replace(_decl("alpha", "Alpha description first line."))
        out = build(sr)
        # The block always starts with the literal header.
        assert out.startswith("## Available skills")

    def test_block_contains_skill_name_and_description(self) -> None:
        build = _import_build_skills_block()
        sr = InMemorySkillRegistry()
        sr.replace(_decl("alpha", "Alpha first line.\nNot the first line."))
        out = build(sr)
        assert "alpha" in out
        # Only the first line of the description is rendered.
        assert "Alpha first line." in out
        # Subsequent description lines are dropped from the block.
        assert "Not the first line." not in out

    def test_block_contains_routing_guidance(self) -> None:
        """Spec example references `use_skill` + a "matches a skill"
        sentence. We don't pin exact wording — just assert one of those
        substrings shows up somewhere."""
        build = _import_build_skills_block()
        sr = InMemorySkillRegistry()
        sr.replace(_decl("alpha", "alpha desc"))
        out = build(sr)
        lowered = out.lower()
        assert "use_skill" in lowered or "if the user's request matches a skill" in lowered


# --- Multi-skill ordering ----------------------------------------------------


class TestSortedOrder:
    def test_skills_appear_in_name_sorted_order(self) -> None:
        """Insertion order does NOT determine output order; name does
        (TLC-12 + matches `InMemorySkillRegistry.list()` semantics)."""
        build = _import_build_skills_block()
        sr = InMemorySkillRegistry()
        sr.replace(_decl("zeta", "zeta desc"))
        sr.replace(_decl("alpha", "alpha desc"))
        sr.replace(_decl("mid", "mid desc"))
        out = build(sr)
        i_alpha = out.index("alpha")
        i_mid = out.index("mid")
        i_zeta = out.index("zeta")
        assert i_alpha < i_mid < i_zeta


# --- suggest_when triggers ---------------------------------------------------


class TestTriggers:
    def test_suggest_when_renders_triggers_inline(self) -> None:
        """Spec example:
            - `crypto_wallet` — Query ... Triggers: "wallet balances", ...
        We assert the literal "Triggers:" marker plus each trigger
        phrase appears, but don't pin punctuation/quoting beyond that.
        """
        build = _import_build_skills_block()
        sr = InMemorySkillRegistry()
        sr.replace(
            _decl(
                "crypto_wallet",
                "Query the wallet.",
                suggest_when=("wallet balances", "my crypto"),
            )
        )
        out = build(sr)
        assert "Triggers:" in out
        assert "wallet balances" in out
        assert "my crypto" in out

    def test_no_suggest_when_means_no_triggers_section(self) -> None:
        """Declarative skill without suggest_when must NOT emit a
        `Triggers:` line for that entry."""
        build = _import_build_skills_block()
        sr = InMemorySkillRegistry()
        sr.replace(_decl("alpha", "alpha desc"))  # no suggest_when
        out = build(sr)
        # Header + skill rendered without `Triggers:`.
        assert "alpha" in out
        # The whole block should not have a Triggers: line at all,
        # since the only skill has no triggers.
        assert "Triggers:" not in out

    def test_imperative_skill_renders_without_suggest_when_field(self) -> None:
        """ImperativeSkill has no `suggest_when` field at all
        (see skills/base.py). The renderer must use defensive lookup
        (e.g. `getattr(skill, 'suggest_when', ())`) and not crash."""
        build = _import_build_skills_block()
        sr = InMemorySkillRegistry()
        sr.replace(_imp("imper", "imperative description first line."))
        # Should not raise.
        out = build(sr)
        assert "imper" in out
        # Imperative skills carry no triggers, so this entry shouldn't
        # produce a Triggers: section.
        # (We assert at the registry level: no Triggers: appears for
        # *this* skill — easier to verify by absence overall when it's
        # the only skill.)
        assert "Triggers:" not in out


# --- Determinism (TLC-12) ----------------------------------------------------


class TestDeterminism:
    def test_byte_identical_across_consecutive_calls(self) -> None:
        """TLC-12: same registry → byte-identical output across calls."""
        build = _import_build_skills_block()
        sr = InMemorySkillRegistry()
        sr.replace(_decl("alpha", "alpha desc", suggest_when=("a phrase", "another")))
        sr.replace(_decl("beta", "beta desc"))
        sr.replace(_imp("gamma", "gamma desc"))
        out1 = build(sr)
        out2 = build(sr)
        assert out1 == out2

    def test_no_timestamps_or_random_ids(self) -> None:
        """TLC-12 spirit: the block should not embed timestamps. Build
        twice with a small wait between, assert they remain equal."""
        import time

        build = _import_build_skills_block()
        sr = InMemorySkillRegistry()
        sr.replace(_decl("alpha", "alpha desc"))
        out1 = build(sr)
        time.sleep(0.01)
        out2 = build(sr)
        assert out1 == out2


# --- Multi-skill, multi-flavor end-to-end ------------------------------------


class TestMixedRegistry:
    def test_renders_mixed_imperative_and_declarative(self) -> None:
        """Both kinds in the same registry render in name-sorted order
        without raising and without leaking implementation details."""
        build = _import_build_skills_block()
        sr = InMemorySkillRegistry()
        sr.replace(
            _decl(
                "news_brief",
                "Fetch and synthesize a news briefing.",
                suggest_when=("news brief", "morning brief"),
            )
        )
        sr.replace(_imp("scheduler_demo", "An imperative skill."))
        out = build(sr)
        # Both names present.
        assert "news_brief" in out
        assert "scheduler_demo" in out
        # Sorted by name: news_brief < scheduler_demo.
        assert out.index("news_brief") < out.index("scheduler_demo")
        # Triggers come from declarative one only.
        assert "morning brief" in out
