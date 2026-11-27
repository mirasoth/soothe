"""Integration tests for daemon input with image attachments."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest
import pytest_asyncio
from soothe.config import SootheConfig
from soothe_client import WebSocketClient

from soothe_daemon import SootheDaemon
from soothe_daemon.config import SootheDaemonConfig
from tests.integration.daemon_fixtures import (
    alloc_ephemeral_port,
    await_event_type,
    await_status_state,
    build_daemon_config,
    close_client_safely,
    force_isolated_home,
    integration_llm_idle_timeout,
    stop_daemon_safely,
)
from tests.integration.ws_loop_client import loop_new, subscribe_loop_stream

TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="

# Load actual image assets for real-world integration tests
_ASSETS_DIR = Path(__file__).parent.parent.parent.parent.parent.parent / "assets"


def _load_image_b64(filename: str) -> str:
    """Load image file and return base64-encoded string."""
    path = _ASSETS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Asset not found: {path}")
    return base64.b64encode(path.read_bytes()).decode("ascii")


SOOTHE_LOGO_B64: str | None = None
LOGICAL_ARCH_B64: str | None = None


def _get_soothe_logo_b64() -> str:
    """Lazy-load soothe-logo.png base64."""
    global SOOTHE_LOGO_B64
    if SOOTHE_LOGO_B64 is None:
        SOOTHE_LOGO_B64 = _load_image_b64("soothe-logo.png")
    return SOOTHE_LOGO_B64


def _get_logical_arch_b64() -> str:
    """Lazy-load logical-arch.jpg base64."""
    global LOGICAL_ARCH_B64
    if LOGICAL_ARCH_B64 is None:
        LOGICAL_ARCH_B64 = _load_image_b64("logical-arch.jpg")
    return LOGICAL_ARCH_B64


def _build_daemon_config(tmp_path: Path, port: int) -> tuple[SootheConfig, SootheDaemonConfig]:
    return build_daemon_config(tmp_path=tmp_path, websocket_port=port)


def _landed_files(tmp_path: Path) -> list[Path]:
    return list((tmp_path / "soothe-home").rglob(".soothe/cache/attachments/*"))


@pytest_asyncio.fixture
async def websocket_daemon(tmp_path: Path):
    """Start an isolated daemon for attachment-input integration tests."""
    force_isolated_home(tmp_path / "soothe-home")
    port = alloc_ephemeral_port()
    config, daemon_cfg = _build_daemon_config(tmp_path, port)
    daemon = SootheDaemon(config, daemon_config=daemon_cfg)
    await daemon.start()
    await asyncio.sleep(0.2)
    try:
        yield daemon, port
    finally:
        await stop_daemon_safely(daemon)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_websocket_input_with_image_runs_turn(
    websocket_daemon: tuple[SootheDaemon, int],
    requires_llm_api,
    tmp_path: Path,
) -> None:
    daemon, port = websocket_daemon
    _ = daemon

    client = WebSocketClient(url=f"ws://127.0.0.1:{port}")
    await client.connect()
    await client.request_connection_init()
    await client.wait_for_connection_ack()
    try:
        loop_id = await loop_new(client)
        await subscribe_loop_stream(client, loop_id)

        await client.send_input(
            loop_id,
            "ack",
            attachments=[{"mime_type": "image/png", "data": TINY_PNG_B64}],
        )
        running = await await_status_state(
            client.read_event, "running", timeout=integration_llm_idle_timeout()
        )
        assert running.get("state") == "running"
        await await_status_state(client.read_event, "idle", timeout=integration_llm_idle_timeout())

        # Attachments are landed in the turn workspace before the turn runs.
        landed_files = _landed_files(tmp_path)
        assert len(landed_files) == 1
        assert landed_files[0].read_bytes() == base64.b64decode(TINY_PNG_B64)
    finally:
        await close_client_safely(client)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_websocket_input_invalid_attachment_returns_error(
    tmp_path: Path,
) -> None:
    force_isolated_home(tmp_path / "soothe-home")
    port = alloc_ephemeral_port()
    config, daemon_cfg = _build_daemon_config(tmp_path, port)
    daemon = SootheDaemon(config, daemon_config=daemon_cfg)
    await daemon.start()
    await asyncio.sleep(0.2)
    client = WebSocketClient(url=f"ws://127.0.0.1:{port}")
    await client.connect()
    await client.request_connection_init()
    await client.wait_for_connection_ack()
    try:
        loop_id = await loop_new(client)
        await subscribe_loop_stream(client, loop_id)

        await client.send_input(
            loop_id,
            "x",
            attachments=[{"mime_type": "image/png", "data": "!!!"}],
        )
        err_ev = await await_event_type(client.read_event, "error", timeout=5.0)
        err = err_ev.get("error") or {}
        # RFC-450 §7.3: an invalid parameter value (here, malformed base64
        # attachment data) is reported as INVALID_PARAMS (-32602), not the
        # structural INVALID_REQUEST (-32600) used for malformed envelopes.
        assert err.get("code") == -32602  # INVALID_PARAMS
    finally:
        await close_client_safely(client)
        await stop_daemon_safely(daemon)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_websocket_input_with_real_image_attachment(
    websocket_daemon: tuple[SootheDaemon, int],
    requires_llm_api,
    tmp_path: Path,
) -> None:
    """Test agent task execution with a real image file (soothe-logo.png)."""
    daemon, port = websocket_daemon
    _ = daemon

    soothe_logo_b64 = _get_soothe_logo_b64()

    client = WebSocketClient(url=f"ws://127.0.0.1:{port}")
    await client.connect()
    await client.request_connection_init()
    await client.wait_for_connection_ack()
    try:
        loop_id = await loop_new(client)
        await subscribe_loop_stream(client, loop_id)

        await client.send_input(
            loop_id,
            "Describe what you see in this image",
            attachments=[{"mime_type": "image/png", "data": soothe_logo_b64}],
        )
        running = await await_status_state(
            client.read_event, "running", timeout=integration_llm_idle_timeout()
        )
        assert running.get("state") == "running"
        await await_status_state(client.read_event, "idle", timeout=integration_llm_idle_timeout())

        landed_files = _landed_files(tmp_path)
        assert len(landed_files) == 1
        assert landed_files[0].read_bytes() == base64.b64decode(soothe_logo_b64)
    finally:
        await close_client_safely(client)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_websocket_input_with_multi_image_attachments(
    websocket_daemon: tuple[SootheDaemon, int],
    requires_llm_api,
    tmp_path: Path,
) -> None:
    """Test agent task execution with multiple image files (soothe-logo.png + logical-arch.jpg)."""
    daemon, port = websocket_daemon
    _ = daemon

    soothe_logo_b64 = _get_soothe_logo_b64()
    logical_arch_b64 = _get_logical_arch_b64()

    client = WebSocketClient(url=f"ws://127.0.0.1:{port}")
    await client.connect()
    await client.request_connection_init()
    await client.wait_for_connection_ack()
    try:
        loop_id = await loop_new(client)
        await subscribe_loop_stream(client, loop_id)

        await client.send_input(
            loop_id,
            "Compare these two images and describe their contents",
            attachments=[
                {"mime_type": "image/png", "data": soothe_logo_b64},
                {"mime_type": "image/jpeg", "data": logical_arch_b64},
            ],
        )
        running = await await_status_state(
            client.read_event, "running", timeout=integration_llm_idle_timeout()
        )
        assert running.get("state") == "running"
        await await_status_state(client.read_event, "idle", timeout=integration_llm_idle_timeout())

        # Two distinct payloads land as two content-addressed files.
        landed_files = _landed_files(tmp_path)
        assert len(landed_files) == 2
        landed_bytes = {f.read_bytes() for f in landed_files}
        assert landed_bytes == {
            base64.b64decode(soothe_logo_b64),
            base64.b64decode(logical_arch_b64),
        }
    finally:
        await close_client_safely(client)
