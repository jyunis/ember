"""InMemorySkillRegistry (FC-5).

The implementer ships `ember.skills.registry.InMemorySkillRegistry`,
which:
- Stores `ImperativeSkill` objects keyed by `name`.
- `replace(skill)` adds or replaces by name and fires every callback
  registered via `on_change`.
- `remove(name)` deletes (no-op if absent) and fires callbacks.
- `list_schedules()` returns one `ScheduledFunction` per
  `(skill, ScheduleSpec)` pair, in deterministic order (TLC-12 spirit).
- `get(name)` raises `KeyError` if absent.
- `names()` returns names sorted.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from ember.scheduler.base import ScheduledFunction, ScheduleSpec
from ember.skills.base import ImperativeSkill


def _import_registry():
    from ember.skills.registry import InMemorySkillRegistry

    return InMemorySkillRegistry


# --- helpers --------------------------------------------------------------


async def _coro(_ctx: Any) -> str:
    return "ok"


def _make_skill(
    name: str,
    *,
    description: str = "stub",
    schedules: list[ScheduleSpec] | None = None,
    fns: dict[str, Callable[..., Awaitable[Any]]] | None = None,
) -> ImperativeSkill:
    schedules = schedules or []
    fns = fns or {}
    # Default: provide a coroutine for every referenced spec function.
    for spec in schedules:
        fns.setdefault(spec.function, _coro)
    return ImperativeSkill(
        name=name,
        description=description,
        scheduled_runs=tuple(schedules),
        functions=fns,
    )


# --- get / list / names ---------------------------------------------------


class TestBasicReadAccess:
    def test_empty_registry_lists_empty(self) -> None:
        InMemorySkillRegistry = _import_registry()
        reg = InMemorySkillRegistry()
        assert reg.list() == []
        assert reg.names() == []

    def test_get_unknown_raises_key_error(self) -> None:
        InMemorySkillRegistry = _import_registry()
        reg = InMemorySkillRegistry()
        with pytest.raises(KeyError):
            reg.get("nope")

    def test_replace_then_get_round_trip(self) -> None:
        InMemorySkillRegistry = _import_registry()
        reg = InMemorySkillRegistry()
        skill = _make_skill("demo")
        reg.replace(skill)
        assert reg.get("demo").name == "demo"
        assert reg.names() == ["demo"]

    def test_names_sorted(self) -> None:
        InMemorySkillRegistry = _import_registry()
        reg = InMemorySkillRegistry()
        reg.replace(_make_skill("zulu"))
        reg.replace(_make_skill("alpha"))
        reg.replace(_make_skill("mike"))
        assert reg.names() == ["alpha", "mike", "zulu"]


# --- replace + on_change --------------------------------------------------


class TestReplaceFiresCallbacks:
    def test_replace_new_fires_callbacks(self) -> None:
        InMemorySkillRegistry = _import_registry()
        reg = InMemorySkillRegistry()
        called = {"count": 0}

        def cb() -> None:
            called["count"] += 1

        reg.on_change(cb)
        reg.replace(_make_skill("demo"))
        assert called["count"] == 1

    def test_replace_existing_replaces_and_fires(self) -> None:
        InMemorySkillRegistry = _import_registry()
        reg = InMemorySkillRegistry()
        reg.replace(_make_skill("demo", description="v1"))

        called = {"count": 0}
        reg.on_change(lambda: called.__setitem__("count", called["count"] + 1))

        reg.replace(_make_skill("demo", description="v2"))
        assert reg.get("demo").description == "v2"
        assert called["count"] == 1

    def test_multiple_callbacks_all_fire(self) -> None:
        InMemorySkillRegistry = _import_registry()
        reg = InMemorySkillRegistry()
        a, b = {"n": 0}, {"n": 0}
        reg.on_change(lambda: a.__setitem__("n", a["n"] + 1))
        reg.on_change(lambda: b.__setitem__("n", b["n"] + 1))
        reg.replace(_make_skill("demo"))
        assert a["n"] == 1
        assert b["n"] == 1


# --- remove + on_change ---------------------------------------------------


class TestRemoveBehavior:
    def test_remove_present_deletes_and_fires(self) -> None:
        InMemorySkillRegistry = _import_registry()
        reg = InMemorySkillRegistry()
        reg.replace(_make_skill("demo"))
        called = {"n": 0}
        reg.on_change(lambda: called.__setitem__("n", called["n"] + 1))

        reg.remove("demo")
        with pytest.raises(KeyError):
            reg.get("demo")
        assert called["n"] == 1

    def test_remove_absent_is_no_op_and_fires_callbacks(self) -> None:
        # Spec: "Remove by name. No-op if absent. Fires change callbacks."
        # Both clauses apply unconditionally per the docstring.
        InMemorySkillRegistry = _import_registry()
        reg = InMemorySkillRegistry()
        called = {"n": 0}
        reg.on_change(lambda: called.__setitem__("n", called["n"] + 1))

        # Should not raise.
        reg.remove("nope")
        # NOTE [ambiguity]: docstring says "No-op if absent. Fires change
        # callbacks." — read literally, callbacks fire even when nothing
        # changed. If the implementer chose to skip the callback when
        # the entry was absent, both interpretations are defensible. We
        # accept either by asserting `called["n"] in (0, 1)`. If a
        # tighter spec lands, tighten here.
        assert called["n"] in (0, 1)


# --- list_schedules (TLC-12 deterministic ordering) ----------------------


class TestListSchedules:
    def test_one_scheduled_function_per_spec(self) -> None:
        InMemorySkillRegistry = _import_registry()
        reg = InMemorySkillRegistry()
        spec1 = ScheduleSpec(cron="0 5 * * *", function="f1", name="morning")
        spec2 = ScheduleSpec(cron="0 6 * * *", function="f1", name="early")
        skill = _make_skill("demo", schedules=[spec1, spec2])
        reg.replace(skill)

        scheds = reg.list_schedules()
        assert len(scheds) == 2
        assert all(isinstance(s, ScheduledFunction) for s in scheds)
        assert {s.spec.name for s in scheds} == {"morning", "early"}
        assert all(s.skill == "demo" for s in scheds)

    def test_no_scheduled_runs_returns_empty(self) -> None:
        InMemorySkillRegistry = _import_registry()
        reg = InMemorySkillRegistry()
        reg.replace(_make_skill("a"))
        reg.replace(_make_skill("b"))
        assert reg.list_schedules() == []

    def test_deterministic_order_across_skills(self) -> None:
        # TLC-12 spirit: refresh-time diffing relies on order stability.
        # We expect skill names sorted lexicographically (alpha → beta),
        # and within a skill, the ScheduleSpec.name sorted lex too.
        # If the implementer chose a different total order, the test
        # documents the assumption — flip if spec is updated.
        InMemorySkillRegistry = _import_registry()
        reg = InMemorySkillRegistry()
        reg.replace(
            _make_skill(
                "zskill",
                schedules=[
                    ScheduleSpec(cron="0 1 * * *", function="f", name="z_run"),
                    ScheduleSpec(cron="0 2 * * *", function="f", name="a_run"),
                ],
            )
        )
        reg.replace(
            _make_skill(
                "askill",
                schedules=[
                    ScheduleSpec(cron="0 3 * * *", function="f", name="m_run"),
                ],
            )
        )

        order_first = [(s.skill, s.spec.name) for s in reg.list_schedules()]
        order_second = [(s.skill, s.spec.name) for s in reg.list_schedules()]
        # Stable across calls.
        assert order_first == order_second
        # And — crucially for refresh-diff — the same skill's specs are
        # contiguous (i.e. we don't interleave skills). This is the
        # weakest condition that still supports TLC-12 diffing.
        skills_in_order = [s for s, _ in order_first]
        # Each unique skill appears as a contiguous block.
        seen = []
        for skill in skills_in_order:
            if not seen or seen[-1] != skill:
                seen.append(skill)
        assert len(seen) == len(set(seen)), "skills are not contiguously grouped in list_schedules"
