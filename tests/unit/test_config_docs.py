"""`Settings` — documents-indexer fields (FC-34 config).

Properties under test:

- Defaults: `paperless_base_url == "http://paperless:8000"`,
  `paperless_api_token is None`, `paperless_tika_enabled is False`,
  `docs_public_url == "http://localhost:9926"`,
  `docs_exclude_tag == "ember-exclude"`.
- `records_db_path == data_dir / "records.db"` (property, follows
  `DATA_DIR`).
- Env overrides land: `PAPERLESS_API_TOKEN` is a `SecretStr`,
  `PAPERLESS_TIKA_ENABLED=true` parses to True, the URL/tag fields
  override.

Settings are constructed via env like
`tests/integration/test_discord_feedback.py::_settings` (chdir into
`tmp_path` so no stray `.env` is read).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from ember.config import Settings


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> Settings:
    monkeypatch.chdir(tmp_path)
    for key in (
        "PAPERLESS_BASE_URL",
        "PAPERLESS_API_TOKEN",
        "PAPERLESS_TIKA_ENABLED",
        "DOCS_PUBLIC_URL",
        "DOCS_EXCLUDE_TAG",
        "DATA_DIR",
        "DISCORD_ALLOWED_USER_IDS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings()  # type: ignore[call-arg]


class TestDefaults:
    def test_documented_defaults(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        s = _settings(monkeypatch, tmp_path)
        assert s.paperless_base_url == "http://paperless:8000"
        assert s.paperless_api_token is None
        assert s.paperless_tika_enabled is False
        assert s.docs_public_url == "http://localhost:9926"
        assert s.docs_exclude_tag == "ember-exclude"

    def test_records_db_path_follows_data_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        s = _settings(monkeypatch, tmp_path, DATA_DIR=str(tmp_path / "data"))
        assert s.records_db_path == s.data_dir / "records.db"
        assert s.records_db_path == tmp_path / "data" / "records.db"

    def test_records_db_path_default_data_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        s = _settings(monkeypatch, tmp_path)
        assert s.records_db_path == Path("/data/records.db")


class TestOverrides:
    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        s = _settings(
            monkeypatch,
            tmp_path,
            PAPERLESS_BASE_URL="http://paperless.local:8000",
            PAPERLESS_API_TOKEN="secret-token",
            PAPERLESS_TIKA_ENABLED="true",
            DOCS_PUBLIC_URL="https://docs.example",
            DOCS_EXCLUDE_TAG="private",
        )
        assert s.paperless_base_url == "http://paperless.local:8000"
        assert isinstance(s.paperless_api_token, SecretStr)
        assert s.paperless_api_token.get_secret_value() == "secret-token"
        assert s.paperless_tika_enabled is True
        assert s.docs_public_url == "https://docs.example"
        assert s.docs_exclude_tag == "private"

    def test_token_not_leaked_by_repr(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        s = _settings(monkeypatch, tmp_path, PAPERLESS_API_TOKEN="secret-token")
        assert "secret-token" not in repr(s)
        assert "secret-token" not in str(s.paperless_api_token)
