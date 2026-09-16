"""Integration tests for workspace sync disabled-by-default behavior.

Verifies that ``WorkspaceSyncConfig`` defaults to disabled in a real daemon
process, and that a ``loop_new`` request does **not** trigger the sync path
when no explicit ``workspace_sync_source`` is provided and the config has not
opted in via ``enabled: True`` + ``source_uri``.

These tests start a real ``SootheDaemon`` and exercise the full ``loop_new``
RPC path through a WebSocket client, checking that loop metadata records no
``workspace_sync_source`` and that the workspace sync manager is never
constructed.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

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


async def _create_loop(client: WebSocketClient) -> str:
    """Send ``loop_new`` and return the ``loop_id`` from the response."""
    resp = await client.request("loop_new", {}, timeout=10.0)
    result = resp.get("result") or resp
    loop_id = str(result.get("loop_id") or "").strip()
    assert loop_id, f"loop_new response missing loop_id: {resp}"
    return loop_id


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
class TestWorkspaceSyncDefaults:
    """Verify workspace sync is disabled by default in a live daemon."""

    async def test_default_config_sync_is_disabled(self, tmp_path: Path) -> None:
        """A freshly constructed ``SootheConfig`` has ``workspace_sync.is_enabled == False``.

        This is the code-level guarantee that the prior goal's model change
        (``enabled: bool = Field(default=False, ...)``) is wired correctly.
        """
        from soothe.config import SootheConfig

        config = SootheConfig()

        # The master switch defaults to False.
        assert config.workspace_sync.enabled is False
        # source_uri defaults to None.
        assert config.workspace_sync.source_uri is None
        # is_enabled requires both enabled=True and source_uri set.
        assert config.workspace_sync.is_enabled is False

    async def test_source_uri_alone_does_not_enable_sync(self, tmp_path: Path) -> None:
        """Setting ``source_uri`` without ``enabled=True`` must NOT activate sync.

        This is the core regression guard: before the prior goal's change,
        ``is_enabled`` checked only ``source_uri``, so any config (or YAML
        default) that populated ``source_uri`` would silently enable sync.
        """
        from soothe.config import SootheConfig

        config = SootheConfig()
        config.workspace_sync.source_uri = "s3://some-bucket/prefix/"

        # source_uri is set, but enabled is still False → must stay disabled.
        assert config.workspace_sync.source_uri is not None
        assert config.workspace_sync.enabled is False
        assert config.workspace_sync.is_enabled is False

    async def test_enabled_true_without_source_uri_does_not_enable_sync(
        self, tmp_path: Path
    ) -> None:
        """Setting ``enabled=True`` without ``source_uri`` must NOT activate sync.

        Both gates must be satisfied — ``enabled`` alone is insufficient.
        """
        from soothe.config import SootheConfig

        config = SootheConfig()
        config.workspace_sync.enabled = True

        assert config.workspace_sync.enabled is True
        assert config.workspace_sync.source_uri is None
        assert config.workspace_sync.is_enabled is False

    async def test_both_gates_required_to_enable_sync(self, tmp_path: Path) -> None:
        """Both ``enabled=True`` and ``source_uri`` set are required to activate sync."""
        from soothe.config import SootheConfig

        config = SootheConfig()
        config.workspace_sync.enabled = True
        config.workspace_sync.source_uri = "s3://soothe/"

        assert config.workspace_sync.is_enabled is True

    async def test_loop_new_no_sync_source_in_metadata_by_default(self, tmp_path: Path) -> None:
        """A real daemon with default config does not set ``workspace_sync_source`` in loop metadata.

        Starts a ``SootheDaemon``, creates a loop via the WebSocket ``loop_new``
        RPC, and verifies that the persisted loop metadata contains no
        ``workspace_sync_source`` field — proving the sync path was never
        entered.
        """
        force_isolated_home(tmp_path / "soothe-home")
        ws_port = alloc_ephemeral_port()
        config, daemon_cfg = build_daemon_config(tmp_path, websocket_port=ws_port)

        # Sanity: the integration base config must have sync disabled.
        assert not config.workspace_sync.is_enabled, (
            "Integration base config must not enable workspace_sync by default"
        )

        daemon = SootheDaemon(config, daemon_config=daemon_cfg, handle_sigint_shutdown=False)
        await daemon.start()
        await asyncio.sleep(0.3)  # Allow transport to initialize

        client: WebSocketClient | None = None
        try:
            client = await _connect_and_handshake(ws_port)
            loop_id = await _create_loop(client)

            # Read loop metadata from the daemon's persistence layer.
            metadata = await daemon._persistence_manager.get_loop_metadata(loop_id)
            assert metadata is not None, "Loop metadata must exist after loop_new"

            # The sync source must be absent — sync was never activated.
            assert metadata.get("workspace_sync_source") is None, (
                "Default config must not populate workspace_sync_source in loop metadata"
            )

            # The workspace sync manager must never have been constructed.
            assert daemon._workspace_manager is None, (
                "Daemon must not construct workspace sync manager when sync is disabled"
            )
        finally:
            if client is not None:
                await close_client_safely(client)
            await stop_daemon_safely(daemon)

    async def test_loop_new_with_source_uri_in_config_but_disabled_stays_local(
        self, tmp_path: Path
    ) -> None:
        """Even if ``source_uri`` is set in config, sync stays off when ``enabled=False``.

        This is the critical integration-level regression test: a config that
        has a ``source_uri`` populated (e.g. from a YAML default that was not
        fully cleared) must NOT trigger the sync path because ``enabled``
        defaults to ``False``.
        """
        force_isolated_home(tmp_path / "soothe-home")
        ws_port = alloc_ephemeral_port()
        config, daemon_cfg = build_daemon_config(tmp_path, websocket_port=ws_port)

        # Simulate a config that has source_uri set but enabled=False (the
        # disable-by-default state after the prior goal's changes).
        config.workspace_sync.source_uri = "s3://soothe/"
        assert config.workspace_sync.enabled is False
        assert not config.workspace_sync.is_enabled

        daemon = SootheDaemon(config, daemon_config=daemon_cfg, handle_sigint_shutdown=False)
        await daemon.start()
        await asyncio.sleep(0.3)

        client: WebSocketClient | None = None
        try:
            client = await _connect_and_handshake(ws_port)
            loop_id = await _create_loop(client)

            metadata = await daemon._persistence_manager.get_loop_metadata(loop_id)
            assert metadata is not None

            # Sync source must NOT leak into metadata when enabled=False.
            assert metadata.get("workspace_sync_source") is None, (
                "source_uri in config must not activate sync when enabled=False"
            )

            assert daemon._workspace_manager is None, (
                "Daemon must not construct workspace sync manager when enabled=False"
            )
        finally:
            if client is not None:
                await close_client_safely(client)
            await stop_daemon_safely(daemon)

    async def test_loop_new_with_enabled_and_source_uri_activates_sync(
        self, tmp_path: Path
    ) -> None:
        """When both ``enabled=True`` and ``source_uri`` are set, sync activates.

        This is the positive control: the two-gate activation logic correctly
        enables sync when both conditions are met. We patch the sync backend
        to avoid requiring a real S3 endpoint.
        """
        sync_root = tmp_path / "sync-ws"
        sync_root.mkdir()

        # Patch construct_sync_backend to avoid real S3 access.
        from soothe.workspace import sync as sync_module

        class _FakeBackend:
            pass

        class _FakeWorkspace:
            def __init__(self, root_path: Path) -> None:
                self.root = root_path

        class _FakeManager:
            async def open_from_uri(self, *, run_id: str, backend: Any) -> _FakeWorkspace:
                return _FakeWorkspace(sync_root)

        force_isolated_home(tmp_path / "soothe-home")
        ws_port = alloc_ephemeral_port()
        config, daemon_cfg = build_daemon_config(tmp_path, websocket_port=ws_port)

        # Enable both gates.
        config.workspace_sync.enabled = True
        config.workspace_sync.source_uri = "s3://soothe/"
        assert config.workspace_sync.is_enabled

        daemon = SootheDaemon(config, daemon_config=daemon_cfg, handle_sigint_shutdown=False)

        # Patch the sync backend construction on the module before the daemon
        # lazily constructs the workspace manager.
        original_construct = sync_module.construct_sync_backend

        def _fake_construct(_uri: str, _config: Any = None) -> _FakeBackend:
            return _FakeBackend()

        sync_module.construct_sync_backend = _fake_construct  # type: ignore[assignment]

        # Pre-inject a fake workspace manager so _get_workspace_manager returns it
        # without needing the real WorkspaceManager (which requires fsspec backends).
        daemon._workspace_manager = _FakeManager()

        try:
            await daemon.start()
            await asyncio.sleep(0.3)

            client = await _connect_and_handshake(ws_port)
            loop_id = await _create_loop(client)

            metadata = await daemon._persistence_manager.get_loop_metadata(loop_id)
            assert metadata is not None

            # Sync source must be populated when both gates are enabled.
            assert metadata.get("workspace_sync_source") == "s3://soothe/", (
                "When enabled=True and source_uri is set, sync source must appear in metadata"
            )

            await close_client_safely(client)
        finally:
            sync_module.construct_sync_backend = original_construct  # type: ignore[assignment]
            with contextlib.suppress(Exception):
                await daemon.stop()
