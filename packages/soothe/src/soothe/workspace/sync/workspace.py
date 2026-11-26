"""Workspace handle — per-run lifecycle: open, materialize, checkpoint, publish, close.

The `Workspace` is the agent-facing handle to a single run's workspace.
It coordinates the CAS cache, dirty tracker, debouncer, checkpoint
manager, and background uploader to provide a simple async lifecycle API.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from soothe.workspace.sync.cas import CASCache
from soothe.workspace.sync.checkpoint import CheckpointManager
from soothe.workspace.sync.debouncer import CheckpointDebouncer
from soothe.workspace.sync.dirty_tracker import DirtyTracker
from soothe.workspace.sync.materializer import Materializer
from soothe.workspace.sync.paths import validate_relative_path
from soothe.workspace.sync.streaming import iter_content_chunks
from soothe.workspace.sync.uploader import BackgroundUploader
from soothe.workspace.sync.workspace_sync import (
    Artifact,
    ArtifactSpec,
    Manifest,
)

if TYPE_CHECKING:
    from soothe.workspace.state.protocol import WorkspaceStateStore
    from soothe.workspace.sync.workspace_sync import CheckpointPayload, WorkspaceSyncBackend

logger = logging.getLogger(__name__)

_FILE_COPY_CHUNK = 1024 * 1024  # 1 MiB


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

    async def materialize(self, manifest: Manifest) -> None:
        """Materialize a resource manifest into the local workspace.

        Downloads missing blobs from the backend, stores them in CAS,
        and creates local file representations (reflink/copy).

        Args:
            manifest: The manifest whose `resources` are materialized
                (under `<root>/input/`).  Callers holding bare
                :class:`Resource` lists can wrap them in a `Manifest`.
        """
        if self._backend is None:
            logger.warning("Materialize called without a remote backend — nothing to fetch")
            return
        materializer = Materializer(
            backend=self._backend,
            cas=self._cas,
            workspace_root=self.root,
        )
        materialized = await materializer.materialize(manifest)
        self._manifest = manifest
        logger.info(
            "Materialized %d/%d resources for run %s",
            len(materialized),
            len(manifest.resources),
            self.run_id,
        )

    async def materialize_content(
        self,
        *,
        protected: Iterable[str] = (),
        reconcile: bool = True,
    ) -> int:
        """Materialize the content tree (dual-source sync) into the workspace.

        Lists every file under the content backend's root and restores it at
        the same relative path locally, skipping engine directories, paths
        that fail relative-path validation, and files whose local content
        already matches. Downloads stream chunk-by-chunk into a temp file
        (running hash), so memory stays bounded for large content; an
        unchanged file costs one streamed read, not a rewrite. This is the
        "read path" half of content mirroring — the write half happens at
        checkpoint time (dirty files/deletions are mirrored back by the
        checkpoint manager).

        Args:
            protected: Relative paths that must never be reconciled away
                (e.g. engine manifest artifact paths — agent outputs that
                live in the workspace but not necessarily in the content
                tree).  The current dirty set and the in-memory manifest
                artifact paths are ALWAYS protected implicitly (F-C13).
            reconcile: When true, after the restore pass delete local files
                that no longer exist in the content tree and are not
                `protected` (the delete half of the read path — backend-side
                deletions must not linger in the workspace,).

        Returns:
            Number of files downloaded (unchanged files are skipped).
        """
        if self._content_backend is None:
            return 0

        skip_prefixes = (".workspace/", "__pycache__/", ".git/")
        rel_paths = await self._content_backend.list_content_files()
        downloaded = 0
        for rel in rel_paths:
            if rel.startswith(skip_prefixes):
                continue
            # Remote listings are untrusted input — a key containing
            # traversal segments must never reach path joining.
            try:
                validate_relative_path(rel, name="content_path")
            except ValueError:
                logger.warning("Skipping invalid content path from backend: %r", rel)
                continue
            dest = self.root / rel
            if dest.exists() and not dest.is_file():
                continue  # symlink, directory, or special file — leave it alone
            downloaded += await self._stream_content_file(rel, dest)
        if rel_paths:
            logger.info(
                "Content materialization for run %s: %d/%d files fetched",
                self.run_id,
                downloaded,
                len(rel_paths),
            )
        if reconcile:
            protected_set = self._implicit_protected(protected)
            await self._reconcile_content(rel_paths, protected_set, skip_prefixes)
        return downloaded

    async def _stream_content_file(self, rel: str, dest: Path) -> int:
        """Stream one content-tree file to its local destination.

        Returns 1 when the local file was replaced (or created), 0 when
        the local content already matches (temp discarded).

        Args:
            rel: Validated relative path.
            dest: Local destination path.

        Returns:
            1 if downloaded, 0 if skipped.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.parent / f".{dest.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        h = hashlib.sha256()
        # Chunks are awaited sequentially, so sequential writes through
        # one handle are race-free while staying off the event loop.
        f = await asyncio.to_thread(open, tmp, "wb")

        def _write(chunk: bytes) -> None:
            f.write(chunk)

        try:
            async for chunk in iter_content_chunks(self._content_backend, rel):
                await asyncio.to_thread(_write, chunk)
                h.update(chunk)
        except KeyError:
            await asyncio.to_thread(f.close)
            tmp.unlink(missing_ok=True)
            return 0
        except Exception:
            await asyncio.to_thread(f.close)
            tmp.unlink(missing_ok=True)
            raise
        await asyncio.to_thread(f.close)

        if dest.is_file() and not dest.is_symlink():
            local_hash = await asyncio.to_thread(_hash_file, dest)
            if local_hash == h.hexdigest():
                tmp.unlink(missing_ok=True)
                return 0
        await asyncio.to_thread(os.replace, tmp, dest)
        return 1

    def _implicit_protected(self, protected: Iterable[str]) -> set[str]:
        """Extend caller protection with dirty set and manifest artifacts.

        Args:
            protected: Caller-supplied protected relative paths.

        Returns:
            The extended protection set.
        """
        keep = {p.strip("/") for p in protected if p}
        keep.update(self._dirty_tracker.get_dirty().keys())
        keep.update(self._dirty_tracker.deleted_files)
        manifest = self._checkpoint_mgr.manifest if self._checkpoint_mgr else None
        if manifest is not None:
            keep.update(e.path for e in manifest.artifacts)
            keep.update(e.path for e in manifest.resources)
        return keep

    async def _reconcile_content(
        self,
        remote_rel_paths: Sequence[str],
        protected: set[str],
        skip_prefixes: tuple[str, ...],
    ) -> None:
        """Delete local files that no longer exist in the content tree.

        Files under the skip prefixes, symlinked paths, and `protected`
        relative paths (agent artifacts absent from the content tree) are
        never removed. Now-empty parent directories are pruned best-effort,
        except the root and the engine-created top-level dirs
        (`.workspace`/`input`/`working`/`output`).
        """
        keep = set(remote_rel_paths)
        keep.update(protected)
        keep_roots = {"input", "working", "output", ".workspace"}
        # Configured watched subtrees (e.g. "deliverables") are engine-managed
        # roots too — an empty one must survive reconcile pruning.
        watched = getattr(self._dirty_tracker, "watched_subdirs", None)
        if watched:
            keep_roots.update(watched)
        removed: list[Path] = []

        for path in await asyncio.to_thread(self._scan_local_files):
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

    def _scan_local_files(self) -> list[Path]:
        """Return all regular files under the workspace root (sync)."""
        return [p for p in self.root.rglob("*") if p.is_file()]

    async def start_tracking(self) -> None:
        """Start dirty tracking and background uploader.

        Call after materialization, before the agent begins work.
        Every component is optional — a workspace opened without a
        remote backend or state store starts the pieces it has.
        """
        self._dirty_tracker.start()
        if self._uploader is not None:
            self._uploader.start()
        if self._debouncer is not None:
            self._debouncer.start()
        logger.info("Started tracking for run %s", self.run_id)

    async def checkpoint(self) -> str | None:
        """Create a checkpoint of the current dirty state.

        Returns:
            Checkpoint ID, or `None` if nothing was dirty or the
            workspace has no checkpoint manager.
        """
        if self._closing:
            logger.warning("Checkpoint skipped — workspace is closing")
            return None

        if self._checkpoint_mgr is None:
            logger.debug("No checkpoint manager — skipping checkpoint")
            return None

        self._dirty_tracker.scan_now()
        # Ack-clearing is the checkpoint manager's job (snapshot-ack
        # semantics, F-C2) — a full clear here would drop mid-checkpoint
        # writes.
        return await self._checkpoint_mgr.create_checkpoint()

    async def publish(self, artifacts: list[ArtifactSpec]) -> list[Artifact]:
        """Publish artifacts to the durable store.

        Args:
            artifacts: List of artifact specs to publish.

        Returns:
            List of published artifact metadata.
        """
        if self._backend is None:
            logger.warning("Publish called without a remote backend — nothing to publish")
            return []
        published: list[Artifact] = []
        for spec in artifacts:
            if not spec.publish:
                continue
            file_path = self.root / spec.path
            if not file_path.exists():
                logger.warning("Artifact not found for publish: %s", spec.path)
                continue
            data = await asyncio.to_thread(file_path.read_bytes)
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
            2. Flush debouncer (force final checkpoint), then stop its
               timers.
            3. Wait for uploader to drain (pending rows pushed remote).
            4. Close state store.
            5. Close the per-run content backend (its thread pool / fsspec
               client). The primary `backend` is shared across workspaces
               (owned by the manager / caller) and is NOT closed here.
        """
        self._closing = True

        await self._dirty_tracker.stop()

        if self._debouncer is not None:
            await self._debouncer.flush()
            await self._debouncer.stop()

        if self._uploader is not None:
            await self._uploader.stop()

        if self._state_store is not None:
            await self._state_store.close()

        if self._content_backend is not None:
            # Duck-typed call: test fakes implement the sync surface only.
            aclose = getattr(self._content_backend, "aclose", None)
            if callable(aclose):
                await aclose()

        logger.info("Workspace closed for run %s", self.run_id)

    async def cleanup(self) -> None:
        """Remove all workspace state and local files."""
        if self._state_store is not None:
            await self._state_store.cleanup()
        import shutil

        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        logger.info("Workspace cleaned up for run %s", self.run_id)

    async def recover(self) -> CheckpointPayload | None:
        """Recover workspace state from the latest replayable checkpoint.

        Returns:
            The recovered checkpoint payload (``incomplete=True`` when delta
            replay stopped early — the dirty state is then partial), or
            `None` if no checkpoints exist.
        """
        if self._checkpoint_mgr is None:
            return None
        return await self._checkpoint_mgr.recover()


def _hash_file(path: str | Path) -> str:
    """Compute SHA-256 hex digest of a file's contents (chunked).

    Args:
        path: File path.

    Returns:
        Hex-encoded SHA-256 digest.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_FILE_COPY_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
