"""Discord channel adapter (FC-1) — integration tests.

Per FC-1, the channel adapter is responsible for:
- TLC-1 authorization: dropping inbound messages from non-allowlisted
  identities before invoking `on_message`.
- TLC-10 idempotency: deduping inbound deliveries by the channel's
  native message ID before invoking `on_message`.
- send(kind=...) mapping to channel-appropriate primitives.
- Reconnect / exponential-backoff for transient disconnects.

These responsibilities live in `channels/discord.py`. The unit-level
test surface for the real adapter doesn't exist yet — that's part of
the v0 follow-up work per the contracts doc's resolution table for
moving FC-1 tests out of the agent loop.

Once the adapter has a testable surface (likely via dependency-
injecting the `discord.py` Client), the tests below should be
unskipped and rewritten against the real adapter.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(
    reason=(
        "FC-1 dedup belongs in the channel adapter (FC-1 says "
        "'the adapter dedupes inbound deliveries by the channel's "
        "native message ID before invoking on_message'). The real "
        "DiscordChannel doesn't have a unit-test surface yet — pending "
        "the v0 round that exposes one (e.g. via DI of the discord.py "
        "Client). This test was relocated from "
        "tests/integration/test_agent_loop.py::TestIdempotency."
    )
)
async def test_same_message_id_twice_yields_one_provider_call() -> None:
    """TLC-10 idempotency at the channel-adapter level.

    Original test (against the agent loop, which conflated FC-1's
    adapter responsibility with the agent loop's surface):

        provider.queue_text("a")
        provider.queue_text("b")
        m = _msg("hi", mid="dup-1")
        await channel.inject(m)
        await channel.inject(m)
        assert len(provider.calls) == 1
        # And only one user message persisted.

    Rewrite for the real DiscordChannel will feed the adapter two
    raw discord.Message events with the same `Message.id` and assert
    `on_message` was invoked once.
    """


@pytest.mark.skip(
    reason=(
        "FC-1 / TLC-1 authorization belongs in the channel adapter "
        "(rejects non-allowlisted senders before invoking on_message). "
        "Pending DiscordChannel unit-test surface."
    )
)
async def test_non_allowlisted_user_is_rejected_at_channel() -> None:
    """TLC-1 authorization at the channel-adapter level.

    The adapter must drop messages from senders not in
    `discord_allowed_user_ids` and emit a single audit log line, never
    invoking `on_message`. The agent loop trusts the adapter (FC-1).
    """
