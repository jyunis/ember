"""Settings (FC-13) — fail-fast on missing required fields, parse env-style
list strings, expose derived paths, and use `SecretStr` for secrets so
they don't print themselves into logs (TLC-4)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from ember.config import load_settings


# --- Helpers ---------------------------------------------------------------

# Keep a complete set of required env vars in one place so each test can
# start from a known-good baseline and selectively break one thing at a
# time.
REQUIRED_ENV = {
    "DISCORD_BOT_TOKEN": "secret-token-value",
    "DISCORD_GUILD_ID": "11111",
    "DISCORD_CONVERSATIONS_CHANNEL_ID": "22222",
    "DISCORD_SCHEDULER_CHANNEL_ID": "33333",
}


def _clear_ember_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every env var that pydantic-settings might pick up so the
    test environment is reproducible regardless of what the dev shell has
    set."""
    keys = [
        "DISCORD_BOT_TOKEN",
        "DISCORD_GUILD_ID",
        "DISCORD_CONVERSATIONS_CHANNEL_ID",
        "DISCORD_SCHEDULER_CHANNEL_ID",
        "DISCORD_ALLOWED_USER_IDS",
        "CLAUDE_BINARY_PATH",
        "CLAUDE_AUTH_DIR",
        "CLAUDE_DEFAULT_MODEL",
        "DATA_DIR",
        "LOG_LEVEL",
    ]
    for k in keys:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Run each test in a directory with no `.env` and no Ember-related
    env vars. Tests opt back in by setting what they need."""
    monkeypatch.chdir(tmp_path)
    _clear_ember_env(monkeypatch)
    return monkeypatch


# --- Required fields ------------------------------------------------------


class TestRequiredFields:
    def test_missing_required_raises(self, isolated_env: pytest.MonkeyPatch) -> None:
        # Nothing set → pydantic raises ValidationError.
        with pytest.raises(ValidationError):
            load_settings()

    def test_partial_required_raises(self, isolated_env: pytest.MonkeyPatch) -> None:
        # Set only the bot token; numeric IDs still missing.
        isolated_env.setenv("DISCORD_BOT_TOKEN", "x")
        with pytest.raises(ValidationError):
            load_settings()

    def test_full_required_loads(self, isolated_env: pytest.MonkeyPatch) -> None:
        for k, v in REQUIRED_ENV.items():
            isolated_env.setenv(k, v)
        s = load_settings()
        assert s.discord_guild_id == 11111
        assert s.discord_conversations_channel_id == 22222
        assert s.discord_scheduler_channel_id == 33333


# --- SecretStr (TLC-4) ----------------------------------------------------


class TestSecretHandling:
    def test_bot_token_is_secret_str(self, isolated_env: pytest.MonkeyPatch) -> None:
        for k, v in REQUIRED_ENV.items():
            isolated_env.setenv(k, v)
        s = load_settings()
        assert isinstance(s.discord_bot_token, SecretStr)

    def test_secret_does_not_appear_in_repr(self, isolated_env: pytest.MonkeyPatch) -> None:
        # TLC-4: accidental string-coercion / repr should not leak secrets.
        for k, v in REQUIRED_ENV.items():
            isolated_env.setenv(k, v)
        isolated_env.setenv("DISCORD_BOT_TOKEN", "AAAA-SECRET-TOKEN-AAAA")
        s = load_settings()
        assert "AAAA-SECRET-TOKEN-AAAA" not in repr(s)
        assert "AAAA-SECRET-TOKEN-AAAA" not in str(s)
        assert "AAAA-SECRET-TOKEN-AAAA" not in repr(s.discord_bot_token)
        # Real value is recoverable via the explicit accessor.
        assert s.discord_bot_token.get_secret_value() == "AAAA-SECRET-TOKEN-AAAA"


# --- Allowed-user-ids parsing --------------------------------------------


class TestAllowedUserIdsParsing:
    def _base_env(self, mp: pytest.MonkeyPatch) -> None:
        for k, v in REQUIRED_ENV.items():
            mp.setenv(k, v)

    def test_default_is_empty_list(self, isolated_env: pytest.MonkeyPatch) -> None:
        self._base_env(isolated_env)
        s = load_settings()
        assert s.discord_allowed_user_ids == []

    def test_csv_parses(self, isolated_env: pytest.MonkeyPatch) -> None:
        self._base_env(isolated_env)
        isolated_env.setenv("DISCORD_ALLOWED_USER_IDS", "111, 222 ,333")
        s = load_settings()
        assert s.discord_allowed_user_ids == [111, 222, 333]

    def test_empty_string_is_empty_list(self, isolated_env: pytest.MonkeyPatch) -> None:
        self._base_env(isolated_env)
        isolated_env.setenv("DISCORD_ALLOWED_USER_IDS", "")
        s = load_settings()
        assert s.discord_allowed_user_ids == []

    def test_single_value(self, isolated_env: pytest.MonkeyPatch) -> None:
        self._base_env(isolated_env)
        isolated_env.setenv("DISCORD_ALLOWED_USER_IDS", "42")
        s = load_settings()
        assert s.discord_allowed_user_ids == [42]


# --- Defaults --------------------------------------------------------------


class TestDefaults:
    def test_defaults_when_not_overridden(self, isolated_env: pytest.MonkeyPatch) -> None:
        for k, v in REQUIRED_ENV.items():
            isolated_env.setenv(k, v)
        s = load_settings()
        assert s.claude_binary_path == "claude"
        assert s.claude_default_model == "opus"
        # Internal work runs on the toolless subscription provider, not the
        # metered API — see main.py's claude_cli_cheap registration.
        assert s.claude_cheap_model == "sonnet"
        assert s.cheap_tier_route == "claude_cli_cheap:sonnet"
        assert s.claude_auth_dir == Path("/data/claude-auth")
        assert s.data_dir == Path("/data")
        assert s.log_level == "INFO"

    def test_data_dir_override(self, isolated_env: pytest.MonkeyPatch) -> None:
        for k, v in REQUIRED_ENV.items():
            isolated_env.setenv(k, v)
        isolated_env.setenv("DATA_DIR", "/tmp/ember-test")
        s = load_settings()
        assert s.data_dir == Path("/tmp/ember-test")


# --- Derived properties ---------------------------------------------------


class TestDerivedPaths:
    def test_conversations_db_path_uses_data_dir(self, isolated_env: pytest.MonkeyPatch) -> None:
        for k, v in REQUIRED_ENV.items():
            isolated_env.setenv(k, v)
        isolated_env.setenv("DATA_DIR", "/var/ember-data")
        s = load_settings()
        assert s.conversations_db_path == Path("/var/ember-data/conversations.db")


# --- Siri voice channel (doc #55 step 4) -----------------------------------


class TestSiriSettings:
    def test_defaults_disabled(self, isolated_env: pytest.MonkeyPatch) -> None:
        for k, v in REQUIRED_ENV.items():
            isolated_env.setenv(k, v)
        s = load_settings()
        assert s.siri_enabled is False
        assert s.siri_bind == "0.0.0.0"
        assert s.siri_port == 9940
        assert s.siri_deadline_s == 25.0
        assert s.siri_session_window_s == 600
        assert s.siri_user_id == "siri"
        assert isinstance(s.siri_api_token, SecretStr)
        assert s.siri_api_token.get_secret_value() == ""

    def test_env_overrides(self, isolated_env: pytest.MonkeyPatch) -> None:
        for k, v in REQUIRED_ENV.items():
            isolated_env.setenv(k, v)
        isolated_env.setenv("SIRI_ENABLED", "true")
        isolated_env.setenv("SIRI_API_TOKEN", "tok-123")
        isolated_env.setenv("SIRI_PORT", "9999")
        isolated_env.setenv("SIRI_DEADLINE_S", "10.5")
        s = load_settings()
        assert s.siri_enabled is True
        assert s.siri_api_token.get_secret_value() == "tok-123"
        assert s.siri_port == 9999
        assert s.siri_deadline_s == 10.5

    def test_token_does_not_leak_in_repr(self, isolated_env: pytest.MonkeyPatch) -> None:
        for k, v in REQUIRED_ENV.items():
            isolated_env.setenv(k, v)
        isolated_env.setenv("SIRI_API_TOKEN", "tok-secret-value")
        s = load_settings()
        assert "tok-secret-value" not in repr(s.siri_api_token)
