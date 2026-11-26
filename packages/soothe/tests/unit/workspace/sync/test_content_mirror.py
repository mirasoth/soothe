"""Tests for single-root sync — the per-run backend's content tree.

Covers the read path (`Workspace.materialize_content` restores the
plain content tree into the workspace) and the write path
(`Workspace.checkpoint` mirrors dirty-file writes and deletions back to
the content tree and commits the manifest), plus the flat content-tree
primitives on `FsspecSyncBackend`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fsspec.implementations.memory import MemoryFileSystem

from soothe.workspace.sync.backends.fsspec import FsspecSyncBackend
from soothe.workspace.sync.dirty_tracker import DirtyTracker
from soothe.workspace.sync.manager import WorkspaceManager
from soothe.workspace.sync.workspace import Workspace

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_fs() -> MemoryFileSystem:
    fs = MemoryFileSystem()
    yield fs
    fs.store.clear()


@pytest.fixture
def backend(sync_fs: MemoryFileSystem) -> FsspecSyncBackend:
    """Per-run backend (content tree + manifest under one root)."""
    b = FsspecSyncBackend(fs=sync_fs, root="/run", max_workers=2)
    yield b
    b.close()


def _make_workspace(tmp_path: Path, backend: FsspecSyncBackend | None) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(
        run_id="run-001",
        root=root,
        backend=backend,
        dirty_tracker=DirtyTracker(workspace_root=root),
    )


# ---------------------------------------------------------------------------
# FsspecSyncBackend — flat content-tree primitives
# ---------------------------------------------------------------------------


class TestFlatContentPrimitives:
    async def test_roundtrip(self, backend: FsspecSyncBackend) -> None:
        await backend.put_content_file("materials/m1/notes.md", b"hello")
        await backend.put_content_file("materials/m2/data.bin", b"\x00\x01")

        files = await backend.list_content_files()
        assert files == ["materials/m1/notes.md", "materials/m2/data.bin"]

        assert await backend.get_content_file("materials/m1/notes.md") == b"hello"
        assert await backend.get_content_file("missing.txt") is None

        await backend.delete_content_file("materials/m1/notes.md")
        assert await backend.get_content_file("materials/m1/notes.md") is None
        # Idempotent delete.
        await backend.delete_content_file("materials/m1/notes.md")

    async def test_list_empty_when_tree_missing(self, backend: FsspecSyncBackend) -> None:
        assert await backend.list_content_files() == []

    async def test_rejects_path_traversal(self, backend: FsspecSyncBackend) -> None:
        with pytest.raises(ValueError):
            await backend.put_content_file("../escape.txt", b"x")
        with pytest.raises(ValueError):
            await backend.get_content_file("/abs/path")

    async def test_manifest_key_space_excluded_from_content_listing(
        self, backend: FsspecSyncBackend, sync_fs: MemoryFileSystem
    ) -> None:
        await backend.put_content_file("output/a.md", b"a")
        sync_fs.pipe("/run/.workspace/manifest.json", b"{}")

        assert await backend.list_content_files() == ["output/a.md"]


# ---------------------------------------------------------------------------
# Workspace.materialize_content — read path
# ---------------------------------------------------------------------------


class TestMaterializeContent:
    async def test_materializes_files_at_relative_paths(
        self, tmp_path: Path, backend: FsspecSyncBackend
    ) -> None:
        await backend.put_content_file("materials/m1/report.md", b"# report")
        await backend.put_content_file("materials/m1/fig.png", b"\x89PNG")

        ws = _make_workspace(tmp_path, backend)
        fetched = await ws.materialize_content()

        assert fetched == 2
        assert (ws.root / "materials/m1/report.md").read_bytes() == b"# report"
        assert (ws.root / "materials/m1/fig.png").read_bytes() == b"\x89PNG"

    async def test_skips_unchanged_and_engine_dirs(
        self, tmp_path: Path, backend: FsspecSyncBackend, sync_fs: MemoryFileSystem
    ) -> None:
        await backend.put_content_file("materials/m1/a.txt", b"same")
        # Engine-private key space (the remote manifest) never lands locally.
        sync_fs.pipe("/run/.workspace/manifest.json", b"{}")

        ws = _make_workspace(tmp_path, backend)
        target = ws.root / "materials/m1/a.txt"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"same")

        fetched = await ws.materialize_content()
        assert fetched == 0  # unchanged locally + engine dir skipped
        assert not (ws.root / ".workspace/manifest.json").exists()

    async def test_overwrites_diverged_local_copy(
        self, tmp_path: Path, backend: FsspecSyncBackend
    ) -> None:
        await backend.put_content_file("materials/m1/a.txt", b"new")
        ws = _make_workspace(tmp_path, backend)
        target = ws.root / "materials/m1/a.txt"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"stale-local")

        fetched = await ws.materialize_content()
        assert fetched == 1
        assert target.read_bytes() == b"new"

    async def test_noop_without_backend(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path, None)
        assert await ws.materialize_content() == 0


# ---------------------------------------------------------------------------
# Workspace.materialize_content — reconcile (delete half of the read path)
# ---------------------------------------------------------------------------


class TestMaterializeContentReconcile:
    async def test_removes_backend_deleted_file(
        self, tmp_path: Path, backend: FsspecSyncBackend
    ) -> None:
        await backend.put_content_file("materials/m1/a.txt", b"v1")
        ws = _make_workspace(tmp_path, backend)
        target = ws.root / "materials/m1/a.txt"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"v1")

        # Backend deletes the object; the next materialize must reconcile.
        await backend.delete_content_file("materials/m1/a.txt")
        await ws.materialize_content()

        assert not target.exists()

    async def test_protected_artifact_survives(
        self, tmp_path: Path, backend: FsspecSyncBackend
    ) -> None:
        ws = _make_workspace(tmp_path, backend)
        artifact = ws.root / "output/analysis.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"agent output")

        await ws.materialize_content(protected=["output/analysis.md"])

        assert artifact.read_bytes() == b"agent output"

    async def test_unprotected_stale_file_removed(
        self, tmp_path: Path, backend: FsspecSyncBackend
    ) -> None:
        await backend.put_content_file("materials/m1/live.txt", b"live")
        ws = _make_workspace(tmp_path, backend)
        stale = ws.root / "output/stale.txt"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"stale")

        await ws.materialize_content()

        assert not stale.exists()
        assert (ws.root / "materials/m1/live.txt").read_bytes() == b"live"

    async def test_symlink_untouched(self, tmp_path: Path, backend: FsspecSyncBackend) -> None:
        await backend.put_content_file("materials/m1/live.txt", b"live")
        ws = _make_workspace(tmp_path, backend)
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"outside")
        link = ws.root / "materials/m1/link.txt"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)

        await ws.materialize_content()

        assert link.is_symlink()  # never followed or deleted

    async def test_skip_dirs_untouched(self, tmp_path: Path, backend: FsspecSyncBackend) -> None:
        ws = _make_workspace(tmp_path, backend)
        state_db = ws.root / ".workspace/state.db"
        state_db.parent.mkdir(parents=True)
        state_db.write_bytes(b"engine state")

        await ws.materialize_content()

        assert state_db.read_bytes() == b"engine state"

    async def test_reconcile_false_preserves_stale(
        self, tmp_path: Path, backend: FsspecSyncBackend
    ) -> None:
        ws = _make_workspace(tmp_path, backend)
        stale = ws.root / "output/stale.txt"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"stale")

        await ws.materialize_content(reconcile=False)

        assert stale.read_bytes() == b"stale"

    async def test_empty_parent_dirs_pruned_but_top_level_kept(
        self, tmp_path: Path, backend: FsspecSyncBackend
    ) -> None:
        await backend.put_content_file("materials/m1/deep/a.txt", b"x")
        ws = _make_workspace(tmp_path, backend)
        deep = ws.root / "materials/m1/deep/a.txt"
        deep.parent.mkdir(parents=True)
        deep.write_bytes(b"x")
        out = ws.root / "output/result.txt"
        out.parent.mkdir(parents=True)
        out.write_bytes(b"stale output")
        (ws.root / ".workspace").mkdir(exist_ok=True)  # engine state dir

        # Backend removes the deep file; output/result.txt is stale & unprotected.
        await backend.delete_content_file("materials/m1/deep/a.txt")
        await ws.materialize_content()

        assert not (ws.root / "materials/m1/deep").exists()
        assert not out.exists()
        assert (ws.root / "output").is_dir()  # engine-created top-level dir kept
        assert (ws.root / ".workspace").is_dir()


# ---------------------------------------------------------------------------
# Workspace.checkpoint — write path (mirror back)
# ---------------------------------------------------------------------------


def _make_tracked_workspace(
    tmp_path: Path,
    backend: FsspecSyncBackend,
    *,
    run_id: str = "run-001",
) -> tuple[Workspace, DirtyTracker, Path]:
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    tracker = DirtyTracker(workspace_root=root)
    ws = Workspace(
        run_id=run_id,
        root=root,
        backend=backend,
        dirty_tracker=tracker,
    )
    return ws, tracker, root


class TestCheckpointContentMirror:
    async def test_dirty_files_mirrored_to_content_tree(
        self, tmp_path: Path, backend: FsspecSyncBackend
    ) -> None:
        ws, tracker, root = _make_tracked_workspace(tmp_path, backend)
        out = root / "output/result.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"agent output")
        tracker.mark_dirty("output/result.txt")

        await ws.checkpoint()

        assert await backend.get_content_file("output/result.txt") == b"agent output"

    async def test_deletions_mirrored_to_content_tree(
        self, tmp_path: Path, backend: FsspecSyncBackend
    ) -> None:
        await backend.put_content_file("output/stale.txt", b"old")
        ws, tracker, root = _make_tracked_workspace(tmp_path, backend)
        # mark_dirty on a path that no longer exists on disk registers a deletion.
        tracker.mark_dirty("output/stale.txt")

        await ws.checkpoint()

        assert await backend.get_content_file("output/stale.txt") is None

    async def test_missing_dirty_file_treated_as_delete(
        self, tmp_path: Path, backend: FsspecSyncBackend
    ) -> None:
        await backend.put_content_file("gone.txt", b"vanishing")
        ws, tracker, root = _make_tracked_workspace(tmp_path, backend)
        # Dirty path whose file no longer exists on disk.
        tracker.mark_dirty("gone.txt")

        await ws.checkpoint()

        assert await backend.get_content_file("gone.txt") is None


# ---------------------------------------------------------------------------
# WorkspaceManager — backend_factory wiring + round trip
# ---------------------------------------------------------------------------


class TestManagerContentWiring:
    async def test_factory_wires_per_run_backend(
        self, tmp_path: Path, backend: FsspecSyncBackend
    ) -> None:
        created_for: list[str] = []

        def factory(run_id: str):
            created_for.append(run_id)
            return backend

        manager = WorkspaceManager(
            workspaces_root=tmp_path / "workspaces",
            backend_factory=factory,
        )
        ws = await manager.open("run-001")
        assert created_for == ["run-001"]
        assert ws._backend is backend
        await manager.close("run-001")

    async def test_factory_failure_disables_sync_for_run(
        self,
        tmp_path: Path,
    ) -> None:
        def factory(run_id: str):
            raise RuntimeError("no backend for this run")

        manager = WorkspaceManager(
            workspaces_root=tmp_path / "workspaces",
            backend_factory=factory,
        )
        ws = await manager.open("run-001")
        assert ws._backend is None
        await manager.close("run-001")

    async def test_factory_none_runs_local_only(self, tmp_path: Path) -> None:
        manager = WorkspaceManager(
            workspaces_root=tmp_path / "workspaces",
            backend_factory=None,
        )
        ws = await manager.open("run-001")
        assert ws._backend is None
        await manager.close("run-001")

    async def test_single_root_round_trip_after_local_recycle(
        self, tmp_path: Path, sync_fs: MemoryFileSystem
    ) -> None:
        """Materials materialize in; agent outputs mirror out; reopen restores both.

        A local-dir recycle (server restart / VM recycle) re-opens the same
        workspace root: the manifest loads from `.workspace/manifest.json`
        and the content tree re-materializes.
        """
        # Backend uploads a material to the run's content root.
        sync_fs.pipe("/run/materials/m1/notes.md", b"material v1")

        def factory(run_id: str):
            # Production shape: a FRESH per-run backend over the shared
            # filesystem, rooted at the run's own root. `Workspace.close()`
            # closes per-run backends (their executor), so the factory must
            # hand a new instance to each open.
            return FsspecSyncBackend(fs=sync_fs, root="/run", max_workers=2)

        manager = WorkspaceManager(
            workspaces_root=tmp_path / "workspaces",
            backend_factory=factory,
        )

        # Turn 1: materialize content, agent writes output, checkpoint.
        ws1 = await manager.open("run-001")
        fetched = await ws1.materialize_content()
        assert fetched == 1
        assert (ws1.root / "materials/m1/notes.md").read_bytes() == b"material v1"

        agent_out = ws1.root / "output/analysis.md"
        agent_out.parent.mkdir(parents=True, exist_ok=True)
        agent_out.write_bytes(b"agent analysis")
        ws1._dirty_tracker.scan_now()
        ws1._dirty_tracker.mark_dirty("output/analysis.md")
        await ws1.checkpoint()
        await manager.close("run-001")

        # The single root now holds the material, the mirrored output, and
        # the manifest in the engine key space.
        assert sync_fs.cat("/run/output/analysis.md") == b"agent analysis"
        assert "/run/.workspace/manifest.json" in sync_fs.store

        # Turn 2: the local dir is wiped (VM recycle); reopening the same
        # run root re-materializes everything.
        shutil.rmtree(tmp_path / "workspaces" / "run-001")
        ws2 = await manager.open("run-001")
        fetched2 = await ws2.materialize_content()
        assert fetched2 == 2
        assert (ws2.root / "materials/m1/notes.md").read_bytes() == b"material v1"
        assert (ws2.root / "output/analysis.md").read_bytes() == b"agent analysis"
        assert ws2.manifest is not None
        assert ws2.manifest.version == 1
        await manager.close("run-001")
