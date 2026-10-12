"""`/schwab-reauth` Discord slash command + redirect-URL pre-handler (BR-UoW-7,
BR-FC-11).

Step 7 of the brokerage feature: a user-initiated two-message handshake to
refresh the Schwab OAuth token without an SSH tunnel. This test file is on a
test-only branch (`test/brokerage-5-7`) and references symbols the implementer
is creating on a parallel branch:

  - `ember.commands.schwab_reauth.SchwabReauthCommand` — the slash command.
  - `Store.set_pending_schwab_reauth(conv_id, payload_json)` /
    `Store.get_pending_schwab_reauth(conv_id) -> str | None` /
    `Store.consume_pending_schwab_reauth(conv_id) -> str | None`
    — repo methods mirroring `pending_tool_confirmation`.
  - schema v=9 adding `pending_schwab_reauth TEXT` nullable column.
  - Agent pre-handler branch: when a `pending_schwab_reauth` row exists AND
    the user message body matches the redirect URL shape
    (`https://127.0.0.1:8182/?code=...&state=...`), intercept BEFORE the
    provider, validate state, call `schwab.auth.client_from_received_url`,
    write token, clear pending, reply with refresh status.

Tester-flagged ambiguities (encoded as test-side assumptions):

1. **Redirect-URL hostname strictness** — BR-UoW-7 spec uses
   `https://127.0.0.1:8182` as the canonical callback. Tests use exactly
   that host:port literal in messages. We do NOT assert the implementer
   accepts any other host (the contract pins one).

2. **TTL boundary** — spec says "5 minutes" and "expires_at: now+300".
   Tests assert ranges (290..310s window for the freshly-set state) and
   accept any int unix-second representation. The expired test uses
   `now-100` so there is no edge case.

3. **State-mismatch reply text** — spec says "reply with the mismatch +
   restart instructions." Tests look for the substring "mismatch" OR
   "state" plus a hint to re-run; we don't pin exact wording.

4. **TTL-expired reply text** — spec says "reply 're-auth expired; run
   `/schwab-reauth` again'." Tests match on "expired" plus a re-run hint.

5. **Token write format** — spec says the token writes to disk via
   `client_from_received_url`'s `token_write_func`. Tests pass the
   callback through; the fake `client_from_received_url` invokes
   `token_write_func(t)` with a fake token dict. Test asserts that the
   target file contains the JSON-serialized token (no double-wrap).

6. **Successful reply text** — spec says "Ember replies with refresh
   status (token expiry date)." Tests match on "token" + "refresh"
   case-insensitive; we don't pin the date format.

7. **No-pending fall-through** — spec says the pre-handler must require
   `pending_schwab_reauth`; URL shape alone is NOT enough to intercept.
   Test injects a redirect-URL-shaped string with NO pending state and
   asserts the provider is called.

8. **Error during token exchange** — spec failure-mode "Schwab
   token-exchange HTTP error → surface error_description, do not log
   the code or token contents." Tests pass an exception; assert pending
   state cleared, no token file written, and a status/final mentioning
   error or failure landed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from ember.agent import Agent
from ember.channels.base import ChannelMessage
from ember.commands.base import CommandContext
from ember.commands.registry import CommandRegistry
from ember.config import Settings
from ember.identity.store import IdentityStore
from ember.providers.routing import ProviderRouter
from ember.store.repo import Store
from ember.tools.registry import Registry
from tests.fakes.fake_channel import FakeChannel
from tests.fakes.fake_provider import FakeProvider


# ---------------------------------------------------------------------------
# Settings + agent wiring helpers (mirror test_tool_schwab.py +
# test_agent_tlc3.py patterns).
# ---------------------------------------------------------------------------


def _make_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    app_key: str = "schwab-app-key-XXX",
    app_secret: str = "schwab-app-secret-YYY",
) -> Settings:
    """Build a Settings object via env vars (production path)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "1")
    monkeypatch.setenv("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
    monkeypatch.setenv("DISCORD_SCHEDULER_CHANNEL_ID", "3")
    for k in (
        "SCHWAB_APP_KEY",
        "SCHWAB_APP_SECRET",
        "SCHWAB_CALLBACK_URL",
        "SCHWAB_ACCOUNT_NAMES",
        "SCHWAB_ALLOWED_ACCOUNTS",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SCHWAB_APP_KEY", app_key)
    monkeypatch.setenv("SCHWAB_APP_SECRET", app_secret)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return Settings()  # type: ignore[call-arg]


def _schwab_data_dir(tmp_path: Path) -> Path:
    p = tmp_path / "schwab"
    p.mkdir(mode=0o700, exist_ok=True)
    return p


@pytest.fixture
async def opened_store(tmp_path: Path):
    store = Store(tmp_path / "conversations.db")
    await store.open()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
async def identity(tmp_path: Path) -> IdentityStore:
    s = IdentityStore(tmp_path / "identity")
    await s.ensure_initialized()
    return s


def _named_provider(name: str) -> FakeProvider:
    fp = FakeProvider()
    fp.name = name
    return fp


def _make_agent(
    *,
    store: Store,
    settings: Settings,
    identity: IdentityStore | None = None,
    tool_registry: Registry | None = None,
) -> tuple[Agent, FakeChannel, FakeProvider]:
    primary = _named_provider("claude_cli")
    router = ProviderRouter({"claude_cli": primary})
    channel = FakeChannel()
    agent = Agent(
        store=store,
        channel=channel,
        settings=settings,
        router=router,
        identity=identity,
        tool_registry=tool_registry,
    )
    return agent, channel, primary


def _msg(
    text: str,
    *,
    key: str = "thread-1",
    mid: str = "m1",
    user: str = "1000",
) -> ChannelMessage:
    return ChannelMessage(
        channel="discord",
        conversation_key=key,
        user_id=user,
        text=text,
        message_id=mid,
    )


def _ctx(
    store: Store,
    settings: Settings,
    registry: CommandRegistry,
    *,
    conversation_key: str | None = "thread-1",
    args: dict[str, Any] | None = None,
) -> CommandContext:
    return CommandContext(
        conversation_key=conversation_key,
        user_id="1000",
        store=store,
        settings=settings,
        registry=registry,
        args=args or {},
    )


# ---------------------------------------------------------------------------
# Fake schwab-py auth surface
# ---------------------------------------------------------------------------


class _FakeAuthContext(NamedTuple):
    """Mirrors `schwab.auth.AuthContext` shape returned by
    `get_auth_context(api_key, callback_url)`. The implementer reads
    `.state` (and optionally `.authorization_url` / `.callback_url`)
    when persisting pending state and rendering the reply.

    Backed by a NamedTuple to match the real schwab-py shape — that
    means `.state = ...` raises (correctly) and the implementer must
    use `_replace()` to rebuild the context with the persisted state.
    """

    state: str
    authorization_url: str
    callback_url: str

    @staticmethod
    def make(
        *,
        state: str = "ABCDEF",
        authorization_url: str | None = None,
        callback_url: str = "https://127.0.0.1:8182",
    ) -> "_FakeAuthContext":
        return _FakeAuthContext(
            state=state,
            authorization_url=(
                authorization_url
                or f"https://api.schwabapi.com/v1/oauth/authorize?response_type=code&state={state}"
            ),
            callback_url=callback_url,
        )


def _patch_get_auth_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: str = "ABCDEF",
    raises: BaseException | None = None,
) -> dict[str, Any]:
    """Patch `schwab.auth.get_auth_context` to return a `_FakeAuthContext`
    with the given state. Records every invocation in the returned dict."""
    record: dict[str, Any] = {"calls": [], "state": state}

    def _stub(*args: Any, **kwargs: Any) -> _FakeAuthContext:
        record["calls"].append({"args": args, "kwargs": kwargs})
        if raises is not None:
            raise raises
        return _FakeAuthContext.make(state=state)

    try:
        import schwab.auth as schwab_auth  # type: ignore[import-not-found]

        monkeypatch.setattr(schwab_auth, "get_auth_context", _stub, raising=False)
    except Exception:
        import sys as _sys
        import types as _types

        synthetic = _sys.modules.get("schwab.auth")
        if synthetic is None:
            synthetic = _types.ModuleType("schwab.auth")
            _sys.modules.setdefault("schwab", _types.ModuleType("schwab"))
            _sys.modules["schwab.auth"] = synthetic
        synthetic.get_auth_context = _stub  # type: ignore[attr-defined]
    return record


def _patch_client_from_received_url(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raises: BaseException | None = None,
    fake_token: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Patch `schwab.auth.client_from_received_url` to record args and (by
    default) write a fake token through the supplied `token_write_func`.

    The implementer's pre-handler should call this with `token_write_func`
    that does `json.dump(t, f)` (no double-wrap). Our fake invokes the
    `token_write_func` with `fake_token` so we can assert the on-disk
    canonical shape afterwards.
    """
    record: dict[str, Any] = {"calls": []}
    token = fake_token or {
        "creation_timestamp": int(time.time()),
        "token": {
            "access_token": "atk_fake",
            "refresh_token": "rtk_fake",
            "expires_in": 1800,
        },
    }

    def _stub(*args: Any, **kwargs: Any) -> Any:
        record["calls"].append({"args": args, "kwargs": kwargs})
        if raises is not None:
            raise raises
        # Find any `token_write_func` (positional or keyword) and call
        # it with the canonical token dict so the implementer's wrapper
        # can write it to disk.
        twf = kwargs.get("token_write_func")
        if twf is None:
            for a in args:
                if callable(a):
                    twf = a
                    break
        if twf is not None:
            try:
                twf(token)
            except Exception:
                pass
        return object()

    try:
        import schwab.auth as schwab_auth  # type: ignore[import-not-found]

        monkeypatch.setattr(schwab_auth, "client_from_received_url", _stub, raising=False)
    except Exception:
        import sys as _sys
        import types as _types

        synthetic = _sys.modules.get("schwab.auth")
        if synthetic is None:
            synthetic = _types.ModuleType("schwab.auth")
            _sys.modules.setdefault("schwab", _types.ModuleType("schwab"))
            _sys.modules["schwab.auth"] = synthetic
        synthetic.client_from_received_url = _stub  # type: ignore[attr-defined]
    return record


def _seed_pending_reauth(
    *,
    state: str = "ZZZ",
    expires_at: int | None = None,
    callback_url: str = "https://127.0.0.1:8182",
    pkce_verifier: str | None = None,
) -> str:
    """Build the JSON payload the agent's pre-handler will read from
    `pending_schwab_reauth`. Returns the JSON-encoded string."""
    if expires_at is None:
        expires_at = int(time.time()) + 200
    payload = {
        "state": state,
        "callback_url": callback_url,
        "expires_at": expires_at,
        "pkce_verifier": pkce_verifier,
    }
    return json.dumps(payload)


def _redirect_url(state: str = "ZZZ", code: str = "ABC") -> str:
    return f"https://127.0.0.1:8182/?code={code}&state={state}"


# ===========================================================================
# Slash command — `/schwab-reauth` (BR-FC-11 §1)
# ===========================================================================


class TestSchwabReauthCommand:
    async def test_returns_auth_url_and_persists_pending(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
    ) -> None:
        """The command:
        - calls `schwab.auth.get_auth_context(api_key, callback_url)`;
        - persists `pending_schwab_reauth = {state, callback_url, expires_at}`
          on the conversation row;
        - replies with the auth URL inline plus a "30 seconds" timing
          reminder.
        """
        from ember.commands.schwab_reauth import SchwabReauthCommand

        settings = _make_settings(tmp_path, monkeypatch)
        rec = _patch_get_auth_context(monkeypatch, state="ABCDEF")

        registry = CommandRegistry()
        cmd = SchwabReauthCommand()
        before = time.time()
        out = await cmd.run(_ctx(opened_store, settings, registry))
        after = time.time()

        # Reply contains the auth URL.
        assert isinstance(out, str)
        assert "https://api.schwabapi.com/" in out, f"reply missing auth URL; got {out!r}"
        # Reply contains the 30-second timing reminder.
        assert "30" in out and ("second" in out.lower() or "sec" in out.lower()), (
            f"reply missing 30-second timing reminder; got {out!r}"
        )

        # `get_auth_context` was called exactly once with the configured
        # api_key + callback_url. We don't pin positional vs keyword, but
        # the values must appear somewhere.
        assert len(rec["calls"]) == 1
        call_args = rec["calls"][0]
        joined = json.dumps({"args": call_args["args"], "kwargs": call_args["kwargs"]})
        assert "schwab-app-key-XXX" in joined
        assert "https://127.0.0.1:8182" in joined

        # Pending state persisted.
        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        blob = await opened_store.get_pending_schwab_reauth(conv.id)
        assert blob is not None, "expected pending_schwab_reauth to be set"
        parsed = json.loads(blob)
        assert parsed["state"] == "ABCDEF"
        # expires_at is `now + 300` ± slack window for clock-edge.
        ttl = int(parsed["expires_at"]) - int(before)
        assert 290 <= ttl <= 310, (
            f"expected expires_at within 290..310s of call time; got {ttl}s "
            f"(payload: {parsed!r}, before={before}, after={after})"
        )

    async def test_out_of_thread_returns_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
    ) -> None:
        """Mirrors the existing `_OUT_OF_THREAD` shape — when invoked
        without a conversation_key, the command returns an info string
        and never calls into schwab.auth."""
        from ember.commands.schwab_reauth import SchwabReauthCommand

        settings = _make_settings(tmp_path, monkeypatch)
        rec = _patch_get_auth_context(monkeypatch)
        registry = CommandRegistry()
        cmd = SchwabReauthCommand()

        out = await cmd.run(_ctx(opened_store, settings, registry, conversation_key=None))
        assert isinstance(out, str)
        assert out != ""
        lowered = out.lower()
        assert "conversation" in lowered or "thread" in lowered or "must" in lowered, (
            f"expected out-of-thread info shape; got {out!r}"
        )
        # No call into schwab.auth.
        assert rec["calls"] == [], "out-of-thread must not call get_auth_context"

    async def test_handles_get_auth_context_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
    ) -> None:
        """If `get_auth_context` raises, the reply surfaces an error
        gracefully (does not propagate the exception out of `run`),
        and no pending state is written to the conversation row."""
        from ember.commands.schwab_reauth import SchwabReauthCommand

        settings = _make_settings(tmp_path, monkeypatch)
        _patch_get_auth_context(monkeypatch, raises=RuntimeError("schwab unreachable"))
        registry = CommandRegistry()
        cmd = SchwabReauthCommand()

        # Must not raise — channel-level commands return their own status
        # string per the SlashCommand protocol.
        out = await cmd.run(_ctx(opened_store, settings, registry))
        assert isinstance(out, str)
        assert out != ""
        lowered = out.lower()
        assert "fail" in lowered or "error" in lowered or "unreachable" in lowered or "❌" in out, (
            f"expected failure-flavored reply; got {out!r}"
        )

        # Pending row NOT persisted on failure.
        conv = await opened_store.get_or_create_conversation("discord", "thread-1")
        blob = await opened_store.get_pending_schwab_reauth(conv.id)
        assert blob is None, f"failure path must not persist pending state; got {blob!r}"


# ===========================================================================
# Pre-handler in agent loop (BR-FC-11 §2)
# ===========================================================================


async def _seed_conversation_with_pending(
    store: Store,
    *,
    state: str = "ZZZ",
    expires_at: int | None = None,
    key: str = "thread-1",
) -> int:
    """Create a conversation row and stash a pending_schwab_reauth blob.
    Returns the conversation id."""
    conv = await store.get_or_create_conversation("discord", key)
    payload = _seed_pending_reauth(state=state, expires_at=expires_at)
    await store.set_pending_schwab_reauth(conv.id, payload)
    return conv.id


class TestPreHandlerRedirectIntercept:
    async def test_redirect_url_intercepted_when_pending_state_exists(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """Pending state persisted; user pastes the redirect URL → the
        pre-handler intercepts BEFORE the provider:

        - schwab.auth.client_from_received_url called with the persisted
          auth_context (state=ZZZ);
        - pending_schwab_reauth cleared after success;
        - reply mentions "token refreshed" or similar;
        - the `token_write_func` wrote the token to disk in canonical
          format (`json.dump(t, f)` — no double-wrap).
        - provider was NOT called.
        """
        settings = _make_settings(tmp_path, monkeypatch)
        # Stub get_auth_context too: the implementer may reconstruct
        # the auth_context from the persisted fields by calling
        # `get_auth_context` again, or may pass values directly. Either
        # way, neither path should reach the provider.
        get_ctx = _patch_get_auth_context(monkeypatch, state="ZZZ")
        cfru = _patch_client_from_received_url(monkeypatch)

        agent, channel, primary = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=Registry(),
        )
        await channel.start(agent.handle_turn)

        conv_id = await _seed_conversation_with_pending(opened_store, state="ZZZ")

        # Token target — the schwab data dir.
        data_dir = _schwab_data_dir(tmp_path)
        token_path = data_dir / ".schwab_token.json"
        # Ensure the file does not pre-exist.
        if token_path.exists():
            token_path.unlink()

        await channel.inject(_msg(_redirect_url(state="ZZZ", code="ABC"), mid="m_paste"))

        # Provider was NOT called.
        assert primary.calls == [], (
            f"redirect-URL paste with pending state must short-circuit; "
            f"provider was called {len(primary.calls)} times"
        )

        # client_from_receive_url WAS called once.
        assert len(cfru["calls"]) == 1, (
            f"expected exactly one client_from_received_url call; got {cfru['calls']!r}"
        )
        # The state must round-trip through the args; we accept either a
        # received URL string, an auth_context object, or both as
        # positional/keyword args.
        joined = json.dumps(
            {
                "args": [str(a) for a in cfru["calls"][0]["args"]],
                "kwargs": {k: str(v) for k, v in cfru["calls"][0]["kwargs"].items()},
            }
        )
        assert "ZZZ" in joined or "code=ABC" in joined, (
            f"expected the redirect URL or persisted state to appear in "
            f"client_from_received_url args; got {joined!r}"
        )

        # Pending state cleared after success.
        blob_after = await opened_store.get_pending_schwab_reauth(conv_id)
        assert blob_after is None, (
            f"pending_schwab_reauth must be cleared after successful refresh; got {blob_after!r}"
        )

        # Reply mentions token + refresh.
        outs = [s.content for s in channel.sent if s.kind in ("status", "final")]
        joined_replies = "\n".join(outs).lower()
        assert "token" in joined_replies and "refresh" in joined_replies, (
            f"expected reply mentioning token+refresh; got {outs!r}"
        )

        # Token file on disk: canonical shape, no double-wrap.
        # The implementer's `token_write_func` should be `lambda t, f=...: json.dump(t, f)`
        # (or equivalent), so the resulting file content is a JSON object with
        # `creation_timestamp` + `token` at the top level — NOT wrapped in
        # another envelope.
        if token_path.exists():
            on_disk = json.loads(token_path.read_text())
            assert isinstance(on_disk, dict)
            # Canonical shape — top-level keys, not wrapped.
            assert "token" in on_disk or "access_token" in on_disk, (
                f"token file should have canonical top-level keys "
                f"(token / access_token), got: {list(on_disk.keys())!r}"
            )
            # Detect double-wrap: a single key whose value is another
            # dict that itself contains the canonical fields.
            if (
                len(on_disk) == 1
                and isinstance(next(iter(on_disk.values())), dict)
                and "token" in next(iter(on_disk.values()))
            ):
                pytest.fail(
                    f"token file is double-wrapped: {on_disk!r}; expected the "
                    f"canonical (creation_timestamp, token) shape directly"
                )

        # Sanity: get_auth_context may or may not have been called by the
        # pre-handler (depends on whether the implementer reconstructs vs
        # caches). We don't assert exact count.
        del get_ctx

    async def test_state_mismatch_clears_pending_and_replies_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """Pending state="EXPECTED" + redirect URL with state="DIFFERENT"
        → provider NOT called, schwab.auth.client_from_received_url NOT
        called, pending state cleared, reply mentions mismatch."""
        settings = _make_settings(tmp_path, monkeypatch)
        _patch_get_auth_context(monkeypatch, state="EXPECTED")
        cfru = _patch_client_from_received_url(monkeypatch)

        agent, channel, primary = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=Registry(),
        )
        await channel.start(agent.handle_turn)

        conv_id = await _seed_conversation_with_pending(opened_store, state="EXPECTED")

        await channel.inject(_msg(_redirect_url(state="DIFFERENT", code="ABC"), mid="m_mismatch"))

        # Provider NOT called.
        assert primary.calls == [], "state-mismatch must short-circuit before reaching the provider"
        # Token exchange NOT attempted.
        assert cfru["calls"] == [], (
            f"client_from_received_url must NOT run on state mismatch; got {cfru['calls']!r}"
        )

        # Pending state cleared.
        blob_after = await opened_store.get_pending_schwab_reauth(conv_id)
        assert blob_after is None, (
            f"pending_schwab_reauth must be cleared on mismatch; got {blob_after!r}"
        )

        # Reply mentions mismatch (or "state").
        outs = [s.content for s in channel.sent if s.kind in ("status", "final")]
        joined = "\n".join(outs).lower()
        assert "mismatch" in joined or "state" in joined, (
            f"expected mismatch/state in reply; got {outs!r}"
        )

    async def test_ttl_expired_clears_and_replies_expired(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """Pending state with `expires_at = now - 100`. Send any message.
        Pre-handler must clear the pending row and reply "expired" plus
        a "/schwab-reauth" hint to re-run."""
        settings = _make_settings(tmp_path, monkeypatch)
        _patch_get_auth_context(monkeypatch, state="ZZZ")
        cfru = _patch_client_from_received_url(monkeypatch)

        agent, channel, primary = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=Registry(),
        )
        await channel.start(agent.handle_turn)

        conv_id = await _seed_conversation_with_pending(
            opened_store, state="ZZZ", expires_at=int(time.time()) - 100
        )

        # Even pasting a valid-looking redirect URL must trigger the
        # expired branch because TTL is checked first.
        await channel.inject(_msg(_redirect_url(state="ZZZ", code="ABC"), mid="m_expired"))

        # No token exchange attempted.
        assert cfru["calls"] == [], (
            f"client_from_received_url must NOT run when TTL has expired; got {cfru['calls']!r}"
        )

        # Pending state cleared.
        blob_after = await opened_store.get_pending_schwab_reauth(conv_id)
        assert blob_after is None, (
            f"expired pending_schwab_reauth must be cleared; got {blob_after!r}"
        )

        # Reply mentions expired + a re-run hint.
        outs = [s.content for s in channel.sent if s.kind in ("status", "final")]
        joined = "\n".join(outs).lower()
        assert "expired" in joined, f"expected 'expired' in reply; got {outs!r}"
        assert "schwab-reauth" in joined or "/schwab" in joined or "again" in joined, (
            f"expected a re-run hint in reply; got {outs!r}"
        )

        # The provider may or may not be called on the expired branch
        # depending on implementer choice (spec is ambiguous: "let the
        # message flow normally" vs "reply 'expired' and stop"). The
        # contract for this test pins option B: reply expired + stop.
        # If the implementer goes with option A (let it flow), this
        # assertion will need to be revisited. Encoded for clarity:
        assert primary.calls == [], (
            "expired branch is expected to short-circuit (BR-UoW-7 failure-mode "
            "wording: 'reply re-auth expired; run /schwab-reauth again, clear "
            "state'); provider must not be called"
        )

    async def test_redirect_url_when_no_pending_state_falls_through_to_provider(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """No pending state. User message body is a redirect-URL-shaped
        string. Pre-handler must NOT intercept based on URL shape
        alone — provider IS called."""
        settings = _make_settings(tmp_path, monkeypatch)
        _patch_get_auth_context(monkeypatch)
        cfru = _patch_client_from_received_url(monkeypatch)

        agent, channel, primary = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=Registry(),
        )
        await channel.start(agent.handle_turn)

        # No seed call — there is no pending_schwab_reauth row.
        primary.queue_text("ack")
        await channel.inject(_msg(_redirect_url(state="WHATEVER", code="ABC"), mid="m_no_pending"))

        # client_from_received_url NOT called.
        assert cfru["calls"] == [], (
            f"pre-handler must require pending state; got cfru calls {cfru['calls']!r}"
        )
        # Provider WAS called.
        assert len(primary.calls) == 1, (
            "absent pending state, redirect-URL message must flow to provider"
        )
        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1
        assert finals[0].content == "ack"

    async def test_non_redirect_message_when_pending_state_falls_through(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """Pending state exists; user types a non-URL message. Pre-handler
        must NOT intercept; provider IS called; pending state remains
        (TTL still ticking)."""
        settings = _make_settings(tmp_path, monkeypatch)
        _patch_get_auth_context(monkeypatch)
        cfru = _patch_client_from_received_url(monkeypatch)

        agent, channel, primary = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=Registry(),
        )
        await channel.start(agent.handle_turn)

        conv_id = await _seed_conversation_with_pending(opened_store, state="ZZZ")

        primary.queue_text("hmm, what command was that")
        await channel.inject(_msg("what was that command?", mid="m_chitchat"))

        # Token exchange NOT attempted.
        assert cfru["calls"] == [], (
            f"non-URL message must not trigger token exchange; got {cfru['calls']!r}"
        )
        # Provider WAS called normally.
        assert len(primary.calls) == 1
        finals = [s for s in channel.sent if s.kind == "final"]
        assert len(finals) == 1
        assert finals[0].content == "hmm, what command was that"

        # Pending state still present (TTL still ticking).
        blob_after = await opened_store.get_pending_schwab_reauth(conv_id)
        assert blob_after is not None, (
            "pending_schwab_reauth must NOT be cleared by an unrelated user "
            "message — TTL keeps ticking until either expiry or the redirect "
            "URL paste"
        )
        parsed = json.loads(blob_after)
        assert parsed["state"] == "ZZZ"

    async def test_pre_handler_fail_closed_on_token_exchange_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        opened_store: Store,
        identity: IdentityStore,
    ) -> None:
        """Pending state matches → pre-handler invokes
        `client_from_received_url`, which raises (e.g., expired auth
        code). Pre-handler must:

        - clear the pending state (so the user can run /schwab-reauth
          again without stale data);
        - surface the error in the reply (status/final);
        - NOT write a token file;
        - NOT double-clear (single, idempotent clear is fine).
        """
        settings = _make_settings(tmp_path, monkeypatch)
        _patch_get_auth_context(monkeypatch, state="ZZZ")
        cfru = _patch_client_from_received_url(
            monkeypatch,
            raises=RuntimeError("Schwab token-exchange HTTP 400: code expired"),
        )

        agent, channel, primary = _make_agent(
            store=opened_store,
            settings=settings,
            identity=identity,
            tool_registry=Registry(),
        )
        await channel.start(agent.handle_turn)

        conv_id = await _seed_conversation_with_pending(opened_store, state="ZZZ")
        data_dir = _schwab_data_dir(tmp_path)
        token_path = data_dir / ".schwab_token.json"
        if token_path.exists():
            token_path.unlink()

        await channel.inject(_msg(_redirect_url(state="ZZZ", code="ABC"), mid="m_exch_fail"))

        # Token exchange WAS attempted (so we exercised the failure path).
        assert len(cfru["calls"]) == 1
        # No token file written.
        assert not token_path.exists(), (
            f"failed token exchange must not leave a token file at {token_path}"
        )
        # Pending state cleared.
        blob_after = await opened_store.get_pending_schwab_reauth(conv_id)
        assert blob_after is None, (
            f"failed token exchange must clear pending state; got {blob_after!r}"
        )
        # Reply mentions error / failure.
        outs = [s.content for s in channel.sent if s.kind in ("status", "final")]
        joined = "\n".join(outs).lower()
        assert (
            "fail" in joined or "error" in joined or "expired" in joined or "❌" in "\n".join(outs)
        ), f"expected failure-flavored reply; got {outs!r}"
        # Provider not called either way.
        assert primary.calls == []


# ===========================================================================
# Auth bootstrap fix — import-time safety in headless env
# ===========================================================================


class TestSchwabAuthImportHeadless:
    def test_schwab_auth_module_imports_cleanly_in_headless_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Monkeypatch `webbrowser.get` to raise `webbrowser.Error` and
        re-import `ember.tools.schwab_auth`. Module-level import should
        not crash.

        Rationale: in the existing implementation, the browser-launch
        stub is wrapped inside `_main` and only fires when run via
        `python -m`. If the implementer hoists the stub out of `_main`
        to make headless-detection reusable, we need a guarantee that
        the module is still safe to import on a headless host. The
        production agent imports schwab.py (which does NOT import
        schwab_auth), but a future refactor that pulls a shared helper
        out of schwab_auth must not break the import chain.
        """
        import importlib
        import webbrowser

        def _raise(*_a: Any, **_k: Any) -> Any:
            raise webbrowser.Error("no display")

        monkeypatch.setattr(webbrowser, "get", _raise)

        # Force a fresh import.
        import sys

        sys.modules.pop("ember.tools.schwab_auth", None)
        mod = importlib.import_module("ember.tools.schwab_auth")
        assert mod is not None
        # The module should expose either `_main` (current) or some
        # equivalent callable used by the `python -m` path. We don't
        # pin the symbol — just confirm import succeeded.
