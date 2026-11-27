"""Tests for the WorkspaceManager lifecycle orchestrator.

Tests verify:
    - open() creates workspace directories and components.
    - get() returns open workspaces.
    - close() cleans up.
    - open() loads the remote manifest.
    - backend_factory wiring and failure handling.
    - materialize-content security invariants through the manager path.
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
    return WorkspaceManager(workspaces_root=tmp_path / "workspaces")


@pytest.fixture
def backend(tmp_path: Path) -> FsspecSyncBackend:
    """In-memory backend with a per-test root (fsspec caches fs instances)."""
    fs = MemoryFileSystem()
    b = FsspecSyncBackend(fs=fs, root=f"/test-{tmp_path.name}", max_workers=2)
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

    async def test_open_without_backend_warns_local_only(
        self, manager: WorkspaceManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        """open() without a backend logs the local-only fallback."""
        with caplog.at_level("WARNING"):
            ws = await manager.open("run-001")
        assert "without a remote backend" in caplog.text
        assert ws._backend is None
        await ws.close()

    async def test_open_creates_default_dirs_only(self, tmp_path: Path) -> None:
        """Without watched_subdirs, only the engine defaults are created."""
        manager = WorkspaceManager(workspaces_root=tmp_path / "workspaces")
        ws = await manager.open("run-001")
        assert (ws.root / "deliverables").exists() is False
        assert ws._dirty_tracker.watched_subdirs == ("input", "working", "output")
        await ws.close()

    async def test_open_with_watched_subdirs_precreates_and_wires(self, tmp_path: Path) -> None:
        """Configured watched subdirs are pre-created and reach the tracker."""
        manager = WorkspaceManager(
            workspaces_root=tmp_path / "workspaces",
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
        """Unsafe watched subdir entries raise at construction (bad config fails fast)."""
        with pytest.raises(ValueError, match="unsafe watched subdir"):
            WorkspaceManager(
                workspaces_root=tmp_path / "workspaces",
                watched_subdirs=("../escape",),
            )


# Manifest-load wiring and full turn contract


class TestLifecycleWiring:
    async def test_open_initializes_manifest_state(
        self, tmp_path: Path, backend: FsspecSyncBackend
    ) -> None:
        """Open loads the remote manifest without consumer-side recovery glue."""
        from soothe.workspace.sync.workspace_sync import Manifest, ManifestEntry

        await backend.put_manifest(
            "run-init",
            Manifest(
                run_id="run-init",
                version=7,
                artifacts=[ManifestEntry(path="output/old.md", sha256="a" * 64, size=1)],
            ),
        )
        manager = WorkspaceManager(
            workspaces_root=tmp_path / "workspaces",
            backend_factory=lambda run_id: backend,
        )
        ws = await manager.open("run-init")
        assert ws.manifest is not None
        assert ws.manifest.version == 7
        assert [e.path for e in ws.manifest.artifacts] == ["output/old.md"]
        await ws.close()

    async def test_open_without_manifest_starts_clean(
        self, tmp_path: Path, backend: FsspecSyncBackend
    ) -> None:
        """A run with no prior commits opens with manifest=None."""
        manager = WorkspaceManager(
            workspaces_root=tmp_path / "workspaces",
            backend_factory=lambda run_id: backend,
        )
        ws = await manager.open("run-fresh")
        assert ws.manifest is None
        await ws.close()

    async def test_materialize_content_breaks_escaping_broken_symlink(self, tmp_path: Path) -> None:
        """A broken symlink pointing outside cannot escape the root.

        The old buffered write followed the symlink target and created a
        file OUTSIDE the workspace; the streaming restore replaces the
        symlink itself (os.replace never follows a symlink target).
        """
        outside = tmp_path / "outside.md"
        assert not outside.exists()

        manager = WorkspaceManager(workspaces_root=tmp_path / "workspaces")
        ws = await manager.open("run-sym")

        target_dir = ws.root / "output"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "escape.md").symlink_to(outside)

        content_fs = MemoryFileSystem()
        # Distinct root: MemoryFileSystem instances are cached per storage
        # options in-process, so a shared root leaks state across tests.
        content_backend = FsspecSyncBackend(fs=content_fs, root="/ws-symcontent", max_workers=2)
        try:
            await content_backend.put_content_file("output/escape.md", b"safe content")
            ws._backend = content_backend
            downloaded = await ws.materialize_content(reconcile=False)
            assert downloaded == 1
        finally:
            content_backend.close()

        # Nothing escaped the workspace root.
        assert not outside.exists()
        # The symlink was replaced by a regular file with the content.
        restored = ws.root / "output" / "escape.md"
        assert restored.is_file()
        assert not restored.is_symlink()
        assert restored.read_bytes() == b"safe content"
        await ws.close()

    async def test_materialize_content_skips_invalid_remote_keys(self, tmp_path: Path) -> None:
        """Remote listings with traversal keys never reach path joining."""
        manager = WorkspaceManager(workspaces_root=tmp_path / "workspaces")
        ws = await manager.open("run-badkey")

        escaped = tmp_path / "escaped.txt"
        assert not escaped.exists()

        class _HostileBackend:
            async def list_content_files(self):
                return ["../../escaped.txt", "normal.txt"]

            async def get_content_file(self, rel_path):
                return b"payload" if rel_path == "normal.txt" else None

        ws._backend = _HostileBackend()
        downloaded = await ws.materialize_content(reconcile=False)
        assert downloaded == 1
        assert (ws.root / "normal.txt").read_bytes() == b"payload"
        assert not escaped.exists()
        await ws.close()


class TestConsumerContract:
    """Full turn contract against the complete component stack."""

    async def test_full_turn_round_trip(self, tmp_path: Path) -> None:
        """open → materialize_content → track → write → checkpoint → close,
        then a fresh open re-materializes it all from the content tree."""
        fs = MemoryFileSystem()
        backend = FsspecSyncBackend(fs=fs, root="/ws", max_workers=2)
        try:
            # Seed a prior content-tree file (materials).
            await backend.put_content_file("input/material.txt", b"prior material")

            manager = WorkspaceManager(
                workspaces_root=tmp_path / "workspaces",
                # Per-open backend over the shared filesystem (workspace
                # close releases its thread pool).
                backend_factory=lambda run_id: FsspecSyncBackend(fs=fs, root="/ws", max_workers=2),
            )

            # --- first session (the agent turn) ---
            ws = await manager.open("run-rt")

            fetched = await ws.materialize_content(reconcile=True)
            assert fetched == 1
            assert (ws.root / "input" / "material.txt").read_bytes() == b"prior material"

            ws.start_tracking()
            (ws.root / "output" / "report.md").write_text("final report")
            ws._dirty_tracker.mark_dirty("output/report.md")

            version = await ws.checkpoint()
            assert version == 1
            await manager.close("run-rt")

            # Remote state: manifest committed + content tree carries the write.
            manifest = await backend.get_manifest("run-rt")
            assert manifest is not None
            assert any(e.path == "output/report.md" for e in manifest.artifacts)
            assert await backend.get_content_file("output/report.md") == b"final report"

            # --- second session (restart) ---
            ws2 = await manager.open("run-rt")
            assert ws2.manifest is not None
            assert ws2.manifest.version == 1
            await ws2.materialize_content(reconcile=True)
            assert (ws2.root / "input" / "material.txt").read_bytes() == b"prior material"
            assert (ws2.root / "output" / "report.md").read_bytes() == b"final report"
            await manager.close("run-rt")
        finally:
            backend.close()
