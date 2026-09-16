"""Tests for client workspace propagation through ``loop_new``.

The CLI passes the user's CWD on bootstrap so the agent's filesystem tools default
to the user's project directory. ``_handle_loop_new`` must validate the value and
persist it in the database; ``bind_execution_thread_for_loop`` must prefer
``current_workspace`` (or mapped ``client_workspace``) over the persisted ``ws_*`` layout.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from soothe.sloop.checkpoints.manager import (
    StrangeLoopCheckpointPersistenceManager,
)

from soothe_daemon.protocol import ErrorCode, MessageRouter
from soothe_daemon.runtime.loop_dispatcher import bind_execution_thread_for_loop


async def _read_metadata(loop_id: str, config: Any) -> dict[str, Any]:
    """Read loop metadata from the database (replaces old metadata.json read)."""
    pm = StrangeLoopCheckpointPersistenceManager(config=config)
    try:
        return await pm.get_loop_metadata(loop_id) or {}
    finally:
        await pm.close()


class _CapturingDaemon:
    """Minimal daemon double recording the response sent for ``loop_new``."""

    def __init__(self, config: Any = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self._config = config
        self._persistence_manager: StrangeLoopCheckpointPersistenceManager | None = None

    def _get_pm(self) -> StrangeLoopCheckpointPersistenceManager:
        if self._persistence_manager is None:
            self._persistence_manager = StrangeLoopCheckpointPersistenceManager(config=self._config)
        return self._persistence_manager

    @property
    def _pm(self) -> StrangeLoopCheckpointPersistenceManager:
        return self._get_pm()

    async def _send_client_message(self, client_id: Any, msg: dict[str, Any]) -> None:
        self.sent.append(msg)

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        """Stub for autopilot mode broadcast (tests inspect sent, not broadcast)."""
        self.sent.append(msg)

    async def close(self) -> None:
        if self._persistence_manager is not None:
            await self._persistence_manager.close()


def _make_daemon_with_pm(config: Any) -> _CapturingDaemon:
    daemon = _CapturingDaemon(config=config)
    # Pre-init the persistence manager so _handle_loop_new can use daemon._persistence_manager
    daemon._persistence_manager = StrangeLoopCheckpointPersistenceManager(config=config)
    return daemon


@pytest.mark.asyncio
async def test_loop_new_persists_client_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid client workspace on ``loop_new`` is written into the database."""
    monkeypatch.setattr("soothe.config.SOOTHE_HOME", str(tmp_path / "soothe-home"))

    project = tmp_path / "myproject"
    project.mkdir()

    from soothe.config import SootheConfig

    config = SootheConfig()
    daemon = _make_daemon_with_pm(config)
    router = MessageRouter(daemon)

    try:
        await router._handle_loop_new(
            client_id="client-1",
            msg={"type": "loop_new", "workspace": str(project), "request_id": "rid-1"},
        )

        assert daemon.sent, "Daemon must respond to loop_new"
        response = daemon.sent[-1]
        assert response["type"] == "response"
        loop_id = response["result"]["loop_id"]
        assert loop_id

        metadata = await _read_metadata(loop_id, config)
        assert metadata.get("client_workspace") == str(project.resolve())
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_loop_new_persists_is_ephemeral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``is_ephemeral`` on loop_new is stored in loop metadata."""
    monkeypatch.setattr("soothe.config.SOOTHE_HOME", str(tmp_path / "soothe-home"))

    from soothe.config import SootheConfig

    config = SootheConfig()
    daemon = _make_daemon_with_pm(config)
    router = MessageRouter(daemon)

    try:
        await router._handle_loop_new(
            client_id="client-1",
            msg={"type": "loop_new", "is_ephemeral": True, "request_id": "rid-eph"},
        )

        response = daemon.sent[-1]
        assert response["result"].get("is_ephemeral") is True
        loop_id = response["result"]["loop_id"]
        metadata = await _read_metadata(loop_id, config)
        assert metadata.get("is_ephemeral") is True
        assert metadata.get("current_workspace")
        # last_message_at is populated on first counter increment, not at creation,
        # so empty-loop GC can detect bootstrap-only loops via COALESCE(last_message_at, created_at).
        assert metadata.get("last_message_at") is None
        assert metadata.get("human_message_count", 0) == 0
        assert metadata.get("ai_message_count", 0) == 0
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_loop_new_omits_client_workspace_when_not_on_daemon_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host paths that do not exist inside the daemon are ignored (remote Docker CLI)."""
    monkeypatch.setattr("soothe.config.SOOTHE_HOME", str(tmp_path / "soothe-home"))

    missing = tmp_path / "missing-on-daemon"
    assert not missing.exists()

    from soothe.config import SootheConfig

    config = SootheConfig()
    daemon = _make_daemon_with_pm(config)
    router = MessageRouter(daemon)

    try:
        await router._handle_loop_new(
            client_id="client-1",
            msg={"type": "loop_new", "workspace": str(missing), "request_id": "rid-missing"},
        )

        response = daemon.sent[-1]
        loop_id = response["result"]["loop_id"]
        metadata = await _read_metadata(loop_id, config)
        assert metadata.get("client_workspace") is None
        assert metadata.get("current_workspace")
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_loop_new_maps_client_workspace_under_host_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RFC-621: host paths under ``workspace_mount.host_root`` map without existing literally."""
    monkeypatch.setattr("soothe.config.SOOTHE_HOME", str(tmp_path / "soothe-home"))

    host_root = tmp_path / "host"
    container_root = tmp_path / "container"
    host_project = host_root / "myapp"
    container_project = container_root / "myapp"
    assert not host_project.exists()
    container_project.mkdir(parents=True)

    from soothe.config import SootheConfig
    from soothe.config.models import WorkspaceMountConfig

    config = SootheConfig(
        workspace_mount=WorkspaceMountConfig(
            host_root=str(host_root),
            container_root=str(container_root),
        ),
    )
    daemon = _make_daemon_with_pm(config)
    daemon._config = config
    router = MessageRouter(daemon)

    try:
        await router._handle_loop_new(
            client_id="client-1",
            msg={"type": "loop_new", "workspace": str(host_project), "request_id": "rid-map"},
        )

        response = daemon.sent[-1]
        loop_id = response["result"]["loop_id"]
        metadata = await _read_metadata(loop_id, config)
        assert metadata.get("client_workspace") == str(host_project.resolve())
        assert metadata.get("current_workspace") == str(container_project.resolve())
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_loop_new_omits_client_workspace_when_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unsafe workspace values (system dirs) are rejected without aborting loop creation."""
    monkeypatch.setattr("soothe.config.SOOTHE_HOME", str(tmp_path / "soothe-home"))

    from soothe.config import SootheConfig

    config = SootheConfig()
    daemon = _make_daemon_with_pm(config)
    router = MessageRouter(daemon)

    try:
        await router._handle_loop_new(
            client_id="client-1",
            msg={"type": "loop_new", "workspace": "/", "request_id": "rid-2"},
        )

        response = daemon.sent[-1]
        loop_id = response["result"]["loop_id"]
        metadata = await _read_metadata(loop_id, config)
        assert metadata.get("client_workspace") is None
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_loop_new_omits_client_workspace_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a ``workspace`` field the loop falls back to per-loop daemon dir."""
    monkeypatch.setattr("soothe.config.SOOTHE_HOME", str(tmp_path / "soothe-home"))

    from soothe.config import SootheConfig

    config = SootheConfig()
    daemon = _make_daemon_with_pm(config)
    router = MessageRouter(daemon)

    try:
        await router._handle_loop_new(
            client_id="client-1",
            msg={"type": "loop_new", "request_id": "rid-3"},
        )

        response = daemon.sent[-1]
        loop_id = response["result"]["loop_id"]
        metadata = await _read_metadata(loop_id, config)
        assert metadata.get("client_workspace") is None
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_bind_execution_thread_prefers_client_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bind_execution_thread_for_loop`` registers the client_workspace, not the per-loop dir."""
    monkeypatch.setattr("soothe.config.SOOTHE_HOME", str(tmp_path / "soothe-home"))

    project = tmp_path / "myproject"
    project.mkdir()

    from soothe.config import SootheConfig

    config = SootheConfig()
    daemon = _make_daemon_with_pm(config)
    router = MessageRouter(daemon)

    try:
        # Create loop via the same handler so metadata layout matches production.
        await router._handle_loop_new(
            client_id="client-1",
            msg={"type": "loop_new", "workspace": str(project), "request_id": "rid-1"},
        )
        loop_id = daemon.sent[-1]["result"]["loop_id"]

        # Stub the runtime daemon surfaces touched during bind.
        set_workspace_calls: list[Path] = []

        class _ThreadRegistry:
            def ensure(self, _thread_id: str, *, is_draft: bool) -> None:
                _ = is_draft

            def set_workspace(self, _thread_id: str, workspace: Path) -> None:
                set_workspace_calls.append(Path(workspace))

            def set_thread_loop(self, _thread_id: str, _loop_id: str) -> None:
                pass

        class _Runner:
            def set_current_thread_id(self, _thread_id: str) -> None:
                pass

        bind_daemon = SimpleNamespace(
            _persistence_manager=daemon._persistence_manager,
            _thread_registry=_ThreadRegistry(),
            _runner=_Runner(),
            _daemon_workspace=str(tmp_path / "fallback"),
        )

        checkpoint_thread_id = await bind_execution_thread_for_loop(bind_daemon, loop_id)

        # RFC-223: main checkpoint thread id equals loop_id.
        assert checkpoint_thread_id == loop_id
        assert set_workspace_calls, "set_workspace must be invoked"
        assert set_workspace_calls[0] == project.resolve()
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_bind_execution_thread_uses_loop_id_as_checkpoint_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RFC-223: the main StrangeLoop checkpoint thread id equals the loop id."""
    monkeypatch.setattr("soothe.config.SOOTHE_HOME", str(tmp_path / "soothe-home"))

    from soothe.config import SootheConfig

    config = SootheConfig()
    daemon = _make_daemon_with_pm(config)
    router = MessageRouter(daemon)

    try:
        await router._handle_loop_new(
            client_id="client-1",
            msg={"type": "loop_new", "request_id": "rid-1"},
        )
        loop_id = daemon.sent[-1]["result"]["loop_id"]

        class _ThreadRegistry:
            def ensure(self, _thread_id: str, *, is_draft: bool) -> None:
                _ = is_draft

            def set_workspace(self, _thread_id: str, _workspace: Path) -> None:
                pass

            def set_thread_loop(self, thread_id: str, bound_loop_id: str) -> None:
                assert bound_loop_id == loop_id
                # RFC-223: registry key for the main thread IS loop_id.
                assert thread_id == loop_id

        bind_daemon = SimpleNamespace(
            _persistence_manager=daemon._persistence_manager,
            _thread_registry=_ThreadRegistry(),
            _daemon_workspace=str(tmp_path / "fallback"),
        )

        checkpoint_thread_id = await bind_execution_thread_for_loop(bind_daemon, loop_id)
        assert checkpoint_thread_id == loop_id

        metadata = await daemon._persistence_manager.get_loop_metadata(loop_id)
        assert metadata is not None
        assert metadata.get("current_thread_id") == loop_id
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_bind_execution_thread_preserves_existing_loop_id_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RFC-223: a stored ``current_thread_id == loop_id`` is the bound state, not a stale alias.

    Earlier code treated this as a legacy alias and minted a fresh thread,
    which silently abandoned the loop's checkpoint and conversation history
    on every continue. Bind must now leave the binding intact.
    """
    monkeypatch.setattr("soothe.config.SOOTHE_HOME", str(tmp_path / "soothe-home"))

    from soothe.config import SootheConfig

    config = SootheConfig()
    daemon = _make_daemon_with_pm(config)
    router = MessageRouter(daemon)

    try:
        await router._handle_loop_new(
            client_id="client-1",
            msg={"type": "loop_new", "request_id": "rid-1"},
        )
        loop_id = daemon.sent[-1]["result"]["loop_id"]
        await daemon._persistence_manager.update_loop_metadata(
            loop_id,
            current_thread_id=loop_id,
            status="running",
        )

        bind_daemon = SimpleNamespace(
            _persistence_manager=daemon._persistence_manager,
            _thread_registry=SimpleNamespace(
                ensure=lambda *_a, **_k: None,
                set_workspace=lambda *_a, **_k: None,
                set_thread_loop=lambda *_a, **_k: None,
            ),
            _daemon_workspace=str(tmp_path / "fallback"),
        )

        checkpoint_thread_id = await bind_execution_thread_for_loop(bind_daemon, loop_id)
        assert checkpoint_thread_id == loop_id

        metadata = await daemon._persistence_manager.get_loop_metadata(loop_id)
        assert metadata is not None
        assert metadata.get("current_thread_id") == loop_id
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_bind_execution_thread_falls_back_when_client_workspace_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When metadata has no client_workspace use anonymous/ws_<hash(loop_id)>."""
    soothe_home = tmp_path / "soothe-home"
    monkeypatch.setattr("soothe.config.SOOTHE_HOME", str(soothe_home))

    from soothe.config import SootheConfig
    from soothe.workspace.loop_workspace import (
        compute_scoped_workspace_dir_name,
        normalize_user_id,
    )

    config = SootheConfig()
    daemon = _make_daemon_with_pm(config)
    router = MessageRouter(daemon)

    try:
        await router._handle_loop_new(
            client_id="client-1",
            msg={"type": "loop_new", "request_id": "rid-1"},
        )
        loop_id = daemon.sent[-1]["result"]["loop_id"]

        set_workspace_calls: list[Path] = []

        class _ThreadRegistry:
            def ensure(self, _thread_id: str, *, is_draft: bool) -> None:
                _ = is_draft

            def set_workspace(self, _thread_id: str, workspace: Path) -> None:
                set_workspace_calls.append(Path(workspace))

            def set_thread_loop(self, _thread_id: str, _loop_id: str) -> None:
                pass

        class _Runner:
            def set_current_thread_id(self, _thread_id: str) -> None:
                pass

        bind_daemon = SimpleNamespace(
            _persistence_manager=daemon._persistence_manager,
            _thread_registry=_ThreadRegistry(),
            _runner=_Runner(),
            _daemon_workspace=str(tmp_path / "fallback"),
        )

        await bind_execution_thread_for_loop(bind_daemon, loop_id)

        assert set_workspace_calls, "set_workspace must be invoked"
        ws_name = compute_scoped_workspace_dir_name(None, loop_id)
        expected_loop_ws = (
            soothe_home.resolve() / "data" / "workspaces" / normalize_user_id(None) / ws_name
        )
        assert set_workspace_calls[0] == expected_loop_ws
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_checkpoint_thread_id_overrides_stale_runner_for_workspace_resolution() -> None:
    """``bind`` sets registry workspace on the loop checkpoint; ``run_query`` must use that id.

    RFC-221 stopped mutating ``runner.current_thread_id`` inside ``bind_execution_thread_for_loop``.
    Without passing the bound checkpoint into ``run_query``, workspace resolution could read
    a stale singleton id and pass the wrong directory into ``LoopRunRequest``.
    """
    from soothe_daemon.query import QueryEngine

    class _Runner:
        def __init__(self) -> None:
            self.current_thread_id = "stale-global-thread"

        def set_current_thread_id(self, thread_id: str | None) -> None:
            self.current_thread_id = thread_id

    qe = QueryEngine.__new__(QueryEngine)
    qe._daemon = SimpleNamespace(_runner=_Runner())

    resolved = await QueryEngine._resolve_query_checkpoint_thread_id(
        qe,
        checkpoint_thread_id="bound-loop-thread",
        client_id=None,
    )
    assert resolved == "bound-loop-thread"
    assert qe._daemon._runner.current_thread_id == "bound-loop-thread"


# ---------------------------------------------------------------------------
# Workspace sync (RFC-906 §51) — URI detection and sync workspace opening
# ---------------------------------------------------------------------------


class _SyncDaemon(_CapturingDaemon):
    """Daemon double that supports workspace sync manager mocking."""

    def __init__(self, config: Any = None) -> None:
        super().__init__(config=config)
        self._workspace_manager: Any = None

    def _get_workspace_manager(self) -> Any:
        return self._workspace_manager


def _patch_sync_backend(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Patch ``construct_sync_backend`` to return a fake workspace with ``root``."""

    class _FakeBackend:
        pass

    class _FakeWorkspace:
        def __init__(self, root_path: Path) -> None:
            self.root = root_path

    class _FakeManager:
        async def open_from_uri(self, *, run_id: str, backend: Any) -> _FakeWorkspace:
            return _FakeWorkspace(root)

    def _fake_construct(_uri: str, _config: Any = None) -> _FakeBackend:
        return _FakeBackend()

    monkeypatch.setattr("soothe.workspace.sync.construct_sync_backend", _fake_construct)


@pytest.mark.asyncio
async def test_loop_new_rejects_non_allowlisted_uri_scheme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S8: ``file://`` URI in ``client_workspace`` is rejected (SSRF prevention)."""
    monkeypatch.setattr("soothe.config.SOOTHE_HOME", str(tmp_path / "soothe-home"))

    from soothe.config import SootheConfig

    config = SootheConfig()
    daemon = _make_daemon_with_pm(config)
    router = MessageRouter(daemon)

    try:
        await router._handle_loop_new(
            client_id="client-1",
            msg={
                "type": "loop_new",
                "client_workspace": "file:///etc/passwd",
                "request_id": "rid-1",
            },
        )
        assert daemon.sent, "Daemon must respond with an error"
        response = daemon.sent[-1]
        assert response["type"] == "error"
        assert response["error"]["code"] == ErrorCode.WORKSPACE_RESOLUTION_FAILED
        assert "file" in response["error"]["message"]
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_loop_new_s3_uri_triggers_sync_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``s3://`` URI in ``client_workspace`` opens a sync workspace."""
    monkeypatch.setattr("soothe.config.SOOTHE_HOME", str(tmp_path / "soothe-home"))

    sync_root = tmp_path / "sync-ws"
    sync_root.mkdir()

    _patch_sync_backend(monkeypatch, sync_root)

    from soothe.config import SootheConfig

    config = SootheConfig()
    daemon = _SyncDaemon(config=config)
    daemon._persistence_manager = StrangeLoopCheckpointPersistenceManager(config=config)

    class _FakeManager:
        async def open_from_uri(self, *, run_id: str, backend: Any) -> Any:
            return SimpleNamespace(root=sync_root)

    daemon._workspace_manager = _FakeManager()
    router = MessageRouter(daemon)

    try:
        await router._handle_loop_new(
            client_id="client-1",
            msg={
                "type": "loop_new",
                "client_workspace": "s3://test-bucket/project/",
                "request_id": "rid-1",
            },
        )
        assert daemon.sent, "Daemon must respond"
        response = daemon.sent[-1]
        assert response["type"] == "response"
        loop_id = response["result"]["loop_id"]

        metadata = await _read_metadata(loop_id, config)
        assert metadata.get("workspace_sync_source") == "s3://test-bucket/project/"
        assert metadata.get("current_workspace") == str(sync_root)
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_loop_new_config_default_enables_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config ``workspace_sync`` with enabled+source_uri activates sync."""
    monkeypatch.setattr("soothe.config.SOOTHE_HOME", str(tmp_path / "soothe-home"))

    sync_root = tmp_path / "sync-ws"
    sync_root.mkdir()

    _patch_sync_backend(monkeypatch, sync_root)

    from soothe.config import SootheConfig

    config = SootheConfig()
    config.workspace_sync.enabled = True
    config.workspace_sync.source_uri = "s3://soothe/"
    assert config.workspace_sync.is_enabled

    class _FakeManager:
        async def open_from_uri(self, *, run_id: str, backend: Any) -> Any:
            return SimpleNamespace(root=sync_root)

    daemon = _SyncDaemon(config=config)
    daemon._persistence_manager = StrangeLoopCheckpointPersistenceManager(config=config)
    daemon._workspace_manager = _FakeManager()
    router = MessageRouter(daemon)

    try:
        await router._handle_loop_new(
            client_id="client-1",
            msg={"type": "loop_new", "request_id": "rid-1"},
        )
        assert daemon.sent, "Daemon must respond"
        response = daemon.sent[-1]
        assert response["type"] == "response"
        loop_id = response["result"]["loop_id"]

        metadata = await _read_metadata(loop_id, config)
        assert metadata.get("workspace_sync_source") == "s3://soothe/"
        assert metadata.get("current_workspace") == str(sync_root)
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_loop_new_sync_source_field_overrides_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit ``workspace_sync_source`` field takes priority over config default."""
    monkeypatch.setattr("soothe.config.SOOTHE_HOME", str(tmp_path / "soothe-home"))

    sync_root = tmp_path / "sync-ws"
    sync_root.mkdir()

    _patch_sync_backend(monkeypatch, sync_root)

    from soothe.config import SootheConfig

    config = SootheConfig()
    config.workspace_sync.enabled = True
    config.workspace_sync.source_uri = "s3://config-default/"

    class _FakeManager:
        async def open_from_uri(self, *, run_id: str, backend: Any) -> Any:
            return SimpleNamespace(root=sync_root)

    daemon = _SyncDaemon(config=config)
    daemon._persistence_manager = StrangeLoopCheckpointPersistenceManager(config=config)
    daemon._workspace_manager = _FakeManager()
    router = MessageRouter(daemon)

    try:
        await router._handle_loop_new(
            client_id="client-1",
            msg={
                "type": "loop_new",
                "workspace_sync_source": "s3://explicit/override/",
                "request_id": "rid-1",
            },
        )
        response = daemon.sent[-1]
        assert response["type"] == "response"
        loop_id = response["result"]["loop_id"]

        metadata = await _read_metadata(loop_id, config)
        assert metadata.get("workspace_sync_source") == "s3://explicit/override/"
    finally:
        await daemon.close()
