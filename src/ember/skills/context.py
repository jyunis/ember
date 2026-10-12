"""`RunContext` factory for scheduled skill fires (FC-5, FC-6).

Builds a fresh `RunContext` per fire — UoW-5: cross-run state never
leaks. `ctx.notify` posts via `Channel.notify_scheduler` prefixed with
`[{skill}:{run_name}]` so the user can see at a glance which job
emitted the message.

`ctx.profile` and `ctx.soul` come from `IdentityStore.read_profile()` /
`.read_soul()` so the fire sees the same identity surface as a turn.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ember.scheduler.base import RunContext, ScheduleSpec

if TYPE_CHECKING:
    from ember.channels.base import Channel
    from ember.identity.store import IdentityStore
    from ember.providers.routing import ProviderRouter
    from ember.store.repo import Store
    from ember.tools.registry import Registry

_log = logging.getLogger("ember.skills.context")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_run_context(
    *,
    skill: str,
    spec: ScheduleSpec,
    scheduled_for: datetime | None,
    identity: "IdentityStore",
    tools: "Registry",
    router: "ProviderRouter",
    channel: "Channel",
    store: "Store | None" = None,
    now=_utcnow,
) -> RunContext:
    """Construct a `RunContext` for a single fire.

    `notify` wraps `channel.notify_scheduler` and prefixes the message
    with `[skill:run_name]`. Failures in `notify_scheduler` are logged
    and swallowed — the recorder is the source of truth (TLC-8).

    When a `store` is provided, `ctx.learning` is wired to a narrow
    `StoreLearningContext` façade (v12, FC-22) — the retrospective's only
    DB access. Absent a store (bare test wiring) `ctx.learning` is None.
    """

    async def notify(message: str) -> None:
        prefixed = f"[{skill}:{spec.name}] {message}"
        try:
            await channel.notify_scheduler(prefixed)
        except Exception:
            _log.warning("notify_scheduler failed for %s:%s", skill, spec.name, exc_info=True)

    profile = ""
    soul = ""
    try:
        profile = identity.read_profile()
    except Exception:
        _log.warning("identity.read_profile() failed; proceeding with empty profile")
    try:
        soul = identity.read_soul()
    except Exception:
        _log.warning("identity.read_soul() failed; proceeding with empty soul")

    learning = None
    if store is not None:
        from ember.scheduler.learning import StoreLearningContext

        learning = StoreLearningContext(store)

    return RunContext(
        skill=skill,
        run_name=spec.name,
        scheduled_for=scheduled_for,
        profile=profile,
        soul=soul,
        tools=tools,
        router=router,
        notify=notify,
        now=now,
        learning=learning,
    )
