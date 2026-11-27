"""Per-run workspace handle for content-authoritative sync.

`checkpoint()` hashes dirty files, streams them to the content tree,
mirrors deletions, and writes the manifest (version + optimistic
concurrency guard) as the atomic commit point. Obtain handles through
`WorkspaceManager.open`.
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

from soothe.workspace.sync.dirty_tracker import DirtyTracker
from soothe.workspace.sync.paths import validate_relative_path
from soothe.workspace.sync.reserved import ENGINE_SKIP_PREFIXES
from soothe.workspace.sync.streaming import iter_content_chunks, put_content_file_from_file
from soothe.workspace.sync.workspace_sync import Manifest, ManifestEntry

if TYPE_CHECKING:
    from soothe.workspace.sync.workspace_sync import WorkspaceSyncBackend

logger = logging.getLogger(__name__)

_FILE_COPY_CHUNK = 1024 * 1024
_MAX_CONCURRENT_FILES = 8


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


class Workspace:
    """Per-run workspace handle (content-authoritative sync).

    Coordinates content materialization, dirty tracking, and turn commits
    for a single agent run.

    Args:
        run_id: Unique run identifier.
        root: Local filesystem root of the workspace.
        backend: Per-run remote backend — serves both the plain
            content tree (authoritative file store) and the
            engine-private manifest (commit record).  `None` disables
            remote sync entirely (local-only mode).
        dirty_tracker: Dirty file tracker.
        manifest: Manifest loaded from the backend at open (the
            engine-owned artifact set of the last committed turn).
    """

    def __init__(
        self,
        *,
        run_id: str,
        root: Path,
        backend: WorkspaceSyncBackend | None,
        dirty_tracker: DirtyTracker,
        manifest: Manifest | None = None,
    ) -> None:
        """Initialize the workspace sync session with all sub-components."""
        self.run_id = run_id
        self.root = root
        self._backend = backend
        self._dirty_tracker = dirty_tracker
        self._manifest = manifest
        self._closing = False

    @property
    def closing(self) -> bool:
        """Return `True` if the workspace is in shutdown."""
        return self._closing

    @property
    def manifest(self) -> Manifest | None:
        """Return the current manifest, or `None` if none was loaded."""
        return self._manifest

    async def materialize_content(
        self,
        *,
        protected: Iterable[str] = (),
        reconcile: bool = True,
    ) -> int:
        """Materialize the content tree (authoritative file store) into the workspace.

        Lists every file under the content backend's root and restores it at
        the same relative path locally, skipping engine directories, paths
        that fail relative-path validation, and files whose local content
        already matches. Downloads stream chunk-by-chunk into a temp file
        (running hash), so memory stays bounded for large content; an
        unchanged file costs one streamed read, not a rewrite.

        Args:
            protected: Relative paths that must never be reconciled away
                (e.g. manifest artifact paths — engine-owned outputs that
                a mid-commit crash may not have landed in the content
                tree yet).  The current dirty set and the in-memory
                manifest artifact paths are ALWAYS protected implicitly.
            reconcile: When true, after the restore pass delete local files
                that no longer exist in the content tree and are not
                `protected` (backend-side deletions must not linger in
                the workspace).

        Returns:
            Number of files downloaded (unchanged files are skipped).
        """
        if self._backend is None:
            return 0

        skip_prefixes = ENGINE_SKIP_PREFIXES
        rel_paths = await self._backend.list_content_files()
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
                continue
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
            async for chunk in iter_content_chunks(self._backend, rel):
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
        if self._manifest is not None:
            keep.update(e.path for e in self._manifest.artifacts)
        return keep

    async def _reconcile_content(
        self,
        remote_rel_paths: Sequence[str],
        protected: set[str],
        skip_prefixes: tuple[str, ...],
    ) -> None:
        """Delete local files that no longer exist in the content tree.

        Files under the skip prefixes, symlinked paths, and `protected`
        relative paths are never removed. Now-empty parent directories
        are pruned best-effort, except the root and the engine-created
        top-level dirs (`.soothe`/`input`/`working`/`output`).
        """
        keep = set(remote_rel_paths)
        keep.update(protected)
        keep_roots = {"input", "working", "output", ".soothe"}
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

    def start_tracking(self) -> None:
        """Start dirty tracking.

        Call after materialization, before the agent begins work.
        """
        self._dirty_tracker.start()
        logger.info("Started tracking for run %s", self.run_id)

    async def checkpoint(self) -> int | None:
        """Commit dirty state to the content tree.

        Dirty files are hashed and streamed to the content tree, deletions
        are mirrored, then the manifest is advanced and written as the
        commit point. Only committed paths are acknowledged, so writes
        arriving mid-commit stay dirty. Failed uploads produce a partial
        commit and a final `RuntimeError`.

        Returns:
            The new manifest version, or `None` when nothing was dirty
            or deleted, or the workspace has no backend (local-only).
        """
        if self._closing:
            logger.warning("Checkpoint skipped — workspace is closing")
            return None
        if self._backend is None:
            logger.debug("No backend — skipping checkpoint")
            return None

        self._dirty_tracker.scan_now()

        dirty = self._dirty_tracker.get_dirty()
        deleted = set(self._dirty_tracker.deleted_files)

        for rel_path in dirty:
            if not (self.root / rel_path).exists():
                deleted.add(rel_path)
        dirty = {p: e for p, e in dirty.items() if p not in deleted}

        if not dirty and not deleted:
            logger.debug("No dirty or deleted files — skipping checkpoint")
            return None

        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_FILES)
        committed: list[ManifestEntry] = []
        failures: list[tuple[str, BaseException]] = []

        async def _commit_file(rel_path: str) -> None:
            async with semaphore:
                try:
                    entry = await self._commit_one_file(rel_path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Failed to commit %s for run %s; it stays dirty for the next commit",
                        rel_path,
                        self.run_id,
                        exc_info=True,
                    )
                    failures.append((rel_path, exc))
                    return
                committed.append(entry)

        await asyncio.gather(*(_commit_file(rel_path) for rel_path in sorted(dirty)))

        for d in sorted(deleted):
            try:
                await self._backend.delete_content_file(d)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Content delete mirror failed for %s (run %s)",
                    d,
                    self.run_id,
                    exc_info=True,
                )

        self._manifest = self._advance_manifest(committed, deleted)
        await self._backend.put_manifest(
            self.run_id,
            self._manifest,
            if_match=(str(self._manifest.version - 1) if self._manifest.version > 1 else None),
        )
        logger.info(
            "Committed run %s at manifest version %d: %d file(s), %d deletion(s)",
            self.run_id,
            self._manifest.version,
            len(committed),
            len(deleted),
        )

        self._dirty_tracker.clear(acked_paths=[e.path for e in committed] + sorted(deleted))

        if failures:
            failed_paths = ", ".join(p for p, _ in failures)
            raise RuntimeError(
                f"commit for run {self.run_id} completed partially: "
                f"{len(failures)} file(s) failed to upload ({failed_paths}); "
                "they remain dirty"
            )

        return self._manifest.version

    async def _commit_one_file(self, rel_path: str) -> ManifestEntry:
        """Hash and upload one dirty file to the content tree.

        Args:
            rel_path: Workspace-relative path of the file.

        Returns:
            The manifest entry for the committed file.

        Raises:
            Exception: When the file cannot be committed (caller keeps
                it dirty and records the failure).
        """
        abs_path = self.root / rel_path
        if not abs_path.exists():
            raise FileNotFoundError(rel_path)
        if abs_path.is_symlink():
            target = abs_path.resolve()
            try:
                target.relative_to(self.root.resolve())
            except ValueError:
                raise ValueError(f"escaping symlink not committed: {rel_path}") from None

        sha256 = await asyncio.to_thread(_hash_file, abs_path)
        size = abs_path.stat().st_size
        await put_content_file_from_file(self._backend, rel_path, abs_path)
        return ManifestEntry(path=rel_path, sha256=sha256, size=size)

    def _advance_manifest(
        self,
        committed: list[ManifestEntry],
        deleted: set[str],
    ) -> Manifest:
        """Fold committed entries and tombstones into the manifest.

        Args:
            committed: Successfully uploaded entries.
            deleted: Tombstoned relative paths.

        Returns:
            The next manifest (version incremented).
        """
        if self._manifest is None:
            return Manifest(
                run_id=self.run_id,
                version=1,
                artifacts=sorted(committed, key=lambda e: e.path),
            )
        artifacts = {e.path: e for e in self._manifest.artifacts}
        for entry in committed:
            artifacts[entry.path] = entry
        for d in deleted:
            artifacts.pop(d, None)
        return Manifest(
            run_id=self.run_id,
            version=self._manifest.version + 1,
            artifacts=sorted(artifacts.values(), key=lambda e: e.path),
        )

    async def close(self) -> None:
        """Stop dirty tracking and release the per-run backend."""
        self._closing = True

        await self._dirty_tracker.stop()

        if self._backend is not None:
            # Duck-typed backends may not implement `aclose`.
            aclose = getattr(self._backend, "aclose", None)
            if callable(aclose):
                await aclose()

        logger.info("Workspace closed for run %s", self.run_id)
