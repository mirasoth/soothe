"""Checkpoint lifecycle management — snapshot + delta, compaction, recovery.

First checkpoint of a run is a SNAPSHOT (full manifest + dirty set).
Subsequent checkpoints are DELTAs (only changed files, referencing parent).
Periodic compaction collapses the delta chain into a fresh SNAPSHOT when
delta count or cumulative dirty-file count exceeds thresholds.  Recovery
anchors on the latest SNAPSHOT and replays DELTAs forward.

Persistence follows "object store durable, state DB staging" (option A):

1. Blob contents are streamed to the backend synchronously (idempotent,
   content-addressed) with bounded per-file concurrency.
2. The serialized payload is inserted into the state store as a
   ``pending_upload`` row **before** any remote checkpoint write —
   local durability first; a crash between insert and remote push
   leaves a pending row the background uploader drains at startup.
3. The manifest is written to the remote backend (authoritative) with
   an optimistic-concurrency guard.
4. Only after the whole sequence succeeds are the captured paths
   acknowledged in the dirty tracker (see F-C2: a full ``clear()``
   would drop writes that arrived mid-checkpoint).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from soothe.workspace.sync.workspace_sync import (
    CheckpointPayload,
    CheckpointType,
    Manifest,
    ManifestEntry,
)

if TYPE_CHECKING:
    from pathlib import Path

    from soothe.workspace.state.protocol import WorkspaceStateStore
    from soothe.workspace.sync.cas import CASCache
    from soothe.workspace.sync.dirty_tracker import DirtyTracker
    from soothe.workspace.sync.workspace_sync import WorkspaceSyncBackend

logger = logging.getLogger(__name__)

_DEFAULT_MAX_DELTAS = 10
_DEFAULT_MAX_CUMULATIVE_DIRTY = 500
_DEFAULT_MAX_CONCURRENT_FILES = 8

_SEQ_RE = re.compile(r"^c(\d{6})$")


def _checkpoint_seq(checkpoint_id: str) -> int | None:
    """Parse the numeric sequence out of a checkpoint ID.

    Args:
        checkpoint_id: Short (``c000001``) or full (``run-1-c000001``)
            checkpoint ID.

    Returns:
        The numeric sequence, or ``None`` for non-conforming IDs.
    """
    seq = checkpoint_id.rsplit("-", 1)[-1]
    match = _SEQ_RE.match(seq)
    return int(match.group(1)) if match else None


class CheckpointManager:
    """Manages the checkpoint lifecycle for a single workspace run.

    Creates snapshots and deltas, serializes them to `CheckpointPayload`
    JSON, and stores them via the `WorkspaceSyncBackend`.  Also handles
    compaction and crash recovery.

    Args:
        run_id: Unique run identifier.
        backend: Storage backend for remote persistence.
        cas: Local content-addressed storage cache.
        dirty_tracker: Dirty file tracker (source of dirty file paths).
        workspace_root: Local workspace root path.
        state_store: Optional workspace state store; when present the
            serialized payload is staged as a ``pending_upload`` row
            before any remote write so crashes are recoverable via the
            background uploader.
        content_backend: Optional second backend addressing a plain file
            tree (the "content root"). When set, every checkpoint also
            MIRRORS dirty file contents to `<relpath>` on the content
            backend and propagates deletions, so the content tree always
            reflects the latest workspace state without requiring
            manifest/checkpoint parsing to read.
        max_deltas: Compaction threshold — max delta chain length.
        max_cumulative_dirty: Compaction threshold — max cumulative
            dirty-file entries across the delta chain.
        max_concurrent_files: Per-checkpoint concurrency bound for
            hashing / CAS caching / blob uploads.
    """

    def __init__(
        self,
        *,
        run_id: str,
        backend: WorkspaceSyncBackend,
        cas: CASCache,
        dirty_tracker: DirtyTracker,
        workspace_root: Path,
        state_store: WorkspaceStateStore | None = None,
        content_backend: WorkspaceSyncBackend | None = None,
        max_deltas: int = _DEFAULT_MAX_DELTAS,
        max_cumulative_dirty: int = _DEFAULT_MAX_CUMULATIVE_DIRTY,
        max_concurrent_files: int = _DEFAULT_MAX_CONCURRENT_FILES,
    ) -> None:
        """Initialize the checkpoint manager with backend, CAS, and dirty tracker."""
        self._run_id = run_id
        self._backend = backend
        self._cas = cas
        self._dirty_tracker = dirty_tracker
        self._workspace_root = workspace_root
        self._state_store = state_store
        self._content_backend = content_backend
        self._max_deltas = max_deltas
        self._max_cumulative_dirty = max_cumulative_dirty
        self._max_concurrent_files = max_concurrent_files
        self._checkpoint_seq = 0
        self._last_checkpoint_id: str | None = None
        self._last_snapshot_id: str | None = None
        self._delta_count = 0
        self._cumulative_dirty_count = 0
        self._manifest: Manifest | None = None

    @property
    def manifest(self) -> Manifest | None:
        """Return the current manifest (or `None` before first checkpoint)."""
        return self._manifest

    @property
    def last_checkpoint_id(self) -> str | None:
        """Return the ID of the most recent checkpoint."""
        return self._last_checkpoint_id

    async def initialize(self) -> None:
        """Load the existing manifest and counters from the backend.

        Called at workspace open so a fresh process never resets the
        manifest to ``version=1`` (F-L2).  The remote manifest is the
        authoritative artifact state; checkpoint counters derive from
        the stored checkpoint IDs.
        """
        self._manifest = await self._backend.get_manifest(self._run_id)
        checkpoints = await self._backend.list_checkpoints(self._run_id)
        seq_candidates = [
            seq for seq in (_checkpoint_seq(c) for c in checkpoints) if seq is not None
        ]
        if self._state_store is not None:
            # Pending rows staged by a crashed process are not on the
            # remote yet — their sequence numbers must count toward
            # the next ID or a new checkpoint would silently overwrite
            # the pending row (and its payload) under the same key.
            try:
                pending = await self._state_store.list_pending_checkpoints()
                seq_candidates.extend(
                    seq
                    for seq in (_checkpoint_seq(r["checkpoint_id"]) for r in pending)
                    if seq is not None
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to merge pending checkpoint rows during initialize (run %s)",
                    self._run_id,
                    exc_info=True,
                )
        # Sequence derives from remote ∪ pending rows — pending rows
        # from a crashed process are invisible to the remote listing.
        self._checkpoint_seq = max(seq_candidates, default=0)
        if checkpoints:
            self._last_checkpoint_id = checkpoints[-1]
            for ckpt_id in reversed(checkpoints):
                data = await self._backend.get_checkpoint(ckpt_id)
                if data is None:
                    continue
                payload = CheckpointPayload.model_validate_json(data)
                if payload.is_snapshot():
                    self._last_snapshot_id = ckpt_id
                    break
            if self._last_snapshot_id is not None:
                snapshot_seq = _checkpoint_seq(self._last_snapshot_id) or 0
                self._delta_count = max(0, self._checkpoint_seq - snapshot_seq)
            else:
                self._delta_count = len(checkpoints)
        logger.info(
            "Recovered run %s: manifest_version=%s, %d checkpoints, last_snapshot=%s",
            self._run_id,
            self._manifest.version if self._manifest else None,
            len(checkpoints),
            self._last_snapshot_id,
        )

    async def create_checkpoint(self) -> str | None:
        """Create a checkpoint from the current dirty state.

        Decides whether to write a SNAPSHOT or DELTA based on the
        compaction thresholds.  Uploads dirty-file blobs to the backend
        (streamed, bounded concurrency), stages the serialized payload
        in the state store as ``pending_upload``, updates the remote
        manifest, and acknowledges exactly the captured paths in the
        dirty tracker.  Per-file failures produce a partial checkpoint
        (failed paths stay dirty for the next one) and a final
        `RuntimeError`.

        Returns:
            The full checkpoint ID (e.g. ``"run-123-c000001"``), or
            `None` when nothing was dirty or deleted.
        """
        dirty = self._dirty_tracker.get_dirty()
        deleted = set(self._dirty_tracker.deleted_files)

        # Dirty paths that vanished between scan and checkpoint are
        # deletions, not writes.
        for rel_path in dirty:
            if not (self._workspace_root / rel_path).exists():
                deleted.add(rel_path)
        dirty = {p: e for p, e in dirty.items() if p not in deleted}

        if not dirty and not deleted:
            logger.debug("No dirty or deleted files — skipping checkpoint")
            return None

        semaphore = asyncio.Semaphore(self._max_concurrent_files)
        dirty_entries: list[ManifestEntry] = []
        failures: list[tuple[str, BaseException]] = []

        async def _capture(rel_path: str) -> None:
            async with semaphore:
                try:
                    entry = await self._capture_file(rel_path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Failed to capture %s for checkpoint (run %s); "
                        "it stays dirty for the next checkpoint",
                        rel_path,
                        self._run_id,
                        exc_info=True,
                    )
                    failures.append((rel_path, exc))
                    return
                dirty_entries.append(entry)
                await self._mirror_to_content_tree(rel_path)

        await asyncio.gather(*(_capture(rel_path) for rel_path in sorted(dirty)))

        # Mirror deletions to the content tree (also covers dirty paths
        # that vanished between scan and checkpoint).
        if self._content_backend is not None:
            for d in sorted(deleted):
                try:
                    await self._content_backend.delete_content_file(d)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Content mirror delete failed for %s (run %s)",
                        d,
                        self._run_id,
                        exc_info=True,
                    )

        self._manifest = self._advance_manifest(dirty_entries, deleted)

        self._checkpoint_seq += 1
        short_id = f"c{self._checkpoint_seq:06d}"
        full_id = f"{self._run_id}-{short_id}"
        is_compaction = (
            self._delta_count >= self._max_deltas
            or self._cumulative_dirty_count >= self._max_cumulative_dirty
        )

        if self._last_checkpoint_id is None or is_compaction:
            payload = CheckpointPayload(
                checkpoint_id=full_id,
                kind=CheckpointType.SNAPSHOT,
                manifest_version=self._manifest.version,
                dirty_files=sorted(dirty_entries, key=lambda e: e.path),
                deleted_files=sorted(deleted),
                manifest_snapshot=self._manifest,
                parent_checkpoint_id=None,
            )
            self._last_snapshot_id = full_id
            self._delta_count = 0
            self._cumulative_dirty_count = 0
            logger.info("Created SNAPSHOT checkpoint %s for run %s", full_id, self._run_id)
        else:
            payload = CheckpointPayload(
                checkpoint_id=full_id,
                kind=CheckpointType.DELTA,
                manifest_version=self._manifest.version,
                dirty_files=sorted(dirty_entries, key=lambda e: e.path),
                deleted_files=sorted(deleted),
                manifest_snapshot=None,
                parent_checkpoint_id=self._last_checkpoint_id,
            )
            self._delta_count += 1
            self._cumulative_dirty_count += len(dirty_entries)
            logger.info("Created DELTA checkpoint %s for run %s", full_id, self._run_id)

        data = payload.model_dump_json().encode("utf-8")

        if self._state_store is not None:
            # Local durability first (option A): a crash past this point
            # leaves a pending row the background uploader drains.
            await self._state_store.insert_checkpoint(
                full_id,
                manifest_hash=None,
                status="pending_upload",
                data=data,
            )
        else:
            # No state store → no staging row and no uploader to drain
            # it; push the payload synchronously so it is durable.
            await self._backend.put_checkpoint(full_id, data)

        # Payload push is asynchronous (background uploader, F-L3);
        # the manifest is authoritative for artifact state, so it is
        # written synchronously with an optimistic-concurrency guard.
        await self._backend.put_manifest(
            self._run_id,
            self._manifest,
            if_match=str(self._manifest.version - 1) if self._manifest.version > 1 else None,
        )

        self._last_checkpoint_id = full_id

        # Acknowledge ONLY the paths captured by this checkpoint —
        # writes that arrived mid-checkpoint stay dirty (F-C2).
        self._dirty_tracker.clear(acked_paths=[e.path for e in dirty_entries] + sorted(deleted))

        if failures:
            failed_paths = ", ".join(p for p, _ in failures)
            raise RuntimeError(
                f"checkpoint {full_id} completed partially: {len(failures)} file(s) "
                f"failed to capture ({failed_paths}); they remain dirty"
            )

        return full_id

    async def _capture_file(self, rel_path: str) -> ManifestEntry:
        """Hash, cache, and upload one dirty file.

        Args:
            rel_path: Workspace-relative path of the file.

        Returns:
            The manifest entry for the captured file.

        Raises:
            Exception: When the file cannot be captured (caller keeps
                it dirty and records the failure).
        """
        abs_path = self._workspace_root / rel_path
        if not abs_path.exists():
            raise FileNotFoundError(rel_path)
        if abs_path.is_symlink():
            target = abs_path.resolve()
            try:
                target.relative_to(self._workspace_root.resolve())
            except ValueError:
                raise ValueError(f"escaping symlink not checkpointed: {rel_path}") from None

        # Single streaming read: hash while copying into the local CAS
        # cache, then (on remote miss) stream-upload from the immutable
        # cached blob.
        sha256, cas_path = await asyncio.to_thread(self._cas.cache_file, abs_path)
        size = abs_path.stat().st_size

        if not await self._backend.head_blob(sha256):
            from soothe.workspace.sync.streaming import put_blob_from_file

            await put_blob_from_file(self._backend, sha256, cas_path)

        return ManifestEntry(path=rel_path, sha256=sha256, size=size)

    async def _mirror_to_content_tree(self, rel_path: str) -> None:
        """Mirror one captured file to the content tree (best-effort).

        Args:
            rel_path: Workspace-relative path of the file.
        """
        if self._content_backend is None:
            return
        try:
            from soothe.workspace.sync.streaming import put_content_file_from_file

            await put_content_file_from_file(
                self._content_backend, rel_path, self._workspace_root / rel_path
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Content mirror put failed for %s (run %s)",
                rel_path,
                self._run_id,
                exc_info=True,
            )

    def _advance_manifest(
        self,
        dirty_entries: list[ManifestEntry],
        deleted: set[str],
    ) -> Manifest:
        """Fold captured entries and tombstones into the manifest.

        Args:
            dirty_entries: Captured (successfully uploaded) entries.
            deleted: Tombstoned relative paths.

        Returns:
            The next manifest (version incremented).
        """
        if self._manifest is None:
            return Manifest(
                run_id=self._run_id,
                version=1,
                resources=[],
                artifacts=sorted(dirty_entries, key=lambda e: e.path),
            )
        artifacts = {e.path: e for e in self._manifest.artifacts}
        for entry in dirty_entries:
            artifacts[entry.path] = entry
        for d in deleted:
            artifacts.pop(d, None)
        return Manifest(
            run_id=self._run_id,
            version=self._manifest.version + 1,
            resources=self._manifest.resources,
            artifacts=sorted(artifacts.values(), key=lambda e: e.path),
        )

    async def recover(self) -> CheckpointPayload | None:
        """Recover workspace state from the latest replayable checkpoint.

        1. List checkpoints, find the latest SNAPSHOT.
        2. Deserialize and replay subsequent DELTAs (including
           tombstones — deletions must not resurrect).
        3. Return the final APPLIED checkpoint payload.

        The remote manifest is the authoritative artifact state; the
        replayed snapshot manifest is the fallback when the remote
        manifest is missing.

        A missing or unreadable delta stops replay early. The manager
        state and the returned payload then describe the last
        checkpoint that WAS applied — never the newest stored one —
        and the payload carries ``incomplete=True`` so callers can
        treat the dirty state as partial instead of silently
        continuing from a state that never existed.

        Returns:
            The recovered checkpoint payload, or `None` if no
            checkpoints exist.
        """
        checkpoints = await self._backend.list_checkpoints(self._run_id)
        if not checkpoints:
            return None

        snapshot_data: bytes | None = None
        snapshot_id: str | None = None
        snapshot_idx = 0
        for i, ckpt_id in enumerate(reversed(checkpoints)):
            data = await self._backend.get_checkpoint(ckpt_id)
            if data is None:
                continue
            payload = CheckpointPayload.model_validate_json(data)
            if payload.is_snapshot():
                snapshot_data = data
                snapshot_id = ckpt_id
                snapshot_idx = len(checkpoints) - 1 - i
                break

        if snapshot_data is None or snapshot_id is None:
            logger.warning("No snapshot found for run %s; cannot recover", self._run_id)
            return None

        snapshot_payload = CheckpointPayload.model_validate_json(snapshot_data)
        logger.info("Recovery anchored on snapshot %s", snapshot_id)

        # Remote manifest wins (option A); snapshot manifest is the
        # fallback for pre-manifest runs.
        manifest = await self._backend.get_manifest(self._run_id)
        if manifest is None:
            manifest = snapshot_payload.manifest_snapshot

        dirty_files = list(snapshot_payload.dirty_files)
        deleted_paths: set[str] = set(snapshot_payload.deleted_files)

        applied_delta_ids: list[str] = []
        incomplete = False
        for ckpt_id in checkpoints[snapshot_idx + 1 :]:
            data = await self._backend.get_checkpoint(ckpt_id)
            if data is None:
                incomplete = True
                logger.error(
                    "Missing delta %s during recovery of run %s; replay stopped "
                    "at the last applied checkpoint (recovered state is partial)",
                    ckpt_id,
                    self._run_id,
                )
                break
            delta = CheckpointPayload.model_validate_json(data)
            applied_delta_ids.append(ckpt_id)
            dirty_paths = {e.path for e in delta.dirty_files}
            dirty_files = [e for e in dirty_files if e.path not in dirty_paths]
            dirty_files.extend(delta.dirty_files)
            deleted_paths.update(delta.deleted_files)
            deleted_paths.difference_update(dirty_paths)

        # Tombstones prune both the dirty set and the manifest.
        if deleted_paths:
            dirty_files = [e for e in dirty_files if e.path not in deleted_paths]
            if manifest is not None:
                manifest = manifest.model_copy(
                    update={
                        "artifacts": [e for e in manifest.artifacts if e.path not in deleted_paths]
                    }
                )

        last_applied_id = applied_delta_ids[-1] if applied_delta_ids else snapshot_id
        if manifest is not None:
            # Counters track APPLIED checkpoints only: numbering the next
            # checkpoint as if recovery completed would let later uploads
            # treat stale local state as current.
            self._manifest = manifest
            max_applied_seq = max(
                (
                    seq
                    for seq in (_checkpoint_seq(c) for c in [snapshot_id, *applied_delta_ids])
                    if seq is not None
                ),
                default=0,
            )
            self._checkpoint_seq = max_applied_seq
            self._last_checkpoint_id = last_applied_id
            self._last_snapshot_id = snapshot_id
            self._delta_count = len(applied_delta_ids)

        return CheckpointPayload(
            checkpoint_id=last_applied_id,
            kind=CheckpointType.SNAPSHOT,
            manifest_version=manifest.version if manifest else 0,
            dirty_files=sorted(dirty_files, key=lambda e: e.path),
            deleted_files=sorted(deleted_paths),
            manifest_snapshot=manifest,
            parent_checkpoint_id=None,
            incomplete=incomplete,
        )
