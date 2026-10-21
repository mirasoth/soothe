"""Tests for dual-source sync (content mirror / ``content_backend``).

Covers the read path (``Workspace.materialize_content`` restores a plain
content tree into the workspace) and the write path (``CheckpointManager``
mirrors dirty-file writes and deletions back to the content tree), plus the
flat content-tree primitives on ``FsspecSyncBackend``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fsspec.implementations.memory import MemoryFileSystem

from soothe.workspace.sync.backends.fsspec import FsspecSyncBackend
from soothe.workspace.sync.cas import CASCache
from soothe.workspace.sync.checkpoint import CheckpointManager
from soothe.workspace.sync.dirty_tracker import DirtyTracker
from soothe.workspace.sync.manager import WorkspaceManager
from soothe.workspace.sync.workspace import Workspace

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_backend():
    fs = MemoryFileSystem()
    b = FsspecSyncBackend(fs=fs, root="/sync", max_workers=2)
    yield b
    b.close()
    fs.store.clear()


@pytest.fixture
def content_backend():
    fs = MemoryFileSystem()
    b = FsspecSyncBackend(fs=fs, root="/content", max_workers=2)
    yield b
    b.close()
    fs.store.clear()


def _make_workspace(
    tmp_path: Path,
    sync_backend: FsspecSyncBackend,
    content_backend: FsspecSyncBackend | None,
) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(
        run_id="run-001",
        root=root,
        backend=sync_backend,
        cas=CASCache(cache_root=tmp_path / "cas"),
        state_store=None,
        dirty_tracker=DirtyTracker(workspace_root=root),
        debouncer=None,
        checkpoint_mgr=None,
        uploader=None,
        content_backend=content_backend,
    )


# ---------------------------------------------------------------------------
# FsspecSyncBackend — flat content-tree primitives
# ---------------------------------------------------------------------------


class TestFlatContentPrimitives:
    async def test_roundtrip(self, content_backend: FsspecSyncBackend) -> None:
        await content_backend.put_content_file("materials/m1/notes.md", b"hello")
        await content_backend.put_content_file("materials/m2/data.bin", b"\x00\x01")

        files = await content_backend.list_content_files()
        assert files == ["materials/m1/notes.md", "materials/m2/data.bin"]

        assert await content_backend.get_content_file("materials/m1/notes.md") == b"hello"
        assert await content_backend.get_content_file("missing.txt") is None

        await content_backend.delete_content_file("materials/m1/notes.md")
        assert await content_backend.get_content_file("materials/m1/notes.md") is None
        # Idempotent delete.
        await content_backend.delete_content_file("materials/m1/notes.md")

    async def test_list_empty_when_tree_missing(self, content_backend: FsspecSyncBackend) -> None:
        assert await content_backend.list_content_files() == []

    async def test_rejects_path_traversal(self, content_backend: FsspecSyncBackend) -> None:
        with pytest.raises(ValueError):
            await content_backend.put_content_file("../escape.txt", b"x")
        with pytest.raises(ValueError):
            await content_backend.get_content_file("/abs/path")


# ---------------------------------------------------------------------------
# Workspace.materialize_content — read path
# ---------------------------------------------------------------------------


class TestMaterializeContent:
    async def test_materializes_files_at_relative_paths(
        self, tmp_path: Path, sync_backend, content_backend
    ) -> None:
        await content_backend.put_content_file("materials/m1/report.md", b"# report")
        await content_backend.put_content_file("materials/m1/fig.png", b"\x89PNG")

        ws = _make_workspace(tmp_path, sync_backend, content_backend)
        fetched = await ws.materialize_content()

        assert fetched == 2
        assert (ws.root / "materials/m1/report.md").read_bytes() == b"# report"
        assert (ws.root / "materials/m1/fig.png").read_bytes() == b"\x89PNG"

    async def test_skips_unchanged_and_engine_dirs(
        self, tmp_path: Path, sync_backend, content_backend
    ) -> None:
        await content_backend.put_content_file("materials/m1/a.txt", b"same")
        await content_backend.put_content_file(".workspace/state.db", b"engine")

        ws = _make_workspace(tmp_path, sync_backend, content_backend)
        target = ws.root / "materials/m1/a.txt"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"same")

        fetched = await ws.materialize_content()
        assert fetched == 0  # unchanged locally + engine dir skipped
        assert not (ws.root / ".workspace/state.db").exists()

    async def test_overwrites_diverged_local_copy(
        self, tmp_path: Path, sync_backend, content_backend
    ) -> None:
        await content_backend.put_content_file("materials/m1/a.txt", b"new")
        ws = _make_workspace(tmp_path, sync_backend, content_backend)
        target = ws.root / "materials/m1/a.txt"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"stale-local")

        fetched = await ws.materialize_content()
        assert fetched == 1
        assert target.read_bytes() == b"new"

    async def test_noop_without_content_backend(self, tmp_path: Path, sync_backend) -> None:
        ws = _make_workspace(tmp_path, sync_backend, None)
        assert await ws.materialize_content() == 0


# ---------------------------------------------------------------------------
# Workspace.materialize_content — reconcile (delete half of the read path)
# ---------------------------------------------------------------------------


class TestMaterializeContentReconcile:
    async def test_removes_backend_deleted_file(
        self, tmp_path: Path, sync_backend, content_backend
    ) -> None:
        await content_backend.put_content_file("materials/m1/a.txt", b"v1")
        ws = _make_workspace(tmp_path, sync_backend, content_backend)
        target = ws.root / "materials/m1/a.txt"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"v1")

        # Backend deletes the object; the next materialize must reconcile.
        await content_backend.delete_content_file("materials/m1/a.txt")
        await ws.materialize_content()

        assert not target.exists()

    async def test_protected_artifact_survives(
        self, tmp_path: Path, sync_backend, content_backend
    ) -> None:
        ws = _make_workspace(tmp_path, sync_backend, content_backend)
        artifact = ws.root / "output/analysis.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"agent output")

        await ws.materialize_content(protected=["output/analysis.md"])

        assert artifact.read_bytes() == b"agent output"

    async def test_unprotected_stale_file_removed(
        self, tmp_path: Path, sync_backend, content_backend
    ) -> None:
        await content_backend.put_content_file("materials/m1/live.txt", b"live")
        ws = _make_workspace(tmp_path, sync_backend, content_backend)
        stale = ws.root / "output/stale.txt"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"stale")

        await ws.materialize_content()

        assert not stale.exists()
        assert (ws.root / "materials/m1/live.txt").read_bytes() == b"live"

    async def test_symlink_untouched(self, tmp_path: Path, sync_backend, content_backend) -> None:
        await content_backend.put_content_file("materials/m1/live.txt", b"live")
        ws = _make_workspace(tmp_path, sync_backend, content_backend)
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"outside")
        link = ws.root / "materials/m1/link.txt"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)

        await ws.materialize_content()

        assert link.is_symlink()  # never followed or deleted

    async def test_skip_dirs_untouched(self, tmp_path: Path, sync_backend, content_backend) -> None:
        ws = _make_workspace(tmp_path, sync_backend, content_backend)
        state_db = ws.root / ".workspace/state.db"
        state_db.parent.mkdir(parents=True)
        state_db.write_bytes(b"engine state")

        await ws.materialize_content()

        assert state_db.read_bytes() == b"engine state"

    async def test_reconcile_false_preserves_stale(
        self, tmp_path: Path, sync_backend, content_backend
    ) -> None:
        ws = _make_workspace(tmp_path, sync_backend, content_backend)
        stale = ws.root / "output/stale.txt"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"stale")

        await ws.materialize_content(reconcile=False)

        assert stale.read_bytes() == b"stale"

    async def test_empty_parent_dirs_pruned_but_top_level_kept(
        self, tmp_path: Path, sync_backend, content_backend
    ) -> None:
        await content_backend.put_content_file("materials/m1/deep/a.txt", b"x")
        ws = _make_workspace(tmp_path, sync_backend, content_backend)
        deep = ws.root / "materials/m1/deep/a.txt"
        deep.parent.mkdir(parents=True)
        deep.write_bytes(b"x")
        out = ws.root / "output/result.txt"
        out.parent.mkdir(parents=True)
        out.write_bytes(b"stale output")
        (ws.root / ".workspace").mkdir(exist_ok=True)  # engine state dir

        # Backend removes the deep file; output/result.txt is stale & unprotected.
        await content_backend.delete_content_file("materials/m1/deep/a.txt")
        await ws.materialize_content()

        assert not (ws.root / "materials/m1/deep").exists()
        assert not out.exists()
        assert (ws.root / "output").is_dir()  # engine-created top-level dir kept
        assert (ws.root / ".workspace").is_dir()


# ---------------------------------------------------------------------------
# CheckpointManager — write path (mirror back)
# ---------------------------------------------------------------------------


def _make_checkpoint_mgr(
    tmp_path: Path,
    sync_backend,
    content_backend,
    *,
    run_id: str = "run-001",
) -> tuple[CheckpointManager, DirtyTracker, Path]:
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    tracker = DirtyTracker(workspace_root=root)
    mgr = CheckpointManager(
        run_id=run_id,
        backend=sync_backend,
        cas=CASCache(cache_root=tmp_path / "cas"),
        dirty_tracker=tracker,
        workspace_root=root,
        content_backend=content_backend,
    )
    return mgr, tracker, root


class TestCheckpointContentMirror:
    async def test_dirty_files_mirrored_to_content_tree(
        self, tmp_path: Path, sync_backend, content_backend
    ) -> None:
        mgr, tracker, root = _make_checkpoint_mgr(tmp_path, sync_backend, content_backend)
        out = root / "output/result.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"agent output")
        tracker.mark_dirty("output/result.txt")

        await mgr.create_checkpoint()

        assert await content_backend.get_content_file("output/result.txt") == b"agent output"

    async def test_deletions_mirrored_to_content_tree(
        self, tmp_path: Path, sync_backend, content_backend
    ) -> None:
        await content_backend.put_content_file("output/stale.txt", b"old")
        mgr, tracker, root = _make_checkpoint_mgr(tmp_path, sync_backend, content_backend)
        # mark_dirty on a path that no longer exists on disk registers a deletion.
        tracker.mark_dirty("output/stale.txt")

        await mgr.create_checkpoint()

        assert await content_backend.get_content_file("output/stale.txt") is None

    async def test_missing_dirty_file_treated_as_delete(
        self, tmp_path: Path, sync_backend, content_backend
    ) -> None:
        await content_backend.put_content_file("gone.txt", b"vanishing")
        mgr, tracker, root = _make_checkpoint_mgr(tmp_path, sync_backend, content_backend)
        # Dirty path whose file no longer exists on disk.
        tracker.mark_dirty("gone.txt")

        await mgr.create_checkpoint()

        assert await content_backend.get_content_file("gone.txt") is None

    async def test_mirror_failure_is_not_fatal(self, tmp_path: Path, sync_backend) -> None:
        class FailingContentBackend:
            async def put_content_file(self, rel_path, data):
                raise RuntimeError("content backend down")

            async def delete_content_file(self, rel_path):
                raise RuntimeError("content backend down")

        mgr, tracker, root = _make_checkpoint_mgr(tmp_path, sync_backend, FailingContentBackend())
        out = root / "output/ok.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"data")
        tracker.mark_dirty("output/ok.txt")

        # Must not raise — CAS/manifest remain the source of truth.
        checkpoint_id = await mgr.create_checkpoint()
        assert checkpoint_id == "c001"


# ---------------------------------------------------------------------------
# WorkspaceManager — content_backend_factory wiring + round trip
# ---------------------------------------------------------------------------


class TestManagerContentWiring:
    async def test_factory_wires_per_run_content_backend(
        self, tmp_path: Path, sync_backend, content_backend
    ) -> None:
        created_for: list[str] = []

        def factory(run_id: str):
            created_for.append(run_id)
            return content_backend

        manager = WorkspaceManager(
            workspaces_root=tmp_path / "workspaces",
            cas_root=tmp_path / "cas",
            backend=sync_backend,
            content_backend_factory=factory,
        )
        ws = await manager.open("run-001")
        assert created_for == ["run-001"]
        assert ws._content_backend is content_backend
        assert ws._checkpoint_mgr._content_backend is content_backend
        await manager.close("run-001")

    async def test_factory_failure_disables_mirror_for_run(
        self, tmp_path: Path, sync_backend
    ) -> None:
        def factory(run_id: str):
            raise RuntimeError("no content root for this run")

        manager = WorkspaceManager(
            workspaces_root=tmp_path / "workspaces",
            cas_root=tmp_path / "cas",
            backend=sync_backend,
            content_backend_factory=factory,
        )
        ws = await manager.open("run-001")
        assert ws._content_backend is None
        await manager.close("run-001")

    async def test_dual_source_round_trip(
        self, tmp_path: Path, sync_backend, content_backend
    ) -> None:
        """Materials materialize in; agent outputs mirror out; reopen restores both."""
        # Backend uploads a material to the content root.
        await content_backend.put_content_file("materials/m1/notes.md", b"material v1")

        def factory(run_id: str):
            return content_backend

        manager = WorkspaceManager(
            workspaces_root=tmp_path / "workspaces",
            cas_root=tmp_path / "cas",
            backend=sync_backend,
            content_backend_factory=factory,
        )

        # Run 1: materialize content, agent writes output, checkpoint.
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

        # Content tree now holds both the material and the mirrored output.
        assert await content_backend.get_content_file("output/analysis.md") == b"agent analysis"

        # Run 2: a fresh workspace (new run_id → new dir, e.g. after a server
        # restart or VM recycle) re-materializes everything from the content tree.
        ws2 = await manager.open("run-002")
        fetched2 = await ws2.materialize_content()
        assert fetched2 == 2
        assert (ws2.root / "materials/m1/notes.md").read_bytes() == b"material v1"
        assert (ws2.root / "output/analysis.md").read_bytes() == b"agent analysis"
        await manager.close("run-002")
