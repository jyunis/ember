"""Temporal context on user turns (TC-1).

Two halves of one feature:

- `_timestamp_prefix` / `_human_gap` / `_display_tz` — pure helpers that build
  the bracketed local-timestamp prefix (and, after a long silence, a
  precomputed gap note) baked into each persisted/sent user turn.
- `_TRANSCRIPT_TIME_NOTE` — the static system-block instruction teaching the
  model how to read those stamps.

Pure-function coverage below is exhaustive (format, gap threshold, plural,
tz, naive-previous). Two end-to-end checks drive a real turn to confirm the
stamp lands on the persisted user row and the note is in the assembled
system block.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ember.agent import (
    Agent,
    _TRANSCRIPT_TIME_NOTE,
    _display_tz,
    _human_gap,
    _timestamp_prefix,
)
from ember.channels.base import ChannelMessage
from ember.config import Settings
from ember.store.repo import Store
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider
from tests.util import unstamp

# A fixed anchor so stamps are deterministic (the weekday is derived via
# strftime rather than hard-coded, so the tests don't assert a calendar fact).
_NOW = datetime(2026, 7, 11, 18, 52, tzinfo=timezone.utc)
_WD = _NOW.strftime("%a")


# --- _human_gap ------------------------------------------------------------


class TestHumanGap:
    def test_below_threshold_is_none(self) -> None:
        assert _human_gap(timedelta(hours=5, minutes=59)) is None

    def test_at_threshold_reports_hours(self) -> None:
        assert _human_gap(timedelta(hours=6)) == "6 hours after previous message"

    def test_sub_day_reports_hours(self) -> None:
        assert _human_gap(timedelta(hours=23)) == "23 hours after previous message"

    def test_one_day_is_singular(self) -> None:
        assert _human_gap(timedelta(days=1)) == "1 day after previous message"

    def test_day_and_change_floors_to_days(self) -> None:
        assert _human_gap(timedelta(days=1, hours=12)) == "1 day after previous message"

    def test_multi_day_is_plural(self) -> None:
        assert _human_gap(timedelta(days=5)) == "5 days after previous message"


# --- _timestamp_prefix -----------------------------------------------------


class TestTimestampPrefix:
    def test_no_previous_is_bare_stamp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EMBER_DISPLAY_TZ", raising=False)
        out = _timestamp_prefix("what's for dinner?", now=_NOW, previous=None)
        assert out == f"[{_WD} 2026-07-11 18:52]\nwhat's for dinner?"

    def test_recent_previous_adds_no_gap_note(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EMBER_DISPLAY_TZ", raising=False)
        out = _timestamp_prefix("x", now=_NOW, previous=_NOW - timedelta(hours=2))
        assert " · " not in out
        assert out == f"[{_WD} 2026-07-11 18:52]\nx"

    def test_long_silence_adds_precomputed_gap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EMBER_DISPLAY_TZ", raising=False)
        out = _timestamp_prefix("dinner?", now=_NOW, previous=_NOW - timedelta(days=5))
        assert out == f"[{_WD} 2026-07-11 18:52 · 5 days after previous message]\ndinner?"

    def test_naive_previous_treated_as_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EMBER_DISPLAY_TZ", raising=False)
        naive = datetime(2026, 7, 6, 18, 52)  # no tzinfo
        out = _timestamp_prefix("x", now=_NOW, previous=naive)
        assert "5 days after previous message" in out

    def test_renders_in_display_tz(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 18:52Z in July is 14:52 EDT (UTC-4); the calendar date is unchanged.
        monkeypatch.setenv("EMBER_DISPLAY_TZ", "America/New_York")
        out = _timestamp_prefix("x", now=_NOW, previous=None)
        assert "2026-07-11 14:52]" in out

    def test_stamp_line_matches_unstamp_contract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The regex the test suite uses to strip stamps must recognize both
        # the bare and gap-note forms produced here.
        monkeypatch.delenv("EMBER_DISPLAY_TZ", raising=False)
        bare = _timestamp_prefix("hi", now=_NOW, previous=None)
        gapped = _timestamp_prefix("hi", now=_NOW, previous=_NOW - timedelta(days=3))
        assert unstamp(bare) == "hi"
        assert unstamp(gapped) == "hi"


# --- _display_tz -----------------------------------------------------------


class TestDisplayTz:
    def test_unset_defaults_to_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EMBER_DISPLAY_TZ", raising=False)
        assert _display_tz() is timezone.utc

    def test_valid_iana_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMBER_DISPLAY_TZ", "America/New_York")
        assert _display_tz() == ZoneInfo("America/New_York")

    def test_invalid_name_falls_back_to_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMBER_DISPLAY_TZ", "Not/AZone")
        assert _display_tz() is timezone.utc


# --- End-to-end (real Store / Agent / fakes) -------------------------------


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.delenv("EMBER_DISPLAY_TZ", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
async def wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = _make_settings(monkeypatch, tmp_path)
    store = Store(tmp_path / "conversations.db")
    await store.open()
    provider = FakeProvider()
    channel = FakeChannel()
    agent = Agent(store=store, provider=provider, channel=channel, settings=settings)
    await channel.start(agent.handle_turn)
    try:
        yield store, provider, agent, channel
    finally:
        await store.close()


def _msg(text: str) -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key="thread-1",
        user_id="100",
        text=text,
        message_id="m1",
    )


class TestEndToEnd:
    async def test_persisted_user_turn_is_stamped(self, wiring) -> None:
        store, provider, agent, channel = wiring
        provider.queue_text("ok")

        await channel.inject(_msg("what's for lunch?"))

        c = await store.get_or_create_conversation("discord", "thread-1")
        user = next(m for m in await store.recent_messages(c.id) if m.role == "user")
        # Stamp present, strips back to the typed text, and — first turn of a
        # fresh conversation — carries no gap note.
        assert user.content.startswith("[")
        assert unstamp(user.content) == "what's for lunch?"
        assert " · " not in user.content.split("\n", 1)[0]

    async def test_system_block_carries_timing_note(self, wiring) -> None:
        store, _provider, agent, _ = wiring
        c = await store.get_or_create_conversation("discord", "thread-1")
        block = await agent._assemble_system_block(c)
        assert _TRANSCRIPT_TIME_NOTE in block
        assert "## Conversation timing" in block
