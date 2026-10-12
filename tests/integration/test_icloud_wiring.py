"""iCloud tool wiring (`_maybe_register_icloud_tools`) — v16.

The implementer adds:

  - `Settings.icloud_apple_id: str | None`
  - `Settings.icloud_app_password: SecretStr | None`
  - `Settings.icloud_caldav_url: str` (default `https://caldav.icloud.com/`)
  - `ember.main._maybe_register_icloud_tools(registry, settings)`

When both apple_id + app_password are set, all six tools register:
  - reminders_list, reminders_add, reminders_complete
  - calendar_list_calendars, calendar_list_events, calendar_add_event

When either is missing, the helper is a no-op.

Tester barrier: we don't need real caldav. The `_maybe_register_*`
helper is expected to be a thin wrapper over tool construction; the
tools' `__init__` does NOT (and should not) reach the network. This is
consistent with `_maybe_register_gmail_tools` in main.py.

Tester-flagged ambiguity: the env-var names. The contract says
`icloud_apple_id`, `icloud_app_password`, `icloud_caldav_url`. Pydantic
case-insensitive env loading (per Settings.model_config) means
`ICLOUD_APPLE_ID` / `ICLOUD_APP_PASSWORD` / `ICLOUD_CALDAV_URL` map to
those fields. We set all three and expect a clean registration path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.config import Settings
from ember.main import _maybe_register_icloud_tools
from ember.tools.registry import Registry


_EXPECTED_TOOLS = {
    "reminders_list",
    "reminders_add",
    "reminders_complete",
    "calendar_list_calendars",
    "calendar_list_events",
    "calendar_add_event",
}


def _baseline_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Set the discord/required env vars so Settings() constructs."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    # Make sure stale icloud env from a parent shell doesn't leak in.
    monkeypatch.delenv("ICLOUD_APPLE_ID", raising=False)
    monkeypatch.delenv("ICLOUD_APP_PASSWORD", raising=False)
    monkeypatch.delenv("ICLOUD_CALDAV_URL", raising=False)


def test_registers_all_six_tools_when_both_envs_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _baseline_env(monkeypatch, tmp_path)
    monkeypatch.setenv("ICLOUD_APPLE_ID", "ember@example.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "abc-defg-hijk-lmno")  # noqa: S105
    # Default URL is fine; the contract says
    # icloud_caldav_url defaults to https://caldav.icloud.com/.

    settings = Settings()  # type: ignore[call-arg]
    registry = Registry()

    _maybe_register_icloud_tools(registry, settings)

    names = set(registry.names())
    missing = _EXPECTED_TOOLS - names
    assert not missing, f"missing iCloud tools: {missing}; got names={sorted(names)}"


def test_no_op_when_apple_id_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _baseline_env(monkeypatch, tmp_path)
    # Only password set, no apple_id.
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "abc-defg-hijk-lmno")  # noqa: S105

    settings = Settings()  # type: ignore[call-arg]
    registry = Registry()

    _maybe_register_icloud_tools(registry, settings)

    assert _EXPECTED_TOOLS.isdisjoint(set(registry.names()))


def test_no_op_when_password_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _baseline_env(monkeypatch, tmp_path)
    monkeypatch.setenv("ICLOUD_APPLE_ID", "ember@example.com")

    settings = Settings()  # type: ignore[call-arg]
    registry = Registry()

    _maybe_register_icloud_tools(registry, settings)

    assert _EXPECTED_TOOLS.isdisjoint(set(registry.names()))


def test_no_op_when_both_envs_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _baseline_env(monkeypatch, tmp_path)
    # Neither var set.

    settings = Settings()  # type: ignore[call-arg]
    registry = Registry()

    _maybe_register_icloud_tools(registry, settings)

    assert _EXPECTED_TOOLS.isdisjoint(set(registry.names()))


def test_custom_caldav_url_env_supported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The implementer must allow overriding the caldav URL via
    ICLOUD_CALDAV_URL — useful for fastmail/posteo/etc. We don't pin
    that the override flows into tool construction (we'd need to
    inspect tool internals); we pin only that Settings exposes the
    overridden value AND the helper still registers all six tools."""
    _baseline_env(monkeypatch, tmp_path)
    monkeypatch.setenv("ICLOUD_APPLE_ID", "ember@example.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "abc-defg-hijk-lmno")  # noqa: S105
    monkeypatch.setenv("ICLOUD_CALDAV_URL", "https://caldav.example.org/")

    settings = Settings()  # type: ignore[call-arg]
    # Pydantic case-insensitive: this attribute name is contractually
    # part of the wiring — a typo at impl time would surface here.
    assert getattr(settings, "icloud_caldav_url", None) == "https://caldav.example.org/"

    registry = Registry()
    _maybe_register_icloud_tools(registry, settings)
    assert _EXPECTED_TOOLS.issubset(set(registry.names()))
