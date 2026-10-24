"""Tests for the WorkspaceManager lifecycle orchestrator.

Tests verify:
    - open() creates workspace directories and components.
    - get() returns open workspaces.
    - close() cleans up.
    - open_from_uri() uses the provided backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fsspec.implementations.memory import MemoryFileSystem

from soothe.workspace.sync.backends.fsspec import FsspecSyncBackend
from soothe.workspace.sync.manager import WorkspaceManager


@pytest.fixture
def manager(tmp_path: Path) -> WorkspaceManager:
    """Fresh workspace manager."""
    return WorkspaceManager(
        workspaces_root=tmp_path / "workspaces",
        cas_root=tmp_path / "cas",
    )


@pytest.fixture
def backend() -> FsspecSyncBackend:
    """In-memory backend."""
    fs = MemoryFileSystem()
    b = FsspecSyncBackend(fs=fs, root="/test", max_workers=2)
    yield b
    b.close()


class TestWorkspaceManager:
    """Tests for the WorkspaceManager."""

    async def test_open_creates_workspace(self, manager: WorkspaceManager) -> None:
        """open() creates directories and returns a workspace."""
        ws = await manager.open("run-001")

        assert ws.run_id == "run-001"
        assert ws.root.exists()
        assert (ws.root / "input").exists()
        assert (ws.root / "working").exists()
        assert (ws.root / "output").exists()
        assert (ws.root / ".workspace").exists()

        await ws.close()

    async def test_get_returns_open_workspace(self, manager: WorkspaceManager) -> None:
        """get() returns an already-open workspace."""
        ws = await manager.open("run-001")
        assert manager.get("run-001") is ws
        await ws.close()

    async def test_get_returns_none_for_unknown(self, manager: WorkspaceManager) -> None:
        """get() returns None for unknown run IDs."""
        assert manager.get("nonexistent") is None

    async def test_close_removes_from_active(self, manager: WorkspaceManager) -> None:
        """close() removes the workspace from the active set."""
        await manager.open("run-001")
        await manager.close("run-001")
        assert manager.get("run-001") is None

    async def test_close_all(self, manager: WorkspaceManager) -> None:
        """close_all() closes all open workspaces."""
        await manager.open("run-001")
        await manager.open("run-002")
        await manager.close_all()
        assert manager.get("run-001") is None
        assert manager.get("run-002") is None

    async def test_open_from_uri(
        self, manager: WorkspaceManager, backend: FsspecSyncBackend
    ) -> None:
        """open_from_uri() uses the provided backend."""
        ws = await manager.open_from_uri("run-001", backend=backend)
        assert ws._backend is backend
        await ws.close()

    async def test_open_creates_default_dirs_only(self, tmp_path: Path) -> None:
        """Without watched_subdirs, only the engine defaults are created."""
        manager = WorkspaceManager(
            workspaces_root=tmp_path / "workspaces",
            cas_root=tmp_path / "cas",
        )
        ws = await manager.open("run-001")
        assert (ws.root / "deliverables").exists() is False
        assert ws._dirty_tracker.watched_subdirs == ("input", "working", "output")
        await ws.close()

    async def test_open_with_watched_subdirs_precreates_and_wires(self, tmp_path: Path) -> None:
        """Configured watched subdirs are pre-created and reach the tracker."""
        manager = WorkspaceManager(
            workspaces_root=tmp_path / "workspaces",
            cas_root=tmp_path / "cas",
            watched_subdirs=("input", "working", "output", "deliverables"),
        )
        ws = await manager.open("run-001")
        assert (ws.root / "deliverables").is_dir()
        assert ws._dirty_tracker.watched_subdirs == (
            "input",
            "working",
            "output",
            "deliverables",
        )
        await ws.close()

    async def test_open_with_unsafe_watched_subdirs_fails_loud(self, tmp_path: Path) -> None:
        """Unsafe watched subdir entries raise at open (bad config fails fast)."""
        manager = WorkspaceManager(
            workspaces_root=tmp_path / "workspaces",
            cas_root=tmp_path / "cas",
            watched_subdirs=("../escape",),
        )
        with pytest.raises(ValueError, match="unsafe watched subdir"):
            await manager.open("run-001")
