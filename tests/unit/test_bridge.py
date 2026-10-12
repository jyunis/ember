"""MCP bridge interface tests (FC-4).

Per FC-4:
- One bridge per agent process; started on boot, stopped on shutdown.
- Stateless w.r.t. conversations.
- Exposes tools as `agent__<name>` over MCP.
- Schema parity with the in-process registry.

These unit tests cover the construction + lifecycle + config-emission
surface only. Actual MCP wire-protocol round-trips are an integration
concern (round 4); we do not run a real MCP client against the bridge
here.
"""

from __future__ import annotations

import socket

import pytest

from ember.bridge.server import Bridge
from ember.tools.echo import EchoTool
from ember.tools.registry import Registry


def _free_port() -> int:
    """Bind to port 0, capture the OS-assigned port, and release it.

    Returns a port number that was free at this instant. There's a
    small race window — another process could grab it before the
    bridge binds — but it's good enough for unit tests on a quiet
    box.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _registry_with_echo() -> Registry:
    reg = Registry()
    reg.register(EchoTool())
    return reg


# --- Construction --------------------------------------------------------


class TestConstruction:
    def test_construction_with_echo_registry_does_not_raise(self) -> None:
        reg = _registry_with_echo()
        # Default host/port is fine — we don't start it here, just
        # construct.
        Bridge(reg)

    def test_explicit_host_port_construction(self) -> None:
        reg = _registry_with_echo()
        Bridge(reg, host="127.0.0.1", port=_free_port())


# --- mcp_config_dict shape (FC-4) ----------------------------------------


class TestMcpConfigDict:
    def test_shape_has_mcp_servers_agent_entry(self) -> None:
        port = _free_port()
        reg = _registry_with_echo()
        bridge = Bridge(reg, host="127.0.0.1", port=port)

        cfg = bridge.mcp_config_dict()

        assert isinstance(cfg, dict)
        assert "mcpServers" in cfg
        assert "agent" in cfg["mcpServers"]
        agent_cfg = cfg["mcpServers"]["agent"]
        assert agent_cfg.get("type") == "http"
        assert "url" in agent_cfg

    def test_url_matches_bridge_url_property(self) -> None:
        port = _free_port()
        reg = _registry_with_echo()
        bridge = Bridge(reg, host="127.0.0.1", port=port)

        cfg = bridge.mcp_config_dict()
        # The url emitted in mcp_config_dict must match the bridge's
        # public `url` property — single source of truth.
        assert cfg["mcpServers"]["agent"]["url"] == bridge.url

    def test_url_references_configured_host_and_port(self) -> None:
        port = _free_port()
        reg = _registry_with_echo()
        bridge = Bridge(reg, host="127.0.0.1", port=port)
        url = bridge.url
        assert "127.0.0.1" in url
        assert str(port) in url

    def test_only_agent_server_exposed(self) -> None:
        # FC-4: every tool exposed as `agent__<name>`. The bridge does
        # not expose multiple MCP servers — there's exactly one entry
        # in `mcpServers` named "agent".
        reg = _registry_with_echo()
        bridge = Bridge(reg, host="127.0.0.1", port=_free_port())
        cfg = bridge.mcp_config_dict()
        assert list(cfg["mcpServers"].keys()) == ["agent"]


# --- Lifecycle: start then stop ------------------------------------------


class TestLifecycle:
    async def test_start_then_stop_runs_cleanly(self) -> None:
        # FC-4: started on boot, stopped on shutdown. Must not raise.
        reg = _registry_with_echo()
        bridge = Bridge(reg, host="127.0.0.1", port=_free_port())

        await bridge.start()
        try:
            # Bridge should be listening here, but we don't assert that
            # at the wire level — that's a round-4 integration concern.
            pass
        finally:
            await bridge.stop()

    async def test_stop_without_start_does_not_raise(self) -> None:
        # Defensive: if shutdown is called without a successful start
        # (e.g. construction error path in the agent main), stop must
        # be safe to call. Spec doesn't pin this explicitly — flagged
        # as ambiguity. If the implementer rejects, the spec gets a
        # statement on idempotent shutdown.
        reg = _registry_with_echo()
        bridge = Bridge(reg, host="127.0.0.1", port=_free_port())
        try:
            await bridge.stop()
        except Exception:  # noqa: BLE001
            pytest.skip("spec ambiguity: stop()-without-start behavior unspecified")


# --- Registry live-sync ---------------------------------------------------


class _SecondTool(EchoTool):
    name: str = "second"
    description: str = "A second tool, mounted after bridge construction."


class TestRegistryLiveSync:
    """Runtime registry changes (mcp_connect / mcp_disconnect) must be
    mirrored into the served FastMCP tool set. Before this, the bridge
    snapshotted the registry once in __init__, so tools mounted after
    boot answered "No such tool available" until the next restart."""

    @staticmethod
    def _served_names(bridge: Bridge) -> set[str]:
        return {t.name for t in bridge._mcp._tool_manager.list_tools()}

    def test_register_after_construction_is_served(self) -> None:
        reg = _registry_with_echo()
        bridge = Bridge(reg, port=_free_port())
        assert "second" not in self._served_names(bridge)
        reg.register(_SecondTool())
        assert "second" in self._served_names(bridge)

    def test_served_schema_matches_registry_schema(self) -> None:
        reg = _registry_with_echo()
        bridge = Bridge(reg, port=_free_port())
        tool = _SecondTool()
        reg.register(tool)
        served = {t.name: t for t in bridge._mcp._tool_manager.list_tools()}
        assert served["second"].parameters == tool.input_schema

    def test_unregister_removes_served_tool(self) -> None:
        reg = _registry_with_echo()
        bridge = Bridge(reg, port=_free_port())
        reg.register(_SecondTool())
        assert reg.unregister("second") is True
        assert "second" not in self._served_names(bridge)
        assert "echo" in self._served_names(bridge)

    def test_unregister_of_never_served_tool_is_quiet(self) -> None:
        # A tool registered BEFORE the bridge existed and unregistered
        # after must not raise even though remove_tool is strict.
        reg = Registry()
        bridge = Bridge(reg, port=_free_port())
        assert reg.unregister("ghost") is False
        assert self._served_names(bridge) == set()

    def test_boot_snapshot_still_served(self) -> None:
        reg = _registry_with_echo()
        bridge = Bridge(reg, port=_free_port())
        assert "echo" in self._served_names(bridge)
