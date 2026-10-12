"""Cleartext vault reads are gated; Schwab writes announce themselves.

Two independent hardening changes that share a theme — a compromised
conversation should not be able to drain secrets silently, and a placed
order should leave a trace somewhere other than the thread that placed it.
"""

from __future__ import annotations

from typing import Any

import pytest

from ember.tools.base import ToolResult
from ember.tools.schwab import _summarize_write_for_alert
from ember.tools.vault import VaultGetTool


class TestVaultCleartextGate:
    @pytest.fixture
    def tool(self) -> VaultGetTool:
        return VaultGetTool(broker_url="http://broker", token="t")

    def test_redacted_read_is_not_gated(self, tool: VaultGetTool) -> None:
        """The everyday `****<last4>` read stays frictionless."""
        assert tool.needs_confirmation({"item": "schwab"}) is False
        assert tool.needs_confirmation({"item": "schwab", "show_full": False}) is False

    def test_cleartext_read_is_gated(self, tool: VaultGetTool) -> None:
        assert tool.needs_confirmation({"item": "schwab", "show_full": True}) is True

    def test_tool_declares_confirmation(self, tool: VaultGetTool) -> None:
        """`requires_confirmation` must be True for the registry to consult
        the per-call hook at all."""
        assert tool.requires_confirmation is True

    def test_summary_names_the_credential_but_never_a_value(self, tool: VaultGetTool) -> None:
        summary = tool.summarize_action(
            {"item": "schwab-api", "field": "password", "show_full": True}
        )
        assert "schwab-api" in summary
        assert "password" in summary
        assert "CLEARTEXT" in summary

    async def test_registry_gates_the_cleartext_call(
        self, tool: VaultGetTool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end through the registry: a cleartext read returns the
        confirmation marker instead of executing. The gate's preflight
        performs ONE redacted read (stubbed here) so a bad item/field
        fails before the user is asked for a code; the reveal itself
        never runs."""
        from ember.tools.registry import CONFIRMATION_REQUIRED_ERROR, Registry

        fetches: list[tuple[str, str]] = []

        async def _fake_fetch(self: VaultGetTool, item: str, field: str) -> str:
            fetches.append((item, field))
            return "supersecret"

        monkeypatch.setattr(VaultGetTool, "_fetch", _fake_fetch)
        reg = Registry()
        reg.register(tool)
        result = await reg.call_tool("vault_get", {"item": "x", "show_full": True})
        assert result.is_error
        assert result.output["error"] == CONFIRMATION_REQUIRED_ERROR
        assert fetches == [("x", "password")], "preflight does exactly one redacted read"
        assert "supersecret" not in repr(result.output)


class TestWriteAlertSummary:
    def test_placed_orders_render_one_line_each(self) -> None:
        result = ToolResult(
            output={
                "placed": 2,
                "failed": 0,
                "results": [
                    {
                        "symbol": "VTI",
                        "action": "BUY",
                        "quantity": 3,
                        "account": "Roth IRA",
                        "status": "placed",
                        "order_id": "111",
                    },
                    {
                        "symbol": "SCHD",
                        "action": "SELL",
                        "quantity": 1,
                        "account": "Roth IRA",
                        "status": "placed",
                        "order_id": "222",
                    },
                ],
            }
        )
        out = _summarize_write_for_alert(result)
        assert "VTI" in out and "SCHD" in out
        assert "BUY" in out and "SELL" in out
        assert "Roth IRA" in out
        assert "111" in out and "222" in out

    def test_failed_count_is_surfaced(self) -> None:
        result = ToolResult(
            output={
                "placed": 1,
                "failed": 2,
                "results": [
                    {
                        "symbol": "VTI",
                        "action": "BUY",
                        "quantity": 1,
                        "account": "A",
                        "status": "placed",
                        "order_id": "1",
                    },
                    {"symbol": "X", "status": "failed"},
                ],
            }
        )
        assert "2 failed" in _summarize_write_for_alert(result)

    def test_nothing_placed_produces_no_alert(self) -> None:
        """A call that placed nothing must stay silent — alert fatigue is
        how a real alert gets ignored."""
        result = ToolResult(output={"placed": 0, "failed": 1, "results": [{"status": "failed"}]})
        assert _summarize_write_for_alert(result) == ""

    def test_cancel_uses_the_spelling_the_tool_emits(self) -> None:
        """`_cancel` returns `canceled` (one l). A summary keyed on the
        two-l spelling would silently never fire."""
        result = ToolResult(
            output={"canceled": True, "order_id": "999", "account": "Individual Brokerage"}
        )
        out = _summarize_write_for_alert(result)
        assert "999" in out and "Individual Brokerage" in out

    def test_failed_cancel_is_silent(self) -> None:
        result = ToolResult(output={"canceled": False, "error": "boom"}, is_error=True)
        assert _summarize_write_for_alert(result) == ""

    def test_non_dict_output_is_tolerated(self) -> None:
        assert _summarize_write_for_alert(ToolResult(output="something")) == ""


class TestNotifierPlumbing:
    """The notifier is attached post-construction (tools register before the
    channel exists), so absence must be the safe default."""

    def _place_tool(self, tmp_path: Any) -> Any:
        from ember.config import Settings
        from ember.tools.schwab import SchwabPlaceOrdersTool

        import os

        os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
        os.environ.setdefault("DISCORD_GUILD_ID", "1")
        os.environ.setdefault("DISCORD_CONVERSATIONS_CHANNEL_ID", "2")
        os.environ.setdefault("DISCORD_SCHEDULER_CHANNEL_ID", "3")
        return SchwabPlaceOrdersTool(
            settings=Settings(),  # type: ignore[call-arg]
            data_dir=tmp_path,
        )

    def test_defaults_to_no_notifier(self, tmp_path: Any) -> None:
        assert self._place_tool(tmp_path).notifier is None

    async def test_notify_is_a_noop_without_a_notifier(self, tmp_path: Any) -> None:
        tool = self._place_tool(tmp_path)
        await tool._notify_writes(ToolResult(output={"placed": 1, "results": []}))

    async def test_notifier_receives_the_summary(self, tmp_path: Any) -> None:
        tool = self._place_tool(tmp_path)
        sent: list[str] = []

        async def _capture(text: str) -> None:
            sent.append(text)

        tool.notifier = _capture
        await tool._notify_writes(
            ToolResult(
                output={
                    "placed": 1,
                    "failed": 0,
                    "results": [
                        {
                            "symbol": "VTI",
                            "action": "BUY",
                            "quantity": 1,
                            "account": "A",
                            "status": "placed",
                            "order_id": "7",
                        }
                    ],
                }
            )
        )
        assert len(sent) == 1 and "VTI" in sent[0]

    async def test_a_failing_notifier_never_breaks_the_order(self, tmp_path: Any) -> None:
        """The order already executed by this point; a Discord outage must
        not turn it into a tool error."""
        tool = self._place_tool(tmp_path)

        async def _boom(text: str) -> None:
            raise RuntimeError("discord down")

        tool.notifier = _boom
        await tool._notify_writes(
            ToolResult(
                output={
                    "placed": 1,
                    "failed": 0,
                    "results": [
                        {
                            "symbol": "VTI",
                            "action": "BUY",
                            "quantity": 1,
                            "account": "A",
                            "status": "placed",
                            "order_id": "7",
                        }
                    ],
                }
            )
        )
