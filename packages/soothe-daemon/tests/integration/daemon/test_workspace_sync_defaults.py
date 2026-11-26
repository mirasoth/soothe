"""Integration tests for daemon workspace-sync de-wiring (IG-CORE-010).

Workspace sync is an agent-plane capability (flowjet via soothe
``TurnScopedWorkspaces``); the daemon does NOT wire it. These tests start a
real ``SootheDaemon`` and exercise the full ``loop_new`` RPC path through a
WebSocket client, verifying that sync requests fail loud and that the sync
subsystem is never constructed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from soothe_client import WebSocketClient

from soothe_daemon import SootheDaemon
from tests.integration.daemon_fixtures import (
    alloc_ephemeral_port,
    build_daemon_config,
    close_client_safely,
    force_isolated_home,
    stop_daemon_safely,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _connect_and_handshake(ws_port: int) -> WebSocketClient:
    """Connect a client and complete the protocol-1 handshake."""
    client = WebSocketClient(url=f"ws://127.0.0.1:{ws_port}")
    await client.connect()
    await client.request_connection_init()
    await client.wait_for_connection_ack()
    return client


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
class TestWorkspaceSyncDeWiring:
    """Verify the daemon rejects workspace-sync requests over the real RPC path."""

    async def test_config_has_no_workspace_sync_block(self, tmp_path: Path) -> None:
        """The daemon config surface has no ``workspace_sync`` key at all."""
        from soothe.config import SootheConfig

        config = SootheConfig()
        assert not hasattr(config, "workspace_sync"), (
            "WorkspaceSyncConfig was removed — the daemon must not carry sync config"
        )

    async def test_daemon_has_no_workspace_sync_manager(self, tmp_path: Path) -> None:
        """A running daemon never constructs the (removed) workspace sync manager."""
        force_isolated_home(tmp_path / "soothe-home")
        ws_port = alloc_ephemeral_port()
        config, daemon_cfg = build_daemon_config(tmp_path, websocket_port=ws_port)

        daemon = SootheDaemon(config, daemon_config=daemon_cfg, handle_sigint_shutdown=False)
        try:
            await daemon.start()
            await asyncio.sleep(0.3)  # Allow transport to initialize

            # The sync manager attribute was removed from the daemon core;
            # nothing may reintroduce it during startup.
            assert not hasattr(daemon, "_workspace_manager"), (
                "Daemon must not construct a workspace sync manager"
            )
            assert not hasattr(daemon, "_get_workspace_manager")
        finally:
            await stop_daemon_safely(daemon)

    async def test_loop_new_with_sync_source_fails_loud(self, tmp_path: Path) -> None:
        """A ``workspace_sync_source`` in ``loop_new`` gets a fail-loud error response."""
        force_isolated_home(tmp_path / "soothe-home")
        ws_port = alloc_ephemeral_port()
        config, daemon_cfg = build_daemon_config(tmp_path, websocket_port=ws_port)

        daemon = SootheDaemon(config, daemon_config=daemon_cfg, handle_sigint_shutdown=False)
        await daemon.start()
        await asyncio.sleep(0.3)

        client: WebSocketClient | None = None
        try:
            client = await _connect_and_handshake(ws_port)
            resp = await client.request(
                "loop_new",
                {"workspace_sync_source": "s3://some-bucket/prefix/"},
                timeout=10.0,
            )
            error = resp.get("error") or {}
            assert error.get("code") == "WORKSPACE_RESOLUTION_FAILED", (
                f"sync requests must fail loud with WORKSPACE_RESOLUTION_FAILED: {resp}"
            )
            assert "not supported" in str(error.get("message", ""))
        finally:
            if client is not None:
                await close_client_safely(client)
            await stop_daemon_safely(daemon)
