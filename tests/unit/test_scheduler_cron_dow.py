"""Crontab day-of-week translation for the scheduler.

`CronTrigger.from_crontab` does not translate the day-of-week field: crontab
numbers Sunday 0, APScheduler numbers Monday 0, and the field is handed
straight through. Every DOW-restricted job in the skill tree therefore fired
exactly one day late — the portfolio brief ran Tue-Sat instead of Mon-Fri,
the weekly digest ran Monday instead of Sunday.

These tests pin the translation and, more importantly, the resulting fire
times for the crons actually in the tree.
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

import pytest

from ember.scheduler.service import _normalize_crontab_dow, crontab_trigger

CHICAGO = ZoneInfo("America/Chicago")


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("*", "*"),
        ("1-5", "mon,tue,wed,thu,fri"),  # the portfolio brief
        ("0", "sun"),  # the weekly digest
        ("7", "sun"),  # crontab's second spelling of Sunday
        ("1", "mon"),  # the mortgage monitor
        ("6", "sat"),
        ("0,6", "sat,sun"),  # weekends
        ("5-1", "mon,fri,sat,sun"),  # wraps through Sunday
        ("0-2", "mon,tue,sun"),  # wraps in APScheduler's ordering
        ("mon-fri", "mon,tue,wed,thu,fri"),  # names pass through unharmed
        ("MON,wed", "mon,wed"),  # case-insensitive
        ("*/2", "tue,thu,sat,sun"),  # 0,2,4,6 — counts from crontab's Sunday
        ("1-5/2", "mon,wed,fri"),
    ],
)
def test_normalize_dow(field: str, expected: str) -> None:
    assert _normalize_crontab_dow(field) == expected


@pytest.mark.parametrize("field", ["8", "-1", "notaday", "1-9", "1/0"])
def test_normalize_dow_rejects_garbage(field: str) -> None:
    with pytest.raises(ValueError):
        _normalize_crontab_dow(field)


def _fire_days(cron: str, *, start: _dt.datetime, count: int) -> list[str]:
    trigger = crontab_trigger(cron, CHICAGO)
    out: list[str] = []
    prev: _dt.datetime | None = None
    nxt = start
    for _ in range(count):
        fire = trigger.get_next_fire_time(prev, nxt)
        assert fire is not None
        out.append(fire.strftime("%Y-%m-%d %a"))
        prev = fire
        nxt = fire + _dt.timedelta(seconds=1)
    return out


def test_portfolio_brief_fires_weekdays_not_tue_through_sat() -> None:
    """`0 17 * * 1-5` means Mon-Fri. Before the fix this skipped every
    Monday and fired every Saturday instead."""
    days = _fire_days(
        "0 17 * * 1-5",
        start=_dt.datetime(2026, 8, 3, tzinfo=CHICAGO),  # a Monday
        count=6,
    )
    assert days == [
        "2026-08-03 Mon",
        "2026-08-04 Tue",
        "2026-08-05 Wed",
        "2026-08-06 Thu",
        "2026-08-07 Fri",
        "2026-08-10 Mon",
    ]


def test_weekly_digest_fires_sunday() -> None:
    """`0 16 * * 0` — retrospective's weekly approval digest."""
    days = _fire_days(
        "0 16 * * 0",
        start=_dt.datetime(2026, 8, 3, tzinfo=CHICAGO),
        count=2,
    )
    assert days == ["2026-08-09 Sun", "2026-08-16 Sun"]


def test_mortgage_monitor_fires_monday() -> None:
    """`0 8 * * 1` — the skill's own description says every Monday."""
    days = _fire_days(
        "0 8 * * 1",
        start=_dt.datetime(2026, 8, 4, tzinfo=CHICAGO),
        count=2,
    )
    assert days == ["2026-08-10 Mon", "2026-08-17 Mon"]


def test_dow_unrestricted_crons_are_untouched() -> None:
    """Daily/monthly jobs have no DOW field to mistranslate."""
    assert _fire_days("0 5 * * *", start=_dt.datetime(2026, 8, 3, tzinfo=CHICAGO), count=3) == [
        "2026-08-03 Mon",
        "2026-08-04 Tue",
        "2026-08-05 Wed",
    ]
    assert _fire_days("0 8 1 * *", start=_dt.datetime(2026, 8, 3, tzinfo=CHICAGO), count=2) == [
        "2026-09-01 Tue",
        "2026-10-01 Thu",
    ]


def test_malformed_cron_still_raises_from_apscheduler() -> None:
    """Non-5-field specs pass through to APScheduler's own validation."""
    with pytest.raises(ValueError):
        crontab_trigger("0 17 * *", CHICAGO)
