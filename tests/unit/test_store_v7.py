"""Store v=7 — connected_mcps (mcp_connector persistence).

Round-trip test for the `connected_mcps` table introduced at v=7 along
with the three Store methods used by the boot replay and the
mcp_connector tools.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember.store.repo import SCHEMA_VERSION, Store


@pytest.fixture
async def store(tmp_path: Path):
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


class TestSchema:
    async def test_version_is_7_or_later(self, store: Store) -> None:
        assert SCHEMA_VERSION >= 7


class TestRoundTrip:
    async def test_save_then_list_returns_row(self, store: Store) -> None:
        await store.save_connected_mcp(
            name="crypto_wallet",
            transport="streamableHttp",
            url="https://example.test/mcp",
            headers={"Authorization": "Bearer token"},
        )
        rows = await store.list_connected_mcps()
        assert rows == [
            {
                "name": "crypto_wallet",
                "transport": "streamableHttp",
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "Bearer token"},
            }
        ]

    async def test_save_with_no_headers_returns_none(self, store: Store) -> None:
        await store.save_connected_mcp(
            name="public",
            transport="streamableHttp",
            url="https://example.test/mcp",
            headers=None,
        )
        rows = await store.list_connected_mcps()
        assert rows[0]["headers"] is None

    async def test_save_is_idempotent_on_name(self, store: Store) -> None:
        await store.save_connected_mcp(
            name="x", transport="streamableHttp", url="http://a", headers=None
        )
        await store.save_connected_mcp(
            name="x", transport="streamableHttp", url="http://b", headers={"k": "v"}
        )
        rows = await store.list_connected_mcps()
        assert len(rows) == 1
        assert rows[0]["url"] == "http://b"
        assert rows[0]["headers"] == {"k": "v"}

    async def test_list_orders_by_name(self, store: Store) -> None:
        for n in ("c", "a", "b"):
            await store.save_connected_mcp(
                name=n, transport="streamableHttp", url=f"http://{n}", headers=None
            )
        rows = await store.list_connected_mcps()
        assert [r["name"] for r in rows] == ["a", "b", "c"]

    async def test_delete_returns_true_when_present(self, store: Store) -> None:
        await store.save_connected_mcp(
            name="x", transport="streamableHttp", url="http://a", headers=None
        )
        assert await store.delete_connected_mcp("x") is True
        assert await store.list_connected_mcps() == []

    async def test_delete_returns_false_when_absent(self, store: Store) -> None:
        assert await store.delete_connected_mcp("missing") is False
