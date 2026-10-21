"""Workspace handle — per-run lifecycle: open, materialize, checkpoint, publish, close.

The `Workspace` is the agent-facing handle to a single run's workspace.
It coordinates the CAS cache, dirty tracker, debouncer, checkpoint
manager, and background uploader to provide a simple async lifecycle API.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from soothe.workspace.sync.cas import CASCache
from soothe.workspace.sync.checkpoint import CheckpointManager
from soothe.workspace.sync.debouncer import CheckpointDebouncer
from soothe.workspace.sync.dirty_tracker import DirtyTracker
from soothe.workspace.sync.materializer import Materializer
from soothe.workspace.sync.uploader import BackgroundUploader
from soothe.workspace.sync.workspace_sync import (
    Artifact,
    ArtifactSpec,
    Manifest,
    Resource,
)

if TYPE_CHECKING:
    from soothe.workspace.state.protocol import WorkspaceStateStore
    from soothe.workspace.sync.workspace_sync import WorkspaceSyncBackend

logger = logging.getLogger(__name__)


class Workspace:
    """Per-run workspace handle.

    Coordinates materialization, dirty tracking, checkpointing, and
    publication for a single agent run.

    Args:
        run_id: Unique run identifier.
        root: Local filesystem root of the workspace.
        backend: Remote storage backend.
        cas: Local CAS cache.
        state_store: Workspace state database.
        dirty_tracker: Dirty file tracker.
        debouncer: Debounced checkpoint trigger.
        checkpoint_mgr: Checkpoint lifecycle manager.
        uploader: Background uploader.
        content_backend: Optional second backend addressing a plain file
            tree (the "content root", e.g. workspace materials + mirrored
            agent outputs). `materialize_content` pulls it into the local
            workspace at open; the checkpoint manager mirrors dirty writes
            and deletions back at checkpoint time.
    """

    def __init__(
        self,
        *,
        run_id: str,
        root: Path,
        backend: WorkspaceSyncBackend | None,
        cas: CASCache,
        state_store: WorkspaceStateStore | None,
        dirty_tracker: DirtyTracker,
        debouncer: CheckpointDebouncer | None,
        checkpoint_mgr: CheckpointManager | None,
        uploader: BackgroundUploader | None,
        content_backend: WorkspaceSyncBackend | None = None,
    ) -> None:
        """Initialize the workspace sync session with all sub-components."""
        self.run_id = run_id
        self.root = root
        self._backend = backend
        self._cas = cas
        self._state_store = state_store
        self._dirty_tracker = dirty_tracker
        self._debouncer = debouncer
        self._checkpoint_mgr = checkpoint_mgr
        self._uploader = uploader
        self._content_backend = content_backend
        self._closing = False
        self._manifest: Manifest | None = None

    @property
    def closing(self) -> bool:
        """Return `True` if the workspace is in shutdown."""
        return self._closing

    @property
    def manifest(self) -> Manifest | None:
        """Return the current manifest, or `None` if not yet loaded."""
        return self._manifest

    async def materialize(self, resources: list[Resource]) -> None:
        """Materialize resources into the local workspace.

        Downloads missing blobs from the backend, stores them in CAS,
        and creates local file representations (reflink/hardlink/copy).

        Args:
            resources: List of resources to materialize.
        """
        materializer = Materializer(
            backend=self._backend,
            cas=self._cas,
            workspace_root=self.root,
        )
        await materializer.materialize(resources)
        logger.info("Materialized %d resources for run %s", len(resources), self.run_id)

    async def materialize_content(
        self,
        *,
        protected: Iterable[str] = (),
        reconcile: bool = True,
    ) -> int:
        """Materialize the content tree (dual-source sync) into the workspace.

        Lists every file under the content backend's root and restores it at
        the same relative path locally, skipping engine directories and files
        whose local content already matches. This is the "read path" half of
        content mirroring — the write half happens at checkpoint time (dirty
        files/deletions are mirrored back by the checkpoint manager).

        Args:
            protected: Relative paths that must never be reconciled away
                (e.g. engine manifest artifact paths — agent outputs that
                live in the workspace but not necessarily in the content
                tree).
            reconcile: When true, after the restore pass delete local files
                that no longer exist in the content tree and are not
                `protected` (the delete half of the read path — backend-side
                deletions must not linger in the workspace, RFC-RD-607 §4.3).

        Returns:
            Number of files downloaded (unchanged files are skipped).
        """
        if self._content_backend is None:
            return 0

        import hashlib as _hashlib

        skip_prefixes = (".workspace/", "__pycache__/", ".git/")
        rel_paths = await self._content_backend.list_content_files()
        downloaded = 0
        for rel in rel_paths:
            if rel.startswith(skip_prefixes):
                continue
            dest = self.root / rel
            if dest.is_file() and not dest.is_symlink():
                existing = dest.read_bytes()
                remote = await self._content_backend.get_content_file(rel)
                if (
                    remote is not None
                    and _hashlib.sha256(existing).hexdigest() == _hashlib.sha256(remote).hexdigest()
                ):
                    continue
            elif dest.exists():
                continue  # symlink or special file — leave it alone
            data = await self._content_backend.get_content_file(rel)
            if data is None:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            downloaded += 1
        if rel_paths:
            logger.info(
                "Content materialization for run %s: %d/%d files fetched",
                self.run_id,
                downloaded,
                len(rel_paths),
            )
        if reconcile:
            await self._reconcile_content(rel_paths, protected, skip_prefixes)
        return downloaded

    async def _reconcile_content(
        self,
        remote_rel_paths: list[str],
        protected: Iterable[str],
        skip_prefixes: tuple[str, ...],
    ) -> None:
        """Delete local files that no longer exist in the content tree.

        Files under the skip prefixes, symlinked paths, and `protected`
        relative paths (agent artifacts absent from the content tree) are
        never removed. Now-empty parent directories are pruned best-effort,
        except the root and the engine-created top-level dirs
        (`.workspace`/`input`/`working`/`output`).
        """
        keep = {rel for rel in remote_rel_paths}
        keep.update(p.strip("/") for p in protected if p)
        keep_roots = {"input", "working", "output", ".workspace"}
        removed: list[Path] = []

        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(self.root).as_posix()
            if rel.startswith(skip_prefixes):
                continue
            if rel in keep:
                continue
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                logger.warning(
                    "Content reconcile: failed to remove %s for run %s", rel, self.run_id
                )

        if removed:
            logger.info(
                "Content reconcile for run %s: %d stale file(s) removed",
                self.run_id,
                len(removed),
            )
            parents = {p.parent for p in removed}
            for parent in sorted(parents, key=lambda d: len(d.parts), reverse=True):
                rel_parent = parent.relative_to(self.root)
                if len(rel_parent.parts) <= 1 or rel_parent.parts[0] in keep_roots:
                    continue
                try:
                    next(parent.rglob("*"))
                except StopIteration:
                    try:
                        parent.rmdir()
                    except OSError:
                        logger.debug(
                            "Content reconcile: rmdir failed for %s", parent, exc_info=True
                        )

    async def start_tracking(self) -> None:
        """Start dirty tracking and background uploader.

        Call after materialization, before the agent begins work.
        """
        self._dirty_tracker.start()
        self._uploader.start()
        self._debouncer.start()
        logger.info("Started tracking for run %s", self.run_id)

    async def checkpoint(self) -> str | None:
        """Create a checkpoint of the current dirty state.

        Returns:
            Checkpoint ID, or `None` if nothing was dirty.
        """
        if self._closing:
            logger.warning("Checkpoint skipped — workspace is closing")
            return None

        self._dirty_tracker.scan_now()
        dirty = self._dirty_tracker.get_dirty()
        deleted = self._dirty_tracker.deleted_files
        if not dirty and not deleted:
            logger.debug("No dirty files — skipping checkpoint")
            return None

        checkpoint_id = await self._checkpoint_mgr.create_checkpoint()
        self._dirty_tracker.clear()
        return checkpoint_id

    async def publish(self, artifacts: list[ArtifactSpec]) -> list[Artifact]:
        """Publish artifacts to the durable store.

        Args:
            artifacts: List of artifact specs to publish.

        Returns:
            List of published artifact metadata.
        """
        published: list[Artifact] = []
        for spec in artifacts:
            if not spec.publish:
                continue
            file_path = self.root / spec.path
            if not file_path.exists():
                logger.warning("Artifact not found for publish: %s", spec.path)
                continue
            data = file_path.read_bytes()
            artifact = await self._backend.publish_artifact(
                spec.path, data, content_type=spec.content_type
            )
            published.append(artifact)
            logger.info("Published artifact: %s → %s", spec.path, artifact.published_uri)
        return published

    async def close(self) -> None:
        """Close the workspace with multi-step drain.

        Sequence:
            1. Stop dirty tracker (no new events).
            2. Flush debouncer (force final checkpoint).
            3. Wait for uploader to drain.
            4. Close state store.
        """
        self._closing = True

        await self._dirty_tracker.stop()

        if self._debouncer is not None:
            await self._debouncer.flush()

        if self._uploader is not None:
            await self._uploader.stop()

        if self._state_store is not None:
            await self._state_store.close()

        logger.info("Workspace closed for run %s", self.run_id)

    async def cleanup(self) -> None:
        """Remove all workspace state and local files."""
        if self._state_store is not None:
            await self._state_store.cleanup()
        import shutil

        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        logger.info("Workspace cleaned up for run %s", self.run_id)

    async def recover(self) -> Manifest | None:
        """Recover workspace state from the latest checkpoint.

        Returns:
            The recovered manifest, or `None` if no checkpoints exist.
        """
        if self._checkpoint_mgr is None:
            return None
        return await self._checkpoint_mgr.recover()
