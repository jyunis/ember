"""SiriChannel adapter unit tests (design doc #55).

Contracts under test:

- SIRI-TLC-4 (ingress auth): nothing without a valid bearer token
  reaches the turn handler or the store; 401 carries a constant body
  and precedes body parsing.
- SIRI-TLC-2 (deadline honesty): POST /turn answers within
  `deadline_s` + epsilon; a `finished: false` response is followed by
  the eventual final being delivered via the Discord fallback with a
  "[siri] " prefix — never silently dropped.
- SIRI-UoW-2 (per-request future plumbing): a final/reply send
  resolves the waiting request(s) for its key; sends with no live
  waiter reroute to the fallback; waiters are strictly per-key.
- Session-window behavior end to end through the HTTP surface.

All HTTP is driven through the ASGI app in-process (httpx
ASGITransport) — no sockets. Synchronization is event-based with
bounded waits; no fixed sleeps.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from ember.channels.base import ChannelMessage
from ember.channels.siri import NO_REPLY_TEXT, WORKING_TEXT, SiriChannel
from ember.store.repo import Store

TOKEN = "test-siri-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
WINDOW_S = 600


# --- Helpers ---------------------------------------------------------------


class FakeClock:
    """Injectable clock; tests mutate `.now` between requests."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FallbackRecorder:
    """Records fallback_notify calls and signals arrival."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.arrived = asyncio.Event()

    async def __call__(self, content: str) -> None:
        self.calls.append(content)
        self.arrived.set()


async def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.01) -> None:
    """Bounded busy-wait on a sync predicate."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


async def _settle() -> None:
    """Yield the event loop a few times so any stray follow-up work
    (e.g. a duplicate fallback call) would have landed."""
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.fixture
async def store(tmp_path: Path):
    s = Store(tmp_path / "conversations.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def fallback() -> FallbackRecorder:
    return FallbackRecorder()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _channel(store: Store, fallback: FallbackRecorder, clock: FakeClock, **kw) -> SiriChannel:
    kw.setdefault("token", TOKEN)
    kw.setdefault("deadline_s", 5.0)
    kw.setdefault("session_window_s", WINDOW_S)
    return SiriChannel(store=store, fallback_notify=fallback, clock=clock, **kw)


def _client(channel: SiriChannel) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=channel.app), base_url="http://test")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"


async def _seed(store: Store, key: str, last_active: datetime) -> int:
    conv = await store.get_or_create_conversation("siri", key)
    await store._c.execute(
        "UPDATE conversations SET last_active = ? WHERE id = ?",
        (_iso(last_active), conv.id),
    )
    await store._c.commit()
    return conv.id


async def _siri_conversation_count(store: Store) -> int:
    cur = await store._c.execute("SELECT COUNT(*) FROM conversations WHERE channel = 'siri'")
    row = await cur.fetchone()
    await cur.close()
    return row[0]


# --- Construction ----------------------------------------------------------


class TestConstruction:
    def test_name_is_siri(self, store, fallback, clock) -> None:
        assert _channel(store, fallback, clock).name == "siri"

    def test_empty_token_raises_value_error(self, store, fallback, clock) -> None:
        with pytest.raises(ValueError):
            SiriChannel(token="", store=store, fallback_notify=fallback)

    def test_blank_token_raises_value_error(self, store, fallback, clock) -> None:
        with pytest.raises(ValueError):
            SiriChannel(token="   ", store=store, fallback_notify=fallback)


# --- GET /healthz ----------------------------------------------------------


class TestHealthz:
    async def test_healthz_ok_without_auth(self, store, fallback, clock) -> None:
        channel = _channel(store, fallback, clock)
        async with _client(channel) as client:
            resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# --- SIRI-TLC-4: ingress auth ----------------------------------------------


class TestAuth:
    async def test_missing_token_is_401_constant_body(self, store, fallback, clock) -> None:
        channel = _channel(store, fallback, clock)
        async with _client(channel) as client:
            resp = await client.post("/turn", json={"text": "hi"})
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}

    async def test_wrong_token_is_401(self, store, fallback, clock) -> None:
        channel = _channel(store, fallback, clock)
        async with _client(channel) as client:
            resp = await client.post(
                "/turn",
                json={"text": "hi"},
                headers={"Authorization": "Bearer wrong-token"},
            )
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}

    async def test_non_bearer_scheme_is_401(self, store, fallback, clock) -> None:
        channel = _channel(store, fallback, clock)
        async with _client(channel) as client:
            resp = await client.post(
                "/turn", json={"text": "hi"}, headers={"Authorization": f"Basic {TOKEN}"}
            )
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}

    async def test_auth_precedes_body_parsing_malformed_body(self, store, fallback, clock) -> None:
        # A malformed body with bad auth must be 401 (not 400): auth is
        # checked before the body is touched.
        channel = _channel(store, fallback, clock)
        async with _client(channel) as client:
            resp = await client.post(
                "/turn",
                content=b"{this is not json",
                headers={"Authorization": "Bearer wrong-token"},
            )
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}

    async def test_auth_precedes_body_parsing_huge_body(self, store, fallback, clock) -> None:
        channel = _channel(store, fallback, clock)
        async with _client(channel) as client:
            resp = await client.post("/turn", content=b"x" * (2 * 1024 * 1024))
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}

    async def test_401_never_invokes_handler_or_touches_store(self, store, fallback, clock) -> None:
        channel = _channel(store, fallback, clock)
        seen: list[ChannelMessage] = []

        async def handler(msg: ChannelMessage) -> None:
            seen.append(msg)

        channel.register_handler(handler)
        async with _client(channel) as client:
            resp = await client.post(
                "/turn",
                json={"text": "hi"},
                headers={"Authorization": "Bearer wrong-token"},
            )
        assert resp.status_code == 401
        assert seen == []
        assert await _siri_conversation_count(store) == 0
        assert fallback.calls == []


# --- Bad requests / not ready ----------------------------------------------


class TestBadRequest:
    @pytest.mark.parametrize(
        "content",
        [
            b"{not json",
            b'["a", "list"]',
            b'"just a string"',
            b"{}",
            b'{"text": 42}',
            b'{"text": null}',
            b'{"text": ""}',
            b'{"text": "   "}',
        ],
    )
    async def test_invalid_body_is_400(self, store, fallback, clock, content: bytes) -> None:
        channel = _channel(store, fallback, clock)

        async def handler(msg: ChannelMessage) -> None:  # pragma: no cover
            raise AssertionError("handler must not run for a bad request")

        channel.register_handler(handler)
        async with _client(channel) as client:
            resp = await client.post(
                "/turn",
                content=content,
                headers={**AUTH, "Content-Type": "application/json"},
            )
        assert resp.status_code == 400
        assert resp.json() == {"error": "bad_request"}

    async def test_valid_request_before_handler_registered_is_503(
        self, store, fallback, clock
    ) -> None:
        channel = _channel(store, fallback, clock)
        async with _client(channel) as client:
            resp = await client.post("/turn", json={"text": "hi"}, headers=AUTH)
        assert resp.status_code == 503
        assert resp.json() == {"error": "not_ready"}


# --- Happy path through the registered handler -----------------------------


class TestTurnHappyPath:
    async def test_final_send_resolves_the_request(self, store, fallback, clock) -> None:
        channel = _channel(store, fallback, clock)

        async def handler(msg: ChannelMessage) -> None:
            await channel.send(msg.conversation_key, "the reply", kind="final")

        channel.register_handler(handler)
        async with _client(channel) as client:
            resp = await client.post("/turn", json={"text": "hi"}, headers=AUTH)

        assert resp.status_code == 200
        data = resp.json()
        assert data["reply"] == "the reply"
        assert data["finished"] is True
        assert data["conversation_key"] == f"siri:{int(NOW.timestamp())}"
        # Delivered to the caller — the fallback must NOT also fire.
        await _settle()
        assert fallback.calls == []

    async def test_reply_kind_also_resolves(self, store, fallback, clock) -> None:
        channel = _channel(store, fallback, clock)

        async def handler(msg: ChannelMessage) -> None:
            await channel.send(msg.conversation_key, "via reply", kind="reply")

        channel.register_handler(handler)
        async with _client(channel) as client:
            resp = await client.post("/turn", json={"text": "hi"}, headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["reply"] == "via reply"
        assert resp.json()["finished"] is True

    async def test_channel_message_shape(self, store, fallback, clock) -> None:
        channel = _channel(store, fallback, clock)
        seen: list[ChannelMessage] = []

        async def handler(msg: ChannelMessage) -> None:
            seen.append(msg)
            await channel.send(msg.conversation_key, "ok", kind="final")

        channel.register_handler(handler)
        async with _client(channel) as client:
            await client.post("/turn", json={"text": "what time is it"}, headers=AUTH)
            await client.post("/turn", json={"text": "and the weather"}, headers=AUTH)

        assert len(seen) == 2
        for msg in seen:
            assert msg.channel == "siri"
            assert msg.user_id == "siri"
            assert msg.attachments == ()
            assert isinstance(msg.message_id, str) and msg.message_id != ""
        assert seen[0].text == "what time is it"
        assert seen[1].text == "and the weather"
        # message ids are unique per request
        assert seen[0].message_id != seen[1].message_id

    async def test_custom_user_id(self, store, fallback, clock) -> None:
        channel = _channel(store, fallback, clock, user_id="alice")
        seen: list[ChannelMessage] = []

        async def handler(msg: ChannelMessage) -> None:
            seen.append(msg)
            await channel.send(msg.conversation_key, "ok", kind="final")

        channel.register_handler(handler)
        async with _client(channel) as client:
            await client.post("/turn", json={"text": "hi"}, headers=AUTH)
        assert seen[0].user_id == "alice"

    async def test_status_then_final_returns_the_final(self, store, fallback, clock) -> None:
        channel = _channel(store, fallback, clock)

        async def handler(msg: ChannelMessage) -> None:
            await channel.send(msg.conversation_key, "working...", kind="status")
            await channel.send(msg.conversation_key, "the final", kind="final")

        channel.register_handler(handler)
        async with _client(channel) as client:
            resp = await client.post("/turn", json={"text": "hi"}, headers=AUTH)
        assert resp.json()["reply"] == "the final"
        assert resp.json()["finished"] is True

    async def test_handler_completes_with_only_status_returns_last_status(
        self, store, fallback, clock
    ) -> None:
        channel = _channel(store, fallback, clock)

        async def handler(msg: ChannelMessage) -> None:
            await channel.send(msg.conversation_key, "status one", kind="status")
            await channel.send(msg.conversation_key, "status two", kind="status")

        channel.register_handler(handler)
        async with _client(channel) as client:
            resp = await client.post("/turn", json={"text": "hi"}, headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["reply"] == "status two"
        assert resp.json()["finished"] is True

    async def test_handler_completes_with_no_sends_returns_no_reply_text(
        self, store, fallback, clock
    ) -> None:
        channel = _channel(store, fallback, clock)

        async def handler(msg: ChannelMessage) -> None:
            return None

        channel.register_handler(handler)
        async with _client(channel) as client:
            resp = await client.post("/turn", json={"text": "hi"}, headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["reply"] == NO_REPLY_TEXT
        assert resp.json()["finished"] is True


# --- SIRI-TLC-2: deadline honesty ------------------------------------------


class TestDeadline:
    async def test_deadline_returns_working_text_then_delivers_via_fallback(
        self, store, fallback, clock
    ) -> None:
        channel = _channel(store, fallback, clock, deadline_s=0.05)
        release = asyncio.Event()

        async def handler(msg: ChannelMessage) -> None:
            await release.wait()
            await channel.send(msg.conversation_key, "late final", kind="final")

        channel.register_handler(handler)
        try:
            loop = asyncio.get_running_loop()
            async with _client(channel) as client:
                t0 = loop.time()
                resp = await asyncio.wait_for(
                    client.post("/turn", json={"text": "hi"}, headers=AUTH), timeout=5
                )
                elapsed = loop.time() - t0

                # Deadline honesty: the response landed promptly, not at
                # the handler's leisure.
                assert elapsed < 2.0
                assert resp.status_code == 200
                data = resp.json()
                assert data["reply"] == WORKING_TEXT
                assert data["finished"] is False
                assert data["conversation_key"] == f"siri:{int(NOW.timestamp())}"
                assert fallback.calls == []

                # The handler keeps running; its final is rerouted to the
                # fallback with the "[siri] " prefix — exactly once.
                release.set()
                await asyncio.wait_for(fallback.arrived.wait(), timeout=5)
                await _settle()
                assert fallback.calls == ["[siri] late final"]
        finally:
            release.set()


# --- SIRI-UoW-2: per-request future plumbing (direct `send` calls) ---------


class TestSendSemantics:
    async def test_final_with_no_waiter_reroutes_to_fallback(self, store, fallback, clock) -> None:
        channel = _channel(store, fallback, clock)
        result = await channel.send("siri:123", "orphan final", kind="final")
        assert result == []
        assert fallback.calls == ["[siri] orphan final"]

    async def test_reply_with_no_waiter_reroutes_to_fallback(self, store, fallback, clock) -> None:
        channel = _channel(store, fallback, clock)
        await channel.send("siri:123", "orphan reply", kind="reply")
        assert fallback.calls == ["[siri] orphan reply"]

    async def test_status_never_resolves_and_never_hits_fallback(
        self, store, fallback, clock
    ) -> None:
        channel = _channel(store, fallback, clock)
        result = await channel.send("siri:123", "a status", kind="status")
        assert result == []
        assert fallback.calls == []

    async def test_concurrent_requests_on_distinct_keys_do_not_cross(
        self, store, fallback, clock
    ) -> None:
        channel = _channel(store, fallback, clock, deadline_s=5.0)
        hold = asyncio.Event()
        arrived = asyncio.Event()
        seen: list[ChannelMessage] = []

        async def handler(msg: ChannelMessage) -> None:
            seen.append(msg)
            arrived.set()
            await hold.wait()

        channel.register_handler(handler)
        try:
            async with _client(channel) as client:
                t1 = int(NOW.timestamp())
                task_a = asyncio.create_task(client.post("/turn", json={"text": "a"}, headers=AUTH))
                await asyncio.wait_for(arrived.wait(), timeout=5)
                arrived.clear()

                # Advance the clock and force a mint so B gets its own key.
                clock.now = NOW + timedelta(hours=1)
                t2 = int(clock.now.timestamp())
                task_b = asyncio.create_task(
                    client.post("/turn", json={"text": "b", "new": True}, headers=AUTH)
                )
                await asyncio.wait_for(arrived.wait(), timeout=5)

                key_a = f"siri:{t1}"
                key_b = f"siri:{t2}"
                assert key_a != key_b

                # Resolving B must not touch A's waiter.
                assert await channel.send(key_b, "reply B", kind="final") == []
                resp_b = await asyncio.wait_for(task_b, timeout=5)
                assert resp_b.json() == {
                    "reply": "reply B",
                    "conversation_key": key_b,
                    "finished": True,
                }
                assert not task_a.done()

                await channel.send(key_a, "reply A", kind="final")
                resp_a = await asyncio.wait_for(task_a, timeout=5)
                assert resp_a.json()["reply"] == "reply A"
                assert resp_a.json()["conversation_key"] == key_a
                assert resp_a.json()["finished"] is True

                # Both were delivered to live waiters; no fallback traffic.
                await _settle()
                assert fallback.calls == []
        finally:
            hold.set()

    async def test_one_final_resolves_all_waiters_on_the_same_key(
        self, store, fallback, clock
    ) -> None:
        channel = _channel(store, fallback, clock, deadline_s=5.0)
        hold = asyncio.Event()
        seen: list[ChannelMessage] = []

        async def handler(msg: ChannelMessage) -> None:
            seen.append(msg)
            await hold.wait()

        channel.register_handler(handler)
        try:
            async with _client(channel) as client:
                # Same fixed clock + empty store: both requests resolve to
                # the same minted key.
                task_a = asyncio.create_task(client.post("/turn", json={"text": "a"}, headers=AUTH))
                task_b = asyncio.create_task(client.post("/turn", json={"text": "b"}, headers=AUTH))
                await _wait_until(lambda: len(seen) == 2)
                key = f"siri:{int(NOW.timestamp())}"

                await channel.send(key, "for everyone", kind="final")
                resp_a = await asyncio.wait_for(task_a, timeout=5)
                resp_b = await asyncio.wait_for(task_b, timeout=5)
                for resp in (resp_a, resp_b):
                    assert resp.json()["reply"] == "for everyone"
                    assert resp.json()["finished"] is True
                    assert resp.json()["conversation_key"] == key
        finally:
            hold.set()


# --- send_attachments / no-ops ---------------------------------------------


class TestOtherChannelSurface:
    async def test_send_attachments_reroutes_paths_to_fallback(
        self, store, fallback, clock
    ) -> None:
        channel = _channel(store, fallback, clock)
        await channel.send_attachments("siri:123", ["/data/a.png", "/data/b.pdf"])
        assert len(fallback.calls) == 1
        assert fallback.calls[0].startswith("[siri] ")
        assert "/data/a.png" in fallback.calls[0]
        assert "/data/b.pdf" in fallback.calls[0]

    async def test_set_typing_and_notify_scheduler_are_noops(self, store, fallback, clock) -> None:
        channel = _channel(store, fallback, clock)
        await channel.set_typing("siri:123", True)
        await channel.set_typing("siri:123", False)
        await channel.notify_scheduler("scheduler says hi")
        assert fallback.calls == []


# --- Session-window resolution through HTTP --------------------------------


class TestSessionWindowOverHttp:
    """Cases (a)-(e): fake clock + pre-seeded store drive the
    continue-vs-mint decision observable in `conversation_key`."""

    @staticmethod
    def _echo_handler(channel: SiriChannel):
        async def handler(msg: ChannelMessage) -> None:
            await channel.send(msg.conversation_key, f"echo:{msg.text}", kind="final")

        return handler

    async def test_fresh_store_mints_epoch_key(self, store, fallback, clock) -> None:
        channel = _channel(store, fallback, clock)
        channel.register_handler(self._echo_handler(channel))
        async with _client(channel) as client:
            resp = await client.post("/turn", json={"text": "hi"}, headers=AUTH)
        assert resp.json()["conversation_key"] == f"siri:{int(NOW.timestamp())}"

    async def test_within_window_reuses_newest_key(self, store, fallback, clock) -> None:
        await _seed(store, "siri:1000", NOW - timedelta(seconds=WINDOW_S // 2))
        channel = _channel(store, fallback, clock)
        channel.register_handler(self._echo_handler(channel))
        async with _client(channel) as client:
            resp = await client.post("/turn", json={"text": "hi"}, headers=AUTH)
        assert resp.json()["conversation_key"] == "siri:1000"

    async def test_beyond_window_mints_new_key(self, store, fallback, clock) -> None:
        await _seed(store, "siri:1000", NOW - timedelta(seconds=WINDOW_S + 60))
        channel = _channel(store, fallback, clock)
        channel.register_handler(self._echo_handler(channel))
        async with _client(channel) as client:
            resp = await client.post("/turn", json={"text": "hi"}, headers=AUTH)
        assert resp.json()["conversation_key"] == f"siri:{int(NOW.timestamp())}"

    async def test_beyond_window_with_pending_reuses_old_key(self, store, fallback, clock) -> None:
        cid = await _seed(store, "siri:1000", NOW - timedelta(seconds=WINDOW_S + 60))
        await store._c.execute(
            "UPDATE conversations SET pending_tool_confirmation = ? WHERE id = ?",
            ('{"tool": "x"}', cid),
        )
        await store._c.commit()
        channel = _channel(store, fallback, clock)
        channel.register_handler(self._echo_handler(channel))
        async with _client(channel) as client:
            resp = await client.post("/turn", json={"text": "yes do it"}, headers=AUTH)
        assert resp.json()["conversation_key"] == "siri:1000"

    async def test_new_true_mints_even_within_window(self, store, fallback, clock) -> None:
        await _seed(store, "siri:1000", NOW - timedelta(seconds=5))
        channel = _channel(store, fallback, clock)
        channel.register_handler(self._echo_handler(channel))
        async with _client(channel) as client:
            resp = await client.post("/turn", json={"text": "hi", "new": True}, headers=AUTH)
        assert resp.json()["conversation_key"] == f"siri:{int(NOW.timestamp())}"
