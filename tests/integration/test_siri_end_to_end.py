"""Siri channel end to end (design doc #55, SIRI-FC-1 + SIRI-TLC-5).

Full wiring: SiriChannel -> TurnGate (serialize_conversations=True,
debounce 0) -> real Agent with a scripted FakeProvider -> real
file-backed Store. HTTP is driven in-process via httpx's ASGITransport;
no sockets, no real LLM.

Covered:

1. Fast provider: POST /turn returns finished:true with the provider's
   answer, and the store holds the siri conversation with one user and
   one assistant row (SIRI-TLC-5).
2. Slow provider: the caller gets finished:false + WORKING_TEXT within
   the tiny deadline; after the provider is released the final reply is
   delivered via fallback_notify with the "[siri] " prefix, and both
   rows are persisted even though no HTTP caller was waiting.
3. A second POST within the session window continues the same
   conversation row (same conversation_key, message_count grows).
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from ember.agent import Agent
from ember.channels.gate import TurnGate
from ember.channels.siri import WORKING_TEXT, SiriChannel
from ember.config import Settings
from ember.store.repo import Store
from tests.fakes.fake_provider import FakeProvider
from tests.util import unstamp

TOKEN = "integration-siri-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# --- Helpers ---------------------------------------------------------------


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    return Settings()  # type: ignore[call-arg]


class FallbackRecorder:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.arrived = asyncio.Event()

    async def __call__(self, content: str) -> None:
        self.calls.append(content)
        self.arrived.set()


class BlockingProvider(FakeProvider):
    """FakeProvider that holds `complete` until released, so a test can
    outlive the HTTP deadline deterministically (no fixed sleeps)."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, *args, **kwargs):
        self.started.set()
        await self.release.wait()
        return await super().complete(*args, **kwargs)


async def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


async def _conversation_id(store: Store, key: str) -> int | None:
    cur = await store._c.execute(
        "SELECT id FROM conversations WHERE channel = 'siri' AND channel_conversation_key = ?",
        (key,),
    )
    row = await cur.fetchone()
    await cur.close()
    return row[0] if row else None


async def _siri_conversation_count(store: Store) -> int:
    cur = await store._c.execute("SELECT COUNT(*) FROM conversations WHERE channel = 'siri'")
    row = await cur.fetchone()
    await cur.close()
    return row[0]


async def _roles(store: Store, conversation_id: int) -> list[str]:
    msgs = await store.recent_messages(conversation_id)
    return [m.role for m in msgs]


@asynccontextmanager
async def _wiring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider: FakeProvider | None = None,
    deadline_s: float = 25.0,
):
    settings = _make_settings(monkeypatch, tmp_path)
    store = Store(tmp_path / "conversations.db")
    await store.open()

    provider = provider if provider is not None else FakeProvider()
    fallback = FallbackRecorder()
    channel = SiriChannel(
        token=TOKEN,
        store=store,
        fallback_notify=fallback,
        deadline_s=deadline_s,
    )
    agent = Agent(store=store, provider=provider, channel=channel, settings=settings)
    gate = TurnGate(agent.handle_turn, debounce_s=0, serialize_conversations=True)
    channel.register_handler(gate.submit)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=channel.app), base_url="http://test"
    )
    try:
        yield store, provider, channel, fallback, gate, client
    finally:
        await client.aclose()
        await store.close()


async def _quiesce(gate: TurnGate) -> None:
    """Wait for the gate's in-flight turn bookkeeping to drain so the
    store isn't closed under a running turn."""
    await _wait_until(lambda: gate._locks == {} and gate._inflight == {})


# --- 1. Fast provider: full happy path + persistence -----------------------


class TestFastTurn:
    async def test_post_turn_returns_answer_and_persists_both_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _wiring(tmp_path, monkeypatch) as (
            store,
            provider,
            _channel,
            fallback,
            gate,
            client,
        ):
            provider.queue_text("the scripted answer")

            resp = await asyncio.wait_for(
                client.post("/turn", json={"text": "hello ember"}, headers=AUTH),
                timeout=10,
            )

            assert resp.status_code == 200
            data = resp.json()
            assert data["finished"] is True
            assert data["reply"] == "the scripted answer"
            key = data["conversation_key"]
            assert key.startswith("siri:")

            await _quiesce(gate)

            # SIRI-TLC-5: the siri conversation row exists with one user
            # and one assistant message.
            cid = await _conversation_id(store, key)
            assert cid is not None
            msgs = await store.recent_messages(cid)
            assert [m.role for m in msgs] == ["user", "assistant"]
            assert unstamp(msgs[0].content) == "hello ember"
            assert msgs[1].content == "the scripted answer"

            # Delivered to the live HTTP waiter; fallback untouched.
            assert fallback.calls == []


# --- 2. Slow provider: deadline honesty + fallback delivery ----------------


class TestSlowTurn:
    async def test_deadline_miss_falls_back_and_still_persists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = BlockingProvider()
        async with _wiring(tmp_path, monkeypatch, provider=provider, deadline_s=0.1) as (
            store,
            provider,
            _channel,
            fallback,
            gate,
            client,
        ):
            provider.queue_text("the late answer")

            try:
                resp = await asyncio.wait_for(
                    client.post("/turn", json={"text": "slow question"}, headers=AUTH),
                    timeout=10,
                )

                # SIRI-TLC-2: honest deadline response.
                assert resp.status_code == 200
                data = resp.json()
                assert data["finished"] is False
                assert data["reply"] == WORKING_TEXT
                key = data["conversation_key"]
                assert key.startswith("siri:")
                assert fallback.calls == []

                # Turn is genuinely still running.
                await asyncio.wait_for(provider.started.wait(), timeout=5)
            finally:
                provider.release.set()

            # The final reply must reach Discord fallback, "[siri] "-prefixed.
            await asyncio.wait_for(fallback.arrived.wait(), timeout=10)
            assert fallback.calls == ["[siri] the late answer"]

            await _quiesce(gate)

            # SIRI-TLC-5: persistence did not depend on the HTTP waiter.
            cid = await _conversation_id(store, key)
            assert cid is not None
            roles = await _roles(store, cid)
            assert roles == ["user", "assistant"]
            msgs = await store.recent_messages(cid)
            assert unstamp(msgs[0].content) == "slow question"
            assert msgs[1].content == "the late answer"


# --- 3. Session window: second POST continues the conversation -------------


class TestSessionContinuation:
    async def test_second_post_within_window_reuses_conversation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with _wiring(tmp_path, monkeypatch) as (
            store,
            provider,
            _channel,
            _fallback,
            gate,
            client,
        ):
            provider.queue_text("first reply")
            provider.queue_text("second reply")

            resp1 = await asyncio.wait_for(
                client.post("/turn", json={"text": "first question"}, headers=AUTH),
                timeout=10,
            )
            resp2 = await asyncio.wait_for(
                client.post("/turn", json={"text": "second question"}, headers=AUTH),
                timeout=10,
            )

            assert resp1.json()["finished"] is True
            assert resp2.json()["finished"] is True
            assert resp1.json()["reply"] == "first reply"
            assert resp2.json()["reply"] == "second reply"

            # Same conversation continued.
            key = resp1.json()["conversation_key"]
            assert resp2.json()["conversation_key"] == key

            await _quiesce(gate)

            assert await _siri_conversation_count(store) == 1
            cid = await _conversation_id(store, key)
            assert cid is not None
            assert await store.message_count(cid) == 4
            msgs = await store.recent_messages(cid)
            assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]
