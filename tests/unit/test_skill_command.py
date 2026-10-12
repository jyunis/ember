"""SkillCommand (`/skill`) — FC-12, UoW-9 (v7).

Per the brief + UoW-9:
- `/skill list` shows one line per loaded skill: `name | description
  first line | tools count | [active]` if active in this conversation.
  Works in any context.
- `/skill set target:<name>` validates the skill exists, then calls
  `Store.set_active_skill(conversation_id, skill_name)`. Returns
  confirmation including the system prompt's first line + tool count.
  If `<name>` doesn't exist → user-facing error message (no DB write).
  Conversation-only.
- `/skill clear` sets `active_skill` to NULL on this conversation row.
  Conversation-only.

The implementer's `SkillCommand.__init__` takes `(skill_registry, store)`.

Spec ambiguities flagged in report:
1. Whether `/skill list` shows a `[scheduled]` marker for imperative
   skills with `scheduled_runs`. The brief lists fields as `name |
   description first line | tools count | [active]` only; scheduled
   marker is not in the list. We assume **no** scheduled marker. If
   the implementer adds one, that test below will be wrong; flip then.
2. Whether `Store.set_active_skill` needs a separate flag-on-Conversation
   read first. We just assert the column updates after `set` runs.

We don't read implementation files for `commands/skill.py`, but we DO
exercise it through its public Protocol surface (name, description,
options, run).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from ember.commands.base import CommandContext
from ember.commands.registry import CommandRegistry
from ember.config import Settings
from ember.scheduler.base import ScheduleSpec
from ember.skills.base import DeclarativeSkill, ImperativeSkill
from ember.store.repo import Store


def _import_skill_command():
    from ember.commands.skill import SkillCommand

    return SkillCommand


# --- Fakes ----------------------------------------------------------------


async def _coro(_ctx: Any) -> None:
    return None


class FakeSkillRegistry:
    """Minimal SkillRegistry surface used by SkillCommand tests."""

    def __init__(self) -> None:
        self._skills: dict[str, ImperativeSkill | DeclarativeSkill] = {}

    def add(self, skill: ImperativeSkill | DeclarativeSkill) -> None:
        self._skills[skill.name] = skill

    # SkillRegistry protocol surface ----------------------------------------

    def get(self, name: str) -> ImperativeSkill | DeclarativeSkill:
        return self._skills[name]

    def list(self) -> list[ImperativeSkill | DeclarativeSkill]:
        return [self._skills[n] for n in sorted(self._skills)]

    def names(self) -> list[str]:
        return sorted(self._skills)

    def replace(self, skill: ImperativeSkill | DeclarativeSkill) -> None:
        self._skills[skill.name] = skill

    def remove(self, name: str) -> None:
        self._skills.pop(name, None)

    # SkillSource protocol surface (some impls might check) -----------------

    def list_schedules(self) -> list:
        return []

    def on_change(self, callback: Callable[[], None]) -> None:
        return None


def _make_imperative(
    name: str,
    *,
    description: str = "stub",
    tools: tuple[str, ...] = (),
    system_prompt: str | None = None,
    schedules: list[ScheduleSpec] | None = None,
) -> ImperativeSkill:
    schedules = schedules or []
    fns: dict[str, Callable[..., Awaitable[Any]]] = {}
    for spec in schedules:
        fns.setdefault(spec.function, _coro)
    return ImperativeSkill(
        name=name,
        description=description,
        tools=tools,
        system_prompt=system_prompt,
        scheduled_runs=tuple(schedules),
        functions=fns,
    )


def _make_declarative(
    name: str,
    *,
    description: str = "stub",
    tools: tuple[str, ...] = (),
    system_prompt: str | None = None,
) -> DeclarativeSkill:
    return DeclarativeSkill(
        name=name,
        description=description,
        tools=tools,
        system_prompt=system_prompt,
    )


# --- Settings + wired fixture --------------------------------------------


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
async def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = _make_settings(monkeypatch, tmp_path)
    store = Store(tmp_path / "conversations.db")
    await store.open()
    registry = CommandRegistry()
    try:
        yield store, settings, registry
    finally:
        await store.close()


def _ctx(
    store: Store,
    settings: Settings,
    registry: CommandRegistry,
    *,
    conversation_key: str | None = "thread-1",
    args: dict | None = None,
) -> CommandContext:
    return CommandContext(
        conversation_key=conversation_key,
        user_id="1000",
        store=store,
        settings=settings,
        registry=registry,
        args=args or {},
    )


# --- Static surface ------------------------------------------------------


class TestStaticSurface:
    def test_name_is_skill(self) -> None:
        SkillCommand = _import_skill_command()
        cmd = SkillCommand(skill_registry=FakeSkillRegistry(), store=None)  # type: ignore[arg-type]
        assert cmd.name == "skill"

    def test_action_option_has_choices_and_default(self) -> None:
        SkillCommand = _import_skill_command()
        cmd = SkillCommand(skill_registry=FakeSkillRegistry(), store=None)  # type: ignore[arg-type]
        opts = {o.name: o for o in cmd.options}
        assert "action" in opts
        assert opts["action"].choices is not None
        assert set(opts["action"].choices) == {"list", "set", "clear"}
        assert opts["action"].default == "list"

    def test_target_option_present(self) -> None:
        SkillCommand = _import_skill_command()
        cmd = SkillCommand(skill_registry=FakeSkillRegistry(), store=None)  # type: ignore[arg-type]
        opts = {o.name: o for o in cmd.options}
        assert "target" in opts


# --- /skill list ---------------------------------------------------------


class TestActionList:
    async def test_list_works_outside_conversation(self, wired) -> None:
        # Brief: `/skill list` works in any context.
        store, settings, registry = wired
        skill_registry = FakeSkillRegistry()
        skill_registry.add(_make_declarative("alpha", description="first"))

        SkillCommand = _import_skill_command()
        cmd = SkillCommand(skill_registry=skill_registry, store=store)
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                conversation_key=None,
                args={"action": "list", "target": ""},
            )
        )
        assert isinstance(out, str)
        assert "alpha" in out

    async def test_list_includes_each_skill_name(self, wired) -> None:
        store, settings, registry = wired
        skill_registry = FakeSkillRegistry()
        skill_registry.add(_make_imperative("imp_skill", description="imp"))
        skill_registry.add(_make_declarative("dec_skill", description="dec"))

        SkillCommand = _import_skill_command()
        cmd = SkillCommand(skill_registry=skill_registry, store=store)
        out = await cmd.run(_ctx(store, settings, registry, args={"action": "list", "target": ""}))
        assert "imp_skill" in out
        assert "dec_skill" in out

    async def test_list_includes_description_first_line(self, wired) -> None:
        # The brief: `name | description first line | tools count`.
        store, settings, registry = wired
        skill_registry = FakeSkillRegistry()
        skill_registry.add(
            _make_declarative(
                "alpha",
                description="THE_FIRST_LINE\nignored second line",
            )
        )

        SkillCommand = _import_skill_command()
        cmd = SkillCommand(skill_registry=skill_registry, store=store)
        out = await cmd.run(_ctx(store, settings, registry, args={"action": "list", "target": ""}))
        assert "THE_FIRST_LINE" in out
        # Second line should be filtered out.
        assert "ignored second line" not in out

    async def test_list_marks_active_skill(self, wired) -> None:
        store, settings, registry = wired
        # Create a conversation and mark `dec_skill` active via direct
        # SQL — we cannot rely on `Store.set_active_skill` existing yet
        # in the public surface for this side path.
        conv = await store.get_or_create_conversation("discord", "thread-1")
        await store._c.execute(  # type: ignore[attr-defined]
            "UPDATE conversations SET active_skill = ? WHERE id = ?",
            ("dec_skill", conv.id),
        )
        await store._c.commit()  # type: ignore[attr-defined]

        skill_registry = FakeSkillRegistry()
        skill_registry.add(_make_declarative("dec_skill", description="active one"))
        skill_registry.add(_make_declarative("other_skill", description="not active"))

        SkillCommand = _import_skill_command()
        cmd = SkillCommand(skill_registry=skill_registry, store=store)
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                conversation_key="thread-1",
                args={"action": "list", "target": ""},
            )
        )
        # "[active]" marker is on the active skill's line (within
        # ~80 chars — same line). We assert both that "[active]" appears
        # and that it's near the active skill's name.
        assert "[active]" in out
        # active marker should be near (in same listing entry as)
        # the active skill name. We allow either "dec_skill ... [active]"
        # OR "[active] ... dec_skill" — both readable orderings.
        active_idx = out.index("[active]")
        # Look at the line containing [active].
        line_start = out.rfind("\n", 0, active_idx)
        line_end = out.find("\n", active_idx)
        if line_end == -1:
            line_end = len(out)
        active_line = out[line_start:line_end]
        assert "dec_skill" in active_line

    async def test_list_includes_tool_count(self, wired) -> None:
        store, settings, registry = wired
        skill_registry = FakeSkillRegistry()
        skill_registry.add(
            _make_declarative(
                "three_tools",
                tools=("a", "b", "c"),
            )
        )

        SkillCommand = _import_skill_command()
        cmd = SkillCommand(skill_registry=skill_registry, store=store)
        out = await cmd.run(_ctx(store, settings, registry, args={"action": "list", "target": ""}))
        # Some textual representation of "3" tools — we don't pin format.
        assert "3" in out

    async def test_list_empty_registry_returns_string(self, wired) -> None:
        # Edge: the brief doesn't pin wording on empty. Just assert it's
        # a string and not an error.
        store, settings, registry = wired
        skill_registry = FakeSkillRegistry()

        SkillCommand = _import_skill_command()
        cmd = SkillCommand(skill_registry=skill_registry, store=store)
        out = await cmd.run(_ctx(store, settings, registry, args={"action": "list", "target": ""}))
        assert isinstance(out, str)


# --- /skill set ----------------------------------------------------------


class TestActionSet:
    async def test_set_updates_active_skill_on_conversation(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")

        skill_registry = FakeSkillRegistry()
        skill_registry.add(
            _make_declarative(
                "demo",
                description="demo",
                tools=("a", "b"),
                system_prompt="First line\nMore details",
            )
        )

        SkillCommand = _import_skill_command()
        cmd = SkillCommand(skill_registry=skill_registry, store=store)
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                conversation_key="thread-1",
                args={"action": "set", "target": "demo"},
            )
        )
        assert isinstance(out, str)
        # Confirmation should mention the skill name.
        assert "demo" in out

        # Active skill column updated.
        conv = await store.get_or_create_conversation("discord", "thread-1")
        assert conv.active_skill == "demo"

    async def test_set_unknown_skill_returns_error_no_db_write(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")
        # Capture the original active_skill (None).
        orig = await store.get_or_create_conversation("discord", "thread-1")
        assert orig.active_skill is None

        skill_registry = FakeSkillRegistry()  # empty.

        SkillCommand = _import_skill_command()
        cmd = SkillCommand(skill_registry=skill_registry, store=store)
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                conversation_key="thread-1",
                args={"action": "set", "target": "ghost"},
            )
        )
        assert isinstance(out, str)
        assert out != ""
        # Spec: user-facing error referencing the missing name.
        assert "ghost" in out.lower() or "unknown" in out.lower() or "not" in out.lower()

        # No DB mutation.
        conv_after = await store.get_or_create_conversation("discord", "thread-1")
        assert conv_after.active_skill is None

    async def test_set_outside_conversation_returns_must_be_in_thread(self, wired) -> None:
        # Brief: `/skill set` requires a `conversation_key`.
        store, settings, registry = wired
        skill_registry = FakeSkillRegistry()
        skill_registry.add(_make_declarative("demo"))

        SkillCommand = _import_skill_command()
        cmd = SkillCommand(skill_registry=skill_registry, store=store)
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                conversation_key=None,
                args={"action": "set", "target": "demo"},
            )
        )
        assert isinstance(out, str)
        lowered = out.lower()
        # Some indication that this needs to be invoked in a thread.
        assert any(t in lowered for t in ("conversation", "thread"))

    async def test_set_confirmation_mentions_tool_count(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")

        skill_registry = FakeSkillRegistry()
        skill_registry.add(
            _make_declarative(
                "demo",
                description="demo",
                tools=("a", "b", "c"),
                system_prompt="line one",
            )
        )

        SkillCommand = _import_skill_command()
        cmd = SkillCommand(skill_registry=skill_registry, store=store)
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                conversation_key="thread-1",
                args={"action": "set", "target": "demo"},
            )
        )
        # Tool count mentioned somewhere in the confirmation.
        assert "3" in out

    async def test_set_confirmation_mentions_system_prompt_first_line(self, wired) -> None:
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")

        skill_registry = FakeSkillRegistry()
        skill_registry.add(
            _make_declarative(
                "demo",
                description="d",
                tools=(),
                system_prompt="THE_PROMPT_FIRST_LINE\nignored second line",
            )
        )

        SkillCommand = _import_skill_command()
        cmd = SkillCommand(skill_registry=skill_registry, store=store)
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                conversation_key="thread-1",
                args={"action": "set", "target": "demo"},
            )
        )
        # First line shown; second line not.
        assert "THE_PROMPT_FIRST_LINE" in out
        assert "ignored second line" not in out


# --- /skill clear --------------------------------------------------------


class TestActionClear:
    async def test_clear_sets_active_skill_to_null(self, wired) -> None:
        store, settings, registry = wired
        conv = await store.get_or_create_conversation("discord", "thread-1")
        # Pre-set an active skill to something via raw SQL.
        await store._c.execute(  # type: ignore[attr-defined]
            "UPDATE conversations SET active_skill = ? WHERE id = ?",
            ("demo", conv.id),
        )
        await store._c.commit()  # type: ignore[attr-defined]
        re = await store.get_or_create_conversation("discord", "thread-1")
        assert re.active_skill == "demo"

        skill_registry = FakeSkillRegistry()
        skill_registry.add(_make_declarative("demo"))

        SkillCommand = _import_skill_command()
        cmd = SkillCommand(skill_registry=skill_registry, store=store)
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                conversation_key="thread-1",
                args={"action": "clear", "target": ""},
            )
        )
        assert isinstance(out, str)

        conv_after = await store.get_or_create_conversation("discord", "thread-1")
        assert conv_after.active_skill is None

    async def test_clear_outside_conversation_returns_must_be_in_thread(self, wired) -> None:
        # Brief: `/skill clear` requires a `conversation_key`.
        store, settings, registry = wired
        skill_registry = FakeSkillRegistry()

        SkillCommand = _import_skill_command()
        cmd = SkillCommand(skill_registry=skill_registry, store=store)
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                conversation_key=None,
                args={"action": "clear", "target": ""},
            )
        )
        assert isinstance(out, str)
        lowered = out.lower()
        assert any(t in lowered for t in ("conversation", "thread"))

    async def test_clear_when_already_clear_is_idempotent(self, wired) -> None:
        # Edge: `/skill clear` on a conversation with no active skill.
        # Should not raise; should return some confirmation. DB still null.
        store, settings, registry = wired
        await store.get_or_create_conversation("discord", "thread-1")
        skill_registry = FakeSkillRegistry()

        SkillCommand = _import_skill_command()
        cmd = SkillCommand(skill_registry=skill_registry, store=store)
        out = await cmd.run(
            _ctx(
                store,
                settings,
                registry,
                conversation_key="thread-1",
                args={"action": "clear", "target": ""},
            )
        )
        assert isinstance(out, str)
        conv_after = await store.get_or_create_conversation("discord", "thread-1")
        assert conv_after.active_skill is None
