"""Tests for the checkpoint manager — snapshot/delta, compaction, recovery.

Tests verify:
    - First checkpoint is a SNAPSHOT.
    - Subsequent checkpoints are DELTAs.
    - Compaction triggers a new SNAPSHOT after max_deltas.
    - Recovery anchors on the latest snapshot and replays deltas.
    - Dirty file blobs are uploaded to the backend.
    - Manifest is updated with correct version.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fsspec.implementations.memory import MemoryFileSystem

from soothe.workspace.sync.backends.fsspec import FsspecSyncBackend
from soothe.workspace.sync.cas import CASCache
from soothe.workspace.sync.checkpoint import CheckpointManager
from soothe.workspace.sync.dirty_tracker import DirtyTracker
from soothe.workspace.sync.workspace_sync import (
    CheckpointType,
)

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ROOT = "/test-checkpoint"


@pytest.fixture
def fs() -> MemoryFileSystem:
    """Fresh in-memory filesystem."""
    mem = MemoryFileSystem()
    yield mem
    try:
        mem.rm(ROOT, recursive=True)
    except FileNotFoundError:
        pass
    mem.store.clear()


@pytest.fixture
def backend(fs: MemoryFileSystem) -> FsspecSyncBackend:
    """Fresh backend instance."""
    b = FsspecSyncBackend(fs=fs, root=ROOT, max_workers=2)
    yield b
    b.close()


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    """Temporary workspace with input/working/output dirs."""
    d = tmp_path / "workspace"
    for subdir in ("input", "working", "output"):
        (d / subdir).mkdir(parents=True)
    return d


@pytest.fixture
def cas(tmp_path: Path) -> CASCache:
    """Fresh CAS cache."""
    return CASCache(cache_root=tmp_path / "cas")


@pytest.fixture
def tracker(workspace_dir: Path) -> DirtyTracker:
    """Fresh dirty tracker (not started — used for manual mark_dirty)."""
    return DirtyTracker(workspace_root=workspace_dir)


@pytest.fixture
def manager(
    backend: FsspecSyncBackend,
    cas: CASCache,
    tracker: DirtyTracker,
    workspace_dir: Path,
) -> CheckpointManager:
    """Fresh checkpoint manager."""
    return CheckpointManager(
        run_id="run-123",
        backend=backend,
        cas=cas,
        dirty_tracker=tracker,
        workspace_root=workspace_dir,
        max_deltas=3,
        max_cumulative_dirty=100,
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Checkpoint creation
# ---------------------------------------------------------------------------


class TestCheckpointCreation:
    """Tests for snapshot/delta checkpoint creation."""

    @pytest.mark.asyncio
    async def test_first_checkpoint_is_snapshot(
        self, manager: CheckpointManager, workspace_dir: Path, backend: FsspecSyncBackend
    ) -> None:
        """First checkpoint is a SNAPSHOT with full manifest."""
        f = workspace_dir / "output" / "report.md"
        f.write_text("# Report")
        tracker = manager._dirty_tracker
        tracker.mark_dirty("output/report.md")

        ckpt_id = await manager.create_checkpoint()
        assert ckpt_id == "run-123-c000001"

        # Verify the stored payload is a SNAPSHOT.
        data = await backend.get_checkpoint("run-123-c000001")
        assert data is not None
        from soothe.workspace.sync.workspace_sync import CheckpointPayload

        payload = CheckpointPayload.model_validate_json(data)
        assert payload.kind == CheckpointType.SNAPSHOT
        assert payload.manifest_snapshot is not None
        assert payload.parent_checkpoint_id is None
        assert len(payload.dirty_files) == 1
        assert payload.dirty_files[0].path == "output/report.md"

    @pytest.mark.asyncio
    async def test_second_checkpoint_is_delta(
        self, manager: CheckpointManager, workspace_dir: Path, backend: FsspecSyncBackend
    ) -> None:
        """Second checkpoint is a DELTA referencing the first."""
        # First checkpoint.
        f1 = workspace_dir / "output" / "a.md"
        f1.write_text("A")
        manager._dirty_tracker.mark_dirty("output/a.md")
        await manager.create_checkpoint()

        # Second checkpoint.
        f2 = workspace_dir / "output" / "b.md"
        f2.write_text("B")
        manager._dirty_tracker.mark_dirty("output/b.md")
        ckpt_id = await manager.create_checkpoint()
        assert ckpt_id == "run-123-c000002"

        data = await backend.get_checkpoint("run-123-c000002")
        assert data is not None
        from soothe.workspace.sync.workspace_sync import CheckpointPayload

        payload = CheckpointPayload.model_validate_json(data)
        assert payload.kind == CheckpointType.DELTA
        assert payload.manifest_snapshot is None
        assert payload.parent_checkpoint_id == "run-123-c000001"
        assert len(payload.dirty_files) == 1
        assert payload.dirty_files[0].path == "output/b.md"

    @pytest.mark.asyncio
    async def test_compaction_creates_snapshot(
        self, manager: CheckpointManager, workspace_dir: Path, backend: FsspecSyncBackend
    ) -> None:
        """After max_deltas (3), a compaction SNAPSHOT is written."""
        # Create 4 checkpoints (c001=SNAPSHOT, c002-c004=DELTA, c005=SNAPSHOT).
        for i in range(5):
            f = workspace_dir / "output" / f"file{i}.md"
            f.write_text(f"content{i}")
            manager._dirty_tracker.mark_dirty(f"output/file{i}.md")
            await manager.create_checkpoint()

        # c000005 should be a SNAPSHOT (compaction after 3 deltas).
        from soothe.workspace.sync.workspace_sync import CheckpointPayload

        data = await backend.get_checkpoint("run-123-c000005")
        assert data is not None
        payload = CheckpointPayload.model_validate_json(data)
        assert payload.kind == CheckpointType.SNAPSHOT
        assert payload.manifest_snapshot is not None

    @pytest.mark.asyncio
    async def test_dirty_blobs_uploaded(
        self, manager: CheckpointManager, workspace_dir: Path, backend: FsspecSyncBackend
    ) -> None:
        """Dirty file content is uploaded as a blob to the backend."""
        data = b"checkpoint me"
        f = workspace_dir / "output" / "data.bin"
        f.write_bytes(data)
        manager._dirty_tracker.mark_dirty("output/data.bin")

        await manager.create_checkpoint()

        h = _sha256(data)
        assert await backend.head_blob(h) is True

    @pytest.mark.asyncio
    async def test_manifest_version_increments(
        self, manager: CheckpointManager, workspace_dir: Path
    ) -> None:
        """Manifest version increments with each checkpoint."""
        f = workspace_dir / "output" / "a.md"
        f.write_text("A")
        manager._dirty_tracker.mark_dirty("output/a.md")
        await manager.create_checkpoint()
        assert manager.manifest is not None
        assert manager.manifest.version == 1

        f.write_text("AA")
        manager._dirty_tracker.mark_dirty("output/a.md")
        await manager.create_checkpoint()
        assert manager.manifest.version == 2

    @pytest.mark.asyncio
    async def test_deleted_files_removed_from_manifest(
        self, manager: CheckpointManager, workspace_dir: Path, backend: FsspecSyncBackend
    ) -> None:
        """Deleted files are removed from the manifest."""
        f = workspace_dir / "output" / "temp.md"
        f.write_text("temp")
        manager._dirty_tracker.mark_dirty("output/temp.md")
        await manager.create_checkpoint()
        assert manager.manifest is not None
        assert any(e.path == "output/temp.md" for e in manager.manifest.artifacts)

        # Delete the file.
        f.unlink()
        manager._dirty_tracker.mark_dirty("output/temp.md")
        await manager.create_checkpoint()
        assert not any(e.path == "output/temp.md" for e in manager.manifest.artifacts)

    @pytest.mark.asyncio
    async def test_dirty_cleared_after_checkpoint(
        self, manager: CheckpointManager, workspace_dir: Path
    ) -> None:
        """Dirty tracker is cleared after a checkpoint."""
        f = workspace_dir / "output" / "a.md"
        f.write_text("A")
        manager._dirty_tracker.mark_dirty("output/a.md")
        assert len(manager._dirty_tracker.dirty_files) == 1

        await manager.create_checkpoint()
        assert len(manager._dirty_tracker.dirty_files) == 0


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


class TestCheckpointRecovery:
    """Tests for crash recovery via snapshot + delta replay."""

    @pytest.mark.asyncio
    async def test_recover_from_snapshot_and_deltas(
        self, manager: CheckpointManager, workspace_dir: Path, backend: FsspecSyncBackend
    ) -> None:
        """Recovery replays snapshot + deltas to restore state."""
        # Create a snapshot + 2 deltas.
        for i in range(3):
            f = workspace_dir / "output" / f"file{i}.md"
            f.write_text(f"content{i}")
            manager._dirty_tracker.mark_dirty(f"output/file{i}.md")
            await manager.create_checkpoint()

        # Simulate crash: create a new manager (no in-memory state).
        manager2 = CheckpointManager(
            run_id="run-123",
            backend=backend,
            cas=manager._cas,
            dirty_tracker=DirtyTracker(workspace_root=workspace_dir),
            workspace_root=workspace_dir,
        )
        await manager2.initialize()

        # The new manager should have recovered the manifest.
        assert manager2.manifest is not None
        assert manager2.manifest.version == 3
        assert len(manager2.manifest.artifacts) == 3

        # Recover the latest state.
        payload = await manager2.recover()
        assert payload is not None
        assert len(payload.dirty_files) == 3

    @pytest.mark.asyncio
    async def test_recover_empty_returns_none(
        self, manager: CheckpointManager, backend: FsspecSyncBackend
    ) -> None:
        """Recovery with no checkpoints returns None."""
        payload = await manager.recover()
        assert payload is None

    @pytest.mark.asyncio
    async def test_recover_handles_missing_delta(
        self,
        manager: CheckpointManager,
        workspace_dir: Path,
        backend: FsspecSyncBackend,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Recovery stops gracefully when a delta is missing.

        The payload is marked `incomplete` and the manager counters track
        the last APPLIED checkpoint, not the newest stored one — a partial
        recovery must never look complete (review f994682f).
        """
        # Create snapshot + delta.
        for i in range(2):
            f = workspace_dir / "output" / f"file{i}.md"
            f.write_text(f"content{i}")
            manager._dirty_tracker.mark_dirty(f"output/file{i}.md")
            await manager.create_checkpoint()

        # Capture the listing, then delete the delta object while the
        # listing still reports it (index/listing desync — the shape that
        # actually triggers the missing-delta branch, since removing the
        # object also removes it from the listing).
        listed = await backend.list_checkpoints("run-123")
        assert len(listed) == 2
        await manager._backend._to_thread(
            backend._fs.rm, f"{ROOT}/runs/run-123/checkpoints/run-123-c000002.json"
        )

        async def _list_with_ghost(run_id: str) -> list[str]:
            if run_id == "run-123":
                return listed
            return await FsspecSyncBackend.list_checkpoints(backend, run_id)  # type: ignore[arg-type]

        monkeypatch.setattr(backend, "list_checkpoints", _list_with_ghost)

        # Recovery should still work, anchored on the snapshot.
        payload = await manager.recover()
        assert payload is not None
        assert payload.incomplete is True
        # Only the snapshot's dirty files are recovered.
        assert len(payload.dirty_files) == 1
        # Counters and checkpoint_id describe the last APPLIED state.
        assert manager._checkpoint_seq == 1
        assert manager._delta_count == 0
        assert payload.checkpoint_id == "run-123-c000001"


# ---------------------------------------------------------------------------
# Regression: mid-checkpoint write loss (F-C2) and option-A lifecycle
# ---------------------------------------------------------------------------


class _FakeStateStore:
    """Minimal WorkspaceStateStore for pending-row lifecycle tests."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def insert_checkpoint(self, checkpoint_id, *, manifest_hash, status, data=None):
        self.rows[checkpoint_id] = {
            "checkpoint_id": checkpoint_id,
            "manifest_hash": manifest_hash,
            "status": status,
            "data": data,
        }

    async def list_pending_checkpoints(self):
        return [r for r in self.rows.values() if r["status"] == "pending_upload"]

    async def update_checkpoint_status(self, checkpoint_id, status):
        if checkpoint_id in self.rows:
            self.rows[checkpoint_id]["status"] = status

    async def mark_checkpoint_uploaded(self, checkpoint_id):
        if checkpoint_id in self.rows:
            self.rows[checkpoint_id]["status"] = "uploaded"
            self.rows[checkpoint_id]["data"] = None


class TestMidCheckpointWrites:
    """F-C2 regression: writes arriving during the checkpoint survive."""

    @pytest.mark.asyncio
    async def test_write_during_checkpoint_stays_dirty(
        self,
        manager: CheckpointManager,
        workspace_dir: Path,
        backend: FsspecSyncBackend,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A write landing mid-checkpoint is NOT acked away."""
        f1 = workspace_dir / "output" / "a.md"
        f1.write_text("A")
        tracker = manager._dirty_tracker
        tracker.mark_dirty("output/a.md")

        original_capture = manager._capture_file

        async def _capture_with_mid_write(rel_path: str):
            # Simulate the stat-scan poll firing mid-checkpoint: a new
            # file is marked dirty and the baseline advances past it.
            f2 = workspace_dir / "output" / "b.md"
            f2.write_text("B")
            tracker.mark_dirty("output/b.md")
            return await original_capture(rel_path)

        monkeypatch.setattr(manager, "_capture_file", _capture_with_mid_write)

        await manager.create_checkpoint()

        # b.md arrived during the checkpoint and must remain dirty.
        assert "output/b.md" in tracker.get_dirty()

        # The manifest of THIS checkpoint does not include b.md, but the
        # next one does.
        assert manager.manifest is not None
        assert not any(e.path == "output/b.md" for e in manager.manifest.artifacts)
        await manager.create_checkpoint()
        assert any(e.path == "output/b.md" for e in manager.manifest.artifacts)
        assert await backend.get_blob(_sha256(b"B")) is not None

    @pytest.mark.asyncio
    async def test_deletion_tombstone_replayed_on_recovery(
        self, workspace_dir: Path, backend: FsspecSyncBackend, tmp_path: Path
    ) -> None:
        """Deletions do not resurrect across crash recovery (F-C3)."""
        tracker = DirtyTracker(workspace_root=workspace_dir)
        manager = CheckpointManager(
            run_id="run-tomb",
            backend=backend,
            cas=CASCache(cache_root=tmp_path / "cas"),
            dirty_tracker=tracker,
            workspace_root=workspace_dir,
        )
        f = workspace_dir / "output" / "temp.md"
        f.write_text("temp")
        tracker.mark_dirty("output/temp.md")
        await manager.create_checkpoint()

        f.unlink()
        tracker.mark_dirty("output/temp.md")
        await manager.create_checkpoint()

        # Crash: fresh manager recovers state.
        tracker2 = DirtyTracker(workspace_root=workspace_dir)
        manager2 = CheckpointManager(
            run_id="run-tomb",
            backend=backend,
            cas=CASCache(cache_root=tmp_path / "cas"),
            dirty_tracker=tracker2,
            workspace_root=workspace_dir,
        )
        await manager2.initialize()
        payload = await manager2.recover()
        assert payload is not None
        assert "output/temp.md" not in {e.path for e in payload.dirty_files}
        assert manager2.manifest is not None
        assert not any(e.path == "output/temp.md" for e in manager2.manifest.artifacts)


class TestOptionALifecycle:
    """Option A: pending-row staging + async push + mark-uploaded."""

    @pytest.mark.asyncio
    async def test_pending_row_lifecycle(
        self, workspace_dir: Path, backend: FsspecSyncBackend, tmp_path: Path
    ) -> None:
        """With a state store the payload is staged, not remotely pushed."""
        store = _FakeStateStore()
        tracker = DirtyTracker(workspace_root=workspace_dir)
        manager = CheckpointManager(
            run_id="run-optA",
            backend=backend,
            cas=CASCache(cache_root=tmp_path / "cas"),
            dirty_tracker=tracker,
            workspace_root=workspace_dir,
            state_store=store,
        )
        f = workspace_dir / "output" / "a.md"
        f.write_text("A")
        tracker.mark_dirty("output/a.md")

        full_id = await manager.create_checkpoint()
        assert full_id == "run-optA-c000001"

        # Local durability: pending row holds the serialized payload.
        row = store.rows[full_id]
        assert row["status"] == "pending_upload"
        assert row["data"] is not None
        # Remote payload not pushed yet (uploader's job) — but the
        # manifest (authoritative for artifacts) IS.
        assert await backend.get_checkpoint(full_id) is None
        assert await backend.get_manifest("run-optA") is not None

        # The background uploader drains: push + mark uploaded.
        from soothe.workspace.sync.uploader import BackgroundUploader

        uploader = BackgroundUploader(
            backend=backend, store=store, run_id="run-optA", poll_interval=0.05
        )
        uploader.start()
        for _ in range(100):
            if store.rows[full_id]["status"] == "uploaded":
                break
            await asyncio.sleep(0.05)
        await uploader.stop()

        assert store.rows[full_id]["status"] == "uploaded"
        assert store.rows[full_id]["data"] is None
        assert await backend.get_checkpoint(full_id) is not None

    @pytest.mark.asyncio
    async def test_initialize_merges_pending_rows_into_seq(
        self, workspace_dir: Path, backend: FsspecSyncBackend, tmp_path: Path
    ) -> None:
        """A crashed process's pending row reserves its sequence number."""
        store = _FakeStateStore()
        tracker = DirtyTracker(workspace_root=workspace_dir)
        manager = CheckpointManager(
            run_id="run-seq",
            backend=backend,
            cas=CASCache(cache_root=tmp_path / "cas"),
            dirty_tracker=tracker,
            workspace_root=workspace_dir,
            state_store=store,
        )
        f = workspace_dir / "output" / "a.md"
        f.write_text("A")
        tracker.mark_dirty("output/a.md")
        await manager.create_checkpoint()  # pending row run-seq-c000001

        # Crash: new manager, same state store, no remote checkpoints.
        tracker2 = DirtyTracker(workspace_root=workspace_dir)
        manager2 = CheckpointManager(
            run_id="run-seq",
            backend=backend,
            cas=CASCache(cache_root=tmp_path / "cas"),
            dirty_tracker=tracker2,
            workspace_root=workspace_dir,
            state_store=store,
        )
        await manager2.initialize()

        f2 = workspace_dir / "output" / "b.md"
        f2.write_text("B")
        tracker2.mark_dirty("output/b.md")
        full_id = await manager2.create_checkpoint()
        # c000001 was reserved by the pending row — the new checkpoint
        # must not reuse (and overwrite) it.
        assert full_id == "run-seq-c000002"
        assert store.rows["run-seq-c000001"]["status"] == "pending_upload"
        assert store.rows["run-seq-c000001"]["data"] is not None
