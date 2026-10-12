"""`/schwab-reauth` — refresh the Schwab OAuth token via a two-message handshake.

Step 1 of BR-FC-11. The user runs `/schwab-reauth`; we mint a fresh OAuth
state token via ``schwab.auth.get_auth_context``, stash
``{state, callback_url, expires_at}`` on the conversation row with a
five-minute TTL, and reply with the auth URL plus pasteback instructions.

Step 2 happens inside ``Agent._handle_turn_inner``: a pre-handler peeks
at ``pending_schwab_reauth`` on every inbound message. When the message
body is a Schwab redirect URL it intercepts, rebuilds the
``auth_context`` (with the persisted state spliced back in for the CSRF
check), and exchanges the code for a token via
``schwab.auth.client_from_received_url``.

TLC-1 (Discord allowlist) is enforced by the channel adapter before any
slash command reaches us — no extra check needed here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import webbrowser as _webbrowser

from ember.commands.base import CommandContext, CommandOption
from ember.errors import ConfigError

_log = logging.getLogger("ember.commands.schwab_reauth")

_OUT_OF_THREAD = "ℹ️ `/schwab-reauth` must be invoked in a conversation thread."
_NOT_CONFIGURED = (
    "❌ Schwab is not configured. Set `SCHWAB_APP_KEY` and "
    "`SCHWAB_APP_SECRET` in `.env` and restart Ember."
)

# 5-minute TTL on the pasted-URL handshake. Schwab's authorization code
# itself only lives ~30 s after issuance, so the realistic deadline is
# tighter than this — but the TTL guards against a stale row sticking
# around forever if the user abandons the flow.
_REAUTH_TTL_SECONDS = 300

# schwab-py uses ``webbrowser.get(...).open(url)`` inside its sync auth
# helpers. On a headless host that raises ``webbrowser.Error`` from
# ``.get()`` before we can intercept. Stub it to a no-op controller at
# import time — the URL is delivered to the user via Discord, not via a
# local browser. Same pattern as ``ember.tools.schwab_auth``'s _main().


class _NoopBrowser:
    def open(self, *a: object, **kw: object) -> bool:  # noqa: D401
        return True


_webbrowser.get = lambda *a, **kw: _NoopBrowser()  # type: ignore[assignment]


class SchwabReauthCommand:
    name: str = "schwab-reauth"
    description: str = "Refresh the Schwab OAuth token via a two-message handshake"
    options: list[CommandOption] = []

    async def run(self, ctx: CommandContext) -> str:
        if ctx.conversation_key is None:
            return _OUT_OF_THREAD

        settings = ctx.settings
        app_key = settings.schwab_app_key
        callback_url = settings.schwab_callback_url
        if app_key is None or settings.schwab_app_secret is None:
            return _NOT_CONFIGURED

        try:
            import schwab.auth  # type: ignore
        except ImportError as e:
            raise ConfigError(f"schwab-py not installed: {e}") from None

        api_key_value = app_key.get_secret_value()

        # get_auth_context is sync + fast (no network), but does build
        # the authlib OAuth2 client. Run off-thread to keep with the
        # codebase convention for schwab-py calls. Wrap broadly: any
        # exception out of authlib (config, transitive import, etc.)
        # surfaces as a friendly reply rather than crashing the channel
        # message handler. No pending state is persisted on this path.
        try:
            auth_context = await asyncio.to_thread(
                schwab.auth.get_auth_context,
                api_key_value,
                callback_url,
            )
        except Exception as e:
            _log.warning("schwab-reauth: get_auth_context failed: %s", e)
            return f"❌ schwab re-auth failed to start: {type(e).__name__}: {e}"

        expires_at = int(time.time()) + _REAUTH_TTL_SECONDS
        payload = {
            "state": auth_context.state,
            "callback_url": callback_url,
            "expires_at": expires_at,
        }

        conv = await ctx.store.get_or_create_conversation("discord", ctx.conversation_key)
        await ctx.store.set_pending_schwab_reauth(conv.id, json.dumps(payload))

        _log.info(
            "schwab-reauth handshake started: conv=%d state=%s ttl=%ds",
            conv.id,
            auth_context.state[:8] + "...",
            _REAUTH_TTL_SECONDS,
        )

        return (
            "🔐 Schwab re-auth — open this URL, sign in, and click Allow:\n"
            f"{auth_context.authorization_url}\n"
            "\n"
            "The redirect page will look broken (no listener on 127.0.0.1:8182) — that's expected.\n"
            "Copy the full URL from your browser's address bar and paste it as your "
            "next message **within 30 seconds** of clicking Allow (Schwab codes expire fast)."
        )
