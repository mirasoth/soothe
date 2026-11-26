"""Tests for ``Workspace.checkpoint`` — content-authoritative commit semantics.

Under IG-CORE-010 the plain content tree is the authoritative file store
and the manifest write is the atomic commit point.  These tests pin:

    - First commit creates the manifest at version 1; commits increment.
    - Dirty writes stream to the content tree and land in manifest
      artifacts (path → sha256/size).
    - Deletions leave the manifest and mirror to the content tree;
      dirty paths vanishing before the commit count as deletions.
    - Mid-commit writes stay dirty (F-C2).
    - Per-file upload failures produce a partial commit + RuntimeError.
    - Manifest write failure (incl. version conflict) leaves everything
      dirty — the commit point never fired.
    - Recovery = manifest + content tree re-materialized by a fresh
      workspace.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from fsspec.implementations.memory import MemoryFileSystem

from soothe.workspace.sync.backends.fsspec import FsspecSyncBackend
from soothe.workspace.sync.dirty_tracker import DirtyTracker
from soothe.workspace.sync.errors import ConcurrentModificationError
from soothe.workspace.sync.workspace import Workspace
from soothe.workspace.sync.workspace_sync import Manifest, ManifestEntry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RUN_ID = "run-123"


@pytest.fixture
def sync_fs() -> MemoryFileSystem:
    """Fresh in-memory filesystem for the manifest root."""
    mem = MemoryFileSystem()
    yield mem
    mem.store.clear()


@pytest.fixture
def content_fs() -> MemoryFileSystem:
    """Fresh in-memory filesystem for the content tree."""
    mem = MemoryFileSystem()
    yield mem
    mem.store.clear()


@pytest.fixture
def backend(sync_fs: MemoryFileSystem) -> FsspecSyncBackend:
    """Manifest-root backend."""
    b = FsspecSyncBackend(fs=sync_fs, root="/sync", max_workers=2)
    yield b
    b.close()


@pytest.fixture
def content_backend(content_fs: MemoryFileSystem) -> FsspecSyncBackend:
    """Content-tree backend."""
    b = FsspecSyncBackend(fs=content_fs, root="/content", max_workers=2)
    yield b
    b.close()


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """Temporary workspace with the engine subdirectories."""
    root = tmp_path / "ws"
    for subdir in ("input", "working", "output", ".workspace"):
        (root / subdir).mkdir(parents=True)
    return root


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_workspace(
    root: Path,
    backend: FsspecSyncBackend | None,
    content_backend: Any,
    *,
    run_id: str = RUN_ID,
    manifest: Manifest | None = None,
) -> Workspace:
    """Build a Workspace over the given backends (fresh tracker)."""
    return Workspace(
        run_id=run_id,
        root=root,
        backend=backend,
        content_backend=content_backend,
        dirty_tracker=DirtyTracker(workspace_root=root),
        manifest=manifest,
    )


# ---------------------------------------------------------------------------
# Commit basics
# ---------------------------------------------------------------------------


class TestCommitBasics:
    async def test_first_commit_streams_content_and_writes_manifest_v1(
        self, workspace_root: Path, backend: FsspecSyncBackend, content_backend: FsspecSyncBackend
    ) -> None:
        ws = _make_workspace(workspace_root, backend, content_backend)
        ws.start_tracking()
        (workspace_root / "output" / "report.md").write_bytes(b"final report")

        version = await ws.checkpoint()

        assert version == 1
        assert await content_backend.get_content_file("output/report.md") == b"final report"
        manifest = await backend.get_manifest(RUN_ID)
        assert manifest is not None
        assert manifest.version == 1
        entry = next(e for e in manifest.artifacts if e.path == "output/report.md")
        assert entry.sha256 == _sha(b"final report")
        assert entry.size == len(b"final report")

    async def test_subsequent_commit_increments_version(
        self, workspace_root: Path, backend: FsspecSyncBackend, content_backend: FsspecSyncBackend
    ) -> None:
        ws = _make_workspace(workspace_root, backend, content_backend)
        ws.start_tracking()
        (workspace_root / "output" / "a.md").write_bytes(b"a")
        assert await ws.checkpoint() == 1

        (workspace_root / "output" / "b.md").write_bytes(b"b")
        assert await ws.checkpoint() == 2

        manifest = await backend.get_manifest(RUN_ID)
        assert manifest is not None
        assert manifest.version == 2
        assert [e.path for e in manifest.artifacts] == ["output/a.md", "output/b.md"]

    async def test_commit_folds_updates_into_existing_entries(
        self, workspace_root: Path, backend: FsspecSyncBackend, content_backend: FsspecSyncBackend
    ) -> None:
        ws = _make_workspace(workspace_root, backend, content_backend)
        ws.start_tracking()
        target = workspace_root / "output" / "a.md"
        target.write_bytes(b"v1")
        await ws.checkpoint()
        target.write_bytes(b"v2-longer")
        await ws.checkpoint()

        manifest = await backend.get_manifest(RUN_ID)
        assert manifest is not None
        assert [e.path for e in manifest.artifacts] == ["output/a.md"]
        entry = manifest.artifacts[0]
        assert entry.sha256 == _sha(b"v2-longer")
        assert entry.size == len(b"v2-longer")

    async def test_checkpoint_without_changes_returns_none(
        self, workspace_root: Path, backend: FsspecSyncBackend, content_backend: FsspecSyncBackend
    ) -> None:
        ws = _make_workspace(workspace_root, backend, content_backend)
        ws.start_tracking()
        assert await ws.checkpoint() is None

    async def test_checkpoint_without_backends_returns_none(self, workspace_root: Path) -> None:
        ws = _make_workspace(workspace_root, None, None)
        ws.start_tracking()
        (workspace_root / "output" / "a.md").write_bytes(b"a")
        assert await ws.checkpoint() is None

    async def test_checkpoint_while_closing_returns_none(
        self, workspace_root: Path, backend: FsspecSyncBackend, content_backend: FsspecSyncBackend
    ) -> None:
        ws = _make_workspace(workspace_root, backend, content_backend)
        ws._closing = True
        (workspace_root / "output" / "a.md").write_bytes(b"a")
        assert await ws.checkpoint() is None


# ---------------------------------------------------------------------------
# Deletions
# ---------------------------------------------------------------------------


class TestCommitDeletions:
    async def test_tracked_deletion_removes_artifact_and_mirrors(
        self, workspace_root: Path, backend: FsspecSyncBackend, content_backend: FsspecSyncBackend
    ) -> None:
        ws = _make_workspace(workspace_root, backend, content_backend)
        ws.start_tracking()
        (workspace_root / "output" / "a.md").write_bytes(b"a")
        (workspace_root / "output" / "b.md").write_bytes(b"b")
        await ws.checkpoint()

        (workspace_root / "output" / "a.md").unlink()
        assert await ws.checkpoint() == 2

        manifest = await backend.get_manifest(RUN_ID)
        assert manifest is not None
        assert [e.path for e in manifest.artifacts] == ["output/b.md"]
        assert await content_backend.get_content_file("output/a.md") is None
        assert await content_backend.get_content_file("output/b.md") == b"b"

    async def test_dirty_path_vanishing_before_commit_is_a_deletion(
        self, workspace_root: Path, backend: FsspecSyncBackend, content_backend: FsspecSyncBackend
    ) -> None:
        ws = _make_workspace(workspace_root, backend, content_backend)
        ws.start_tracking()
        target = workspace_root / "output" / "a.md"
        target.write_bytes(b"a")
        await ws.checkpoint()

        # Modify (dirty), then remove the file before the commit — the
        # vanished dirty path must commit as a deletion, not a write.
        target.write_bytes(b"dirty")
        target.unlink()
        assert await ws.checkpoint() == 2

        manifest = await backend.get_manifest(RUN_ID)
        assert manifest is not None
        assert manifest.artifacts == []
        assert await content_backend.get_content_file("output/a.md") is None

    async def test_manual_mark_dirty_on_missing_path_is_a_deletion(
        self, workspace_root: Path, backend: FsspecSyncBackend, content_backend: FsspecSyncBackend
    ) -> None:
        await content_backend.put_content_file("output/stale.txt", b"old")
        ws = _make_workspace(workspace_root, backend, content_backend)
        ws.start_tracking()
        # mark_dirty on a path with no local file registers a deletion.
        ws._dirty_tracker.mark_dirty("output/stale.txt")

        assert await ws.checkpoint() == 1
        assert await content_backend.get_content_file("output/stale.txt") is None


# ---------------------------------------------------------------------------
# F-C2: mid-commit writes stay dirty
# ---------------------------------------------------------------------------


class _MidCommitHookBackend:
    """Content backend wrapper that performs a write during the commit."""

    def __init__(self, inner: Any, root: Path, tracker: DirtyTracker) -> None:
        self._inner = inner
        self._root = root
        self._tracker = tracker
        self._fired = False

    async def put_content_file_from_file(self, rel_path: str, src: Path) -> None:
        if not self._fired:
            self._fired = True
            mid = self._root / "output" / "mid.md"
            mid.write_bytes(b"mid-commit write")
            self._tracker.mark_dirty("output/mid.md")
        await self._inner.put_content_file_from_file(rel_path, src)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class TestMidCommitSemantics:
    async def test_mid_commit_write_stays_dirty(
        self, workspace_root: Path, backend: FsspecSyncBackend, content_backend: FsspecSyncBackend
    ) -> None:
        tracker = DirtyTracker(workspace_root=workspace_root)
        hooked = _MidCommitHookBackend(content_backend, workspace_root, tracker)
        ws = Workspace(
            run_id=RUN_ID,
            root=workspace_root,
            backend=backend,
            content_backend=hooked,
            dirty_tracker=tracker,
        )
        ws.start_tracking()
        (workspace_root / "output" / "a.md").write_bytes(b"a")

        version = await ws.checkpoint()

        assert version == 1
        # Only the pre-commit write is acked; the mid-commit write stays
        # dirty for the next commit (F-C2).
        assert "output/mid.md" in tracker.get_dirty()
        assert "output/a.md" not in tracker.get_dirty()
        # The mid-commit write is not in this commit's manifest.
        manifest = await backend.get_manifest(RUN_ID)
        assert manifest is not None
        assert [e.path for e in manifest.artifacts] == ["output/a.md"]

        # The next commit picks it up.
        assert await ws.checkpoint() == 2
        assert tracker.get_dirty() == {}
        manifest = await backend.get_manifest(RUN_ID)
        assert manifest is not None
        assert [e.path for e in manifest.artifacts] == ["output/a.md", "output/mid.md"]
        assert await content_backend.get_content_file("output/mid.md") == b"mid-commit write"


# ---------------------------------------------------------------------------
# Failure semantics
# ---------------------------------------------------------------------------


class _FailingContentBackend:
    """Content backend wrapper failing one path's uploads."""

    def __init__(self, inner: Any, fail_path: str) -> None:
        self._inner = inner
        self._fail_path = fail_path

    async def put_content_file_from_file(self, rel_path: str, src: Path) -> None:
        if rel_path == self._fail_path:
            raise OSError("injected upload failure")
        await self._inner.put_content_file_from_file(rel_path, src)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _FailingManifestBackend:
    """Manifest backend wrapper whose put_manifest always fails."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def put_manifest(self, *args: Any, **kwargs: Any) -> Manifest:
        raise RuntimeError("injected manifest write failure")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class TestCommitFailures:
    async def test_upload_failure_partial_commit_and_stays_dirty(
        self, workspace_root: Path, backend: FsspecSyncBackend, content_backend: FsspecSyncBackend
    ) -> None:
        hooked = _FailingContentBackend(content_backend, "output/bad.md")
        ws = _make_workspace(workspace_root, backend, hooked)
        ws.start_tracking()
        (workspace_root / "output" / "ok.md").write_bytes(b"ok")
        (workspace_root / "output" / "bad.md").write_bytes(b"bad")

        with pytest.raises(RuntimeError, match="partially"):
            await ws.checkpoint()

        # The healthy file is committed (per-file atomic state); the
        # failed one stays dirty for the next commit.
        assert await content_backend.get_content_file("output/ok.md") == b"ok"
        manifest = await backend.get_manifest(RUN_ID)
        assert manifest is not None
        assert [e.path for e in manifest.artifacts] == ["output/ok.md"]
        assert "output/bad.md" in ws._dirty_tracker.get_dirty()

    async def test_manifest_write_failure_leaves_everything_dirty(
        self, workspace_root: Path, backend: FsspecSyncBackend, content_backend: FsspecSyncBackend
    ) -> None:
        failing = _FailingManifestBackend(backend)
        ws = _make_workspace(workspace_root, failing, content_backend)
        ws.start_tracking()
        (workspace_root / "output" / "a.md").write_bytes(b"a")

        with pytest.raises(RuntimeError, match="injected manifest write failure"):
            await ws.checkpoint()

        # Per-file state is atomic (bytes landed), but the commit point
        # never fired: the manifest is unchanged and everything stays
        # dirty for the next commit.
        assert await backend.get_manifest(RUN_ID) is None
        assert await content_backend.get_content_file("output/a.md") == b"a"
        assert "output/a.md" in ws._dirty_tracker.get_dirty()

    async def test_version_conflict_propagates_and_stays_dirty(
        self, workspace_root: Path, backend: FsspecSyncBackend, content_backend: FsspecSyncBackend
    ) -> None:
        # The workspace holds a stale manifest view (version 1) while the
        # backend already advanced to version 5 — a concurrent writer.
        ws = _make_workspace(
            workspace_root,
            backend,
            content_backend,
            manifest=Manifest(
                run_id=RUN_ID,
                version=1,
                artifacts=[ManifestEntry(path="output/old.md", sha256=_sha(b"old"), size=3)],
            ),
        )
        await backend.put_manifest(RUN_ID, Manifest(run_id=RUN_ID, version=5))
        ws.start_tracking()
        (workspace_root / "output" / "a.md").write_bytes(b"a")

        with pytest.raises(ConcurrentModificationError):
            await ws.checkpoint()

        # The lost race leaves the dirty set intact.
        assert "output/a.md" in ws._dirty_tracker.get_dirty()
        manifest = await backend.get_manifest(RUN_ID)
        assert manifest is not None
        assert manifest.version == 5


# ---------------------------------------------------------------------------
# Recovery — manifest + content tree re-materialized
# ---------------------------------------------------------------------------


class TestRecovery:
    async def test_fresh_workspace_rematerializes_committed_state(
        self, tmp_path: Path, backend: FsspecSyncBackend, content_backend: FsspecSyncBackend
    ) -> None:
        root1 = tmp_path / "ws1"
        for subdir in ("input", "working", "output", ".workspace"):
            (root1 / subdir).mkdir(parents=True)
        ws = _make_workspace(root1, backend, content_backend)
        ws.start_tracking()
        (root1 / "output" / "a.md").write_bytes(b"keep")
        (root1 / "output" / "drop.md").write_bytes(b"drop")
        await ws.checkpoint()
        (root1 / "output" / "drop.md").unlink()
        await ws.checkpoint()

        # A fresh process opens the run: manifest loads, content tree
        # re-materializes (the tombstoned file must not resurrect).
        root2 = tmp_path / "ws2"
        for subdir in ("input", "working", "output", ".workspace"):
            (root2 / subdir).mkdir(parents=True)
        manifest = await backend.get_manifest(RUN_ID)
        ws2 = _make_workspace(root2, backend, content_backend, manifest=manifest)
        fetched = await ws2.materialize_content()

        assert fetched == 1
        assert (root2 / "output" / "a.md").read_bytes() == b"keep"
        assert not (root2 / "output" / "drop.md").exists()
        assert ws2.manifest is not None
        assert ws2.manifest.version == 2

    async def test_manifest_artifacts_survive_reconcile(
        self,
        tmp_path: Path,
        backend: FsspecSyncBackend,
        content_backend: FsspecSyncBackend,
    ) -> None:
        """Artifacts listed in the manifest are protected at materialize.

        A mid-commit crash may have left engine-owned files in the
        workspace but not yet in the content tree — the manifest
        protection set keeps reconcile from deleting them.
        """
        root = tmp_path / "ws"
        for subdir in ("input", "working", "output", ".workspace"):
            (root / subdir).mkdir(parents=True)
        manifest = Manifest(
            run_id=RUN_ID,
            version=1,
            artifacts=[ManifestEntry(path="output/pending.md", sha256="a" * 64, size=1)],
        )
        ws = _make_workspace(root, backend, content_backend, manifest=manifest)
        (root / "output" / "pending.md").write_bytes(b"not yet uploaded")

        await ws.materialize_content(reconcile=True)

        assert (root / "output" / "pending.md").read_bytes() == b"not yet uploaded"


# ---------------------------------------------------------------------------
# Dirty tracker ack contract
# ---------------------------------------------------------------------------


class TestDirtyAckContract:
    async def test_commit_acks_only_committed_and_deleted_paths(
        self, workspace_root: Path, backend: FsspecSyncBackend, content_backend: FsspecSyncBackend
    ) -> None:
        tracker = DirtyTracker(workspace_root=workspace_root)
        ws = Workspace(
            run_id=RUN_ID,
            root=workspace_root,
            backend=backend,
            content_backend=content_backend,
            dirty_tracker=tracker,
        )
        ws.start_tracking()
        (workspace_root / "output" / "a.md").write_bytes(b"a")
        (workspace_root / "output" / "b.md").write_bytes(b"b")
        await ws.checkpoint()

        (workspace_root / "output" / "b.md").unlink()
        (workspace_root / "output" / "c.md").write_bytes(b"c")
        await ws.checkpoint()

        assert tracker.get_dirty() == {}
        assert tracker.deleted_files == set()

        manifest = await backend.get_manifest(RUN_ID)
        assert manifest is not None
        assert [e.path for e in manifest.artifacts] == ["output/a.md", "output/c.md"]
