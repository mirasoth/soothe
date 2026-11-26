"""Concrete `WorkspaceSyncBackend` backed by any fsspec filesystem.

One adapter supports S3 (`s3fs`), GCS (`gcsfs`), Azure Blob (`adlfs`),
local, and memory filesystems.  All fsspec calls are offloaded to a
dedicated thread pool via `asyncio.to_thread()`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from soothe.workspace.sync.errors import ConcurrentModificationError, IntegrityError
from soothe.workspace.sync.paths import validate_path_component, validate_relative_path
from soothe.workspace.sync.workspace_sync import (
    Artifact,
    Manifest,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

_DEFAULT_MAX_WORKERS = 8
_STREAM_CHUNK_SIZE = 65536  # 64 KiB

# s3fs's default put chunksize (50 MiB) makes it whole-buffer any file
# smaller than 100 MiB; an explicit 8 MiB chunksize switches to
# multipart (each part a known-length bytes buffer — required by S3
# compatible stores like Tencent COS that reject chunked
# Transfer-Encoding) and caps per-part memory.
_S3_PUT_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB
# s3fs defaults to max_concurrency=10 upload parts in flight; with the
# 8 MiB chunk size that is 80 MiB of raw chunk buffers plus serializer
# copies. 2 keeps the in-flight window at ~16 MiB.
_S3_PUT_MAX_CONCURRENCY = 2
# Range-GET size for streaming reads from s3 (same sizing rationale).
_S3_RANGE_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB


def _hash_file(src: Path) -> str:
    """Compute SHA-256 hex digest of a file (chunked, bounded memory).

    Args:
        src: File path.

    Returns:
        Hex-encoded SHA-256 digest.
    """
    h = hashlib.sha256()
    with open(src, "rb") as f:
        while True:
            chunk = f.read(_STREAM_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# Filesystems whose writes require the parent directory to exist.
# Object stores (s3/gs/az) create keys under implicit prefixes — calling
# ``makedirs`` there only costs a bucket probe per write.
_DIR_BEARING_PROTOCOLS = frozenset({"local", "file", "memory", "dir"})


def _checkpoint_sort_key(checkpoint_id: str) -> tuple[str, int, str]:
    """Sort key ordering checkpoint IDs by numeric sequence within a run.

    Args:
        checkpoint_id: Full checkpoint ID (``<run_id>-cNNNNNN``).

    Returns:
        Tuple of (run prefix, numeric sequence, raw ID).  IDs that do
        not match the expected shape sort last, lexicographically.
    """
    run_id, _, seq = checkpoint_id.rpartition("-")
    if run_id and seq.startswith("c") and seq[1:].isdigit():
        return (run_id, int(seq[1:]), checkpoint_id)
    return (checkpoint_id, -1, checkpoint_id)


class FsspecSyncBackend:
    """`WorkspaceSyncBackend` backed by an fsspec `AbstractFileSystem`.

    Args:
        fs: An fsspec filesystem instance (e.g. `MemoryFileSystem`,
            `S3FileSystem`, `LocalFileSystem`).
        root: Root path within the filesystem where all workspace sync
            objects are stored.
        max_workers: Thread pool size for I/O offloading.

    Example:
        >>> backend = FsspecSyncBackend(fs=MemoryFileSystem(), root="/ws")
        >>> data = await backend.get_blob("abc123...")
    """

    def __init__(
        self,
        *,
        fs: Any,
        root: str,
        max_workers: int = _DEFAULT_MAX_WORKERS,
    ) -> None:
        """Initialize the fsspec filesystem, root path, and thread pool."""
        self._fs = fs
        self._root = root.rstrip("/")
        protocol = fs.protocol
        if isinstance(protocol, str):
            protocol = (protocol,)
        self._needs_parent_dirs = any(p in _DIR_BEARING_PROTOCOLS for p in protocol)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="fsspec-sync",
        )
        logger.debug(
            "FsspecSyncBackend initialized: root=%s, fs=%s, max_workers=%d",
            self._root,
            type(fs).__name__,
            max_workers,
        )

    # ------------------------------------------------------------------
    # Path-layout mapper
    # ------------------------------------------------------------------

    def _blob_path(self, sha256: str) -> str:
        """Map a content hash to its storage path."""
        validate_path_component(sha256, name="sha256")
        return f"{self._root}/blobs/sha256/{sha256[:2]}/{sha256}"

    def _manifest_path(self, run_id: str) -> str:
        """Map a run ID to its manifest storage path."""
        validate_path_component(run_id, name="run_id")
        return f"{self._root}/runs/{run_id}/manifest.json"

    def _checkpoint_dir(self, run_id: str) -> str:
        """Map a run ID to its checkpoint directory."""
        validate_path_component(run_id, name="run_id")
        return f"{self._root}/runs/{run_id}/checkpoints"

    def _checkpoint_path(self, checkpoint_id: str) -> str:
        """Map a checkpoint ID to its storage path."""
        validate_path_component(checkpoint_id, name="checkpoint_id")
        if "-" in checkpoint_id:
            run_id = checkpoint_id.rsplit("-", 1)[0]
            validate_path_component(run_id, name="checkpoint_id (run prefix)")
            seq = checkpoint_id.rsplit("-", 1)[1]
            validate_path_component(seq, name="checkpoint_id (sequence)")
        else:
            run_id = checkpoint_id
        return f"{self._root}/runs/{run_id}/checkpoints/{checkpoint_id}.json"

    def _artifact_storage_path(self, artifact_path: str) -> str:
        """Map an artifact relative path to its storage path."""
        validated = validate_relative_path(artifact_path, name="artifact_path")
        return f"{self._root}/artifacts/{validated}"

    def _ensure_parent_dir(self, path: str) -> None:
        """Create the parent directory for ``path`` when required.

        Object stores (s3/gs/az) create keys implicitly; directory-
        bearing filesystems (local/memory) need explicit parents.
        """
        if not self._needs_parent_dirs:
            return
        parent = path.rsplit("/", 1)[0]
        if parent:
            self._fs.makedirs(parent, exist_ok=True)

    def _write_bytes(self, path: str, data: bytes) -> None:
        """Write bytes to ``path``, creating parents when required."""
        self._ensure_parent_dir(path)
        self._fs.pipe(path, data)

    def _stream_from_file(self, dest: str, src: Path, *, expected_sha256: str | None) -> None:
        """Copy a local file to ``dest`` with bounded memory.

        Runs as a single thread job.  The transfer uses
        ``fs.put(src, dest)`` — the source size is known up front, so
        object stores can set Content-Length and use multipart upload
        (s3fs's ``open('wb')`` incremental writes send chunked
        Transfer-Encoding, which S3-compatible stores like Tencent COS
        reject).  When ``expected_sha256`` is given the source is
        hashed first (chunked local read) and a mismatch removes the
        stored object and raises — a blob stored under a wrong hash
        key would poison every future read of that key.

        Args:
            dest: Destination path on the filesystem.
            src: Local source file path.
            expected_sha256: When provided, verify the content hashes
                to this value.

        Raises:
            IntegrityError: On hash mismatch (stored object removed).
        """
        self._ensure_parent_dir(dest)
        if expected_sha256 is not None:
            actual = _hash_file(src)
            if actual != expected_sha256:
                from soothe.workspace.sync.errors import IntegrityError

                raise IntegrityError(
                    f"streamed content hash mismatch for {dest}: "
                    f"expected {expected_sha256[:16]}..., got {actual[:16]}..."
                )
        protocol = self._fs.protocol
        protocols = protocol if isinstance(protocol, (tuple, list)) else (protocol,)
        put_kwargs: dict[str, Any] = {}
        if "s3" in protocols:
            put_kwargs["chunksize"] = _S3_PUT_CHUNK_SIZE
            put_kwargs["max_concurrency"] = _S3_PUT_MAX_CONCURRENCY
        self._fs.put(str(src), dest, **put_kwargs)

    # ------------------------------------------------------------------
    # Thread-pool helper
    # ------------------------------------------------------------------

    async def _to_thread(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a sync function in the dedicated thread pool."""
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, lambda: fn(*args, **kwargs)
        )

    # ------------------------------------------------------------------
    # Blob operations (content-addressed)
    # ------------------------------------------------------------------

    async def get_blob(self, sha256: str) -> bytes | None:
        """Return blob content by hash, or `None` if absent."""
        path = self._blob_path(sha256)
        try:
            return await self._to_thread(self._fs.cat, path)
        except FileNotFoundError:
            return None

    async def put_blob(self, sha256: str, data: bytes) -> None:
        """Store a blob.  Idempotent — no-op if the hash already exists.

        Verifies content hash before storing.

        Raises:
            IntegrityError: If `data`'s actual SHA-256 does not match
                `sha256`.
        """
        validate_path_component(sha256, name="sha256")
        actual_hash = hashlib.sha256(data).hexdigest()
        if actual_hash != sha256:
            raise IntegrityError(
                f"content hash mismatch for blob {sha256[:8]!r}: "
                f"expected {sha256[:16]}..., got {actual_hash[:16]}..."
            )

        path = self._blob_path(sha256)
        await self._to_thread(self._write_bytes, path, data)

    async def put_blob_from_file(self, sha256: str, src: Path) -> None:
        """Store a blob by streaming a local file.

        The content hash is verified against the source file before
        the transfer; the transfer itself uses ``fs.put`` (known
        source size → Content-Length set, multipart for large files).

        Args:
            sha256: Expected content hash.
            src: Local source file path.

        Raises:
            IntegrityError: If the source does not hash to `sha256`.
        """
        validate_path_component(sha256, name="sha256")
        path = self._blob_path(sha256)
        await self._to_thread(self._stream_from_file, path, src, expected_sha256=sha256)

    async def head_blob(self, sha256: str) -> bool:
        """Return whether a blob with this hash exists remotely."""
        path = self._blob_path(sha256)
        return await self._to_thread(self._fs.exists, path)

    # ------------------------------------------------------------------
    # Manifest operations (optimistic concurrency)
    # ------------------------------------------------------------------

    async def get_manifest(self, run_id: str) -> Manifest | None:
        """Fetch the latest manifest for `run_id`, or `None` if none exists."""
        path = self._manifest_path(run_id)
        try:
            data = await self._to_thread(self._fs.cat, path)
        except FileNotFoundError:
            return None
        return Manifest.model_validate_json(data)

    async def put_manifest(
        self,
        run_id: str,
        manifest: Manifest,
        *,
        if_match: str | None = None,
    ) -> Manifest:
        """Write or update the manifest for `run_id`.

        Args:
            run_id: Target run identifier.
            manifest: The manifest to write.
            if_match: If provided, reject the write when the stored
                manifest version does not match.

        Returns:
            The written manifest.

        Raises:
            ConcurrentModificationError: If `if_match` is provided and
                the stored version does not match, or a manifest was
                expected but none exists.
        """
        if if_match is not None:
            existing = await self.get_manifest(run_id)
            if existing is None:
                raise ConcurrentModificationError(
                    f"manifest for run {run_id!r} was expected at version "
                    f"{if_match} but none exists"
                )
            if str(existing.version) != str(if_match):
                raise ConcurrentModificationError(
                    f"manifest version mismatch for run {run_id!r}: "
                    f"expected {if_match}, found {existing.version}"
                )

        path = self._manifest_path(run_id)
        data = manifest.model_dump_json().encode("utf-8")
        await self._to_thread(self._write_bytes, path, data)
        return manifest

    # ------------------------------------------------------------------
    # Checkpoint operations
    # ------------------------------------------------------------------

    async def list_checkpoints(self, run_id: str) -> list[str]:
        """Return checkpoint IDs for `run_id`, ordered oldest-first."""
        ckpt_dir = self._checkpoint_dir(run_id)
        try:
            entries = await self._to_thread(self._fs.ls, ckpt_dir)
        except FileNotFoundError:
            return []

        ids: list[str] = []
        for entry in entries:
            name = entry if isinstance(entry, str) else entry.get("name", "")
            if name.endswith(".json"):
                ckpt_id = name.rsplit("/", 1)[-1].removesuffix(".json")
                ids.append(ckpt_id)
        ids.sort(key=_checkpoint_sort_key)
        return ids

    async def get_checkpoint(self, checkpoint_id: str) -> bytes | None:
        """Fetch checkpoint payload by ID, or `None` if absent."""
        path = self._checkpoint_path(checkpoint_id)
        try:
            return await self._to_thread(self._fs.cat, path)
        except FileNotFoundError:
            return None

    async def put_checkpoint(
        self,
        checkpoint_id: str,
        data: bytes,
    ) -> None:
        """Store a checkpoint payload.

        Args:
            checkpoint_id: Unique checkpoint identifier.
            data: Serialized checkpoint payload.
        """
        path = self._checkpoint_path(checkpoint_id)
        await self._to_thread(self._write_bytes, path, data)

    async def delete_checkpoint(self, checkpoint_id: str) -> None:
        """Delete a stored checkpoint payload (idempotent).

        Args:
            checkpoint_id: Unique checkpoint identifier.
        """
        path = self._checkpoint_path(checkpoint_id)
        try:
            await self._to_thread(self._fs.rm_file, path)
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # Publish operations
    # ------------------------------------------------------------------

    async def publish_artifact(
        self,
        artifact_path: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> Artifact:
        """Publish an artifact to the durable store.

        Args:
            artifact_path: Relative workspace path of the artifact.
            data: Raw artifact content.
            content_type: MIME type hint (unused by fsspec).

        Returns:
            Published `Artifact` with SHA-256, size, and storage URI.
        """
        del content_type
        sha256 = hashlib.sha256(data).hexdigest()
        size = len(data)
        storage_path = self._artifact_storage_path(artifact_path)
        await self._to_thread(self._write_bytes, storage_path, data)
        return Artifact(
            path=artifact_path,
            sha256=sha256,
            size=size,
            published_uri=storage_path,
        )

    # ------------------------------------------------------------------
    # Optional streaming (true chunked transfer)
    # ------------------------------------------------------------------

    def _read_chunk(self, f: Any) -> bytes:
        """Read one chunk from an open remote file handle."""
        return f.read(_STREAM_CHUNK_SIZE)

    async def _stream_path(self, path: str) -> AsyncIterator[bytes]:
        """Yield a remote object's content in bounded-size chunks.

        Each chunk read runs in the backend's thread pool so the event
        loop is never blocked.  For s3 filesystems, ranged reads
        (``cat_file(start, end)``) are used — s3fs's ``open``+``read``
        read-ahead caches the whole remaining object in memory; range
        GETs keep the working set at one chunk.  Memory stays bounded
        at one chunk regardless of object size.

        Args:
            path: Object path on the filesystem.

        Yields:
            Chunks of object content.

        Raises:
            KeyError: If the object does not exist.
        """
        protocol = self._fs.protocol
        protocols = protocol if isinstance(protocol, (tuple, list)) else (protocol,)
        if "s3" in protocols:
            try:
                size = await self._to_thread(self._fs.size, path)
            except FileNotFoundError as exc:
                raise KeyError(f"Object {path!r} not found") from exc
            offset = 0
            while offset < size:
                end = min(offset + _S3_RANGE_CHUNK_SIZE, size)
                chunk = await self._to_thread(self._fs.cat_file, path, start=offset, end=end)
                if not chunk:
                    break
                yield chunk
                offset += len(chunk)
            return

        try:
            f = await self._to_thread(self._fs.open, path, "rb")
        except FileNotFoundError as exc:
            raise KeyError(f"Object {path!r} not found") from exc
        try:
            while True:
                chunk = await self._to_thread(self._read_chunk, f)
                if not chunk:
                    break
                yield chunk
        finally:
            await self._to_thread(f.close)

    async def stream_blob(self, sha256: str) -> AsyncIterator[bytes]:
        """Yield blob content in chunks.

        Args:
            sha256: Content hash of the blob to stream.

        Yields:
            Chunks of blob content.

        Raises:
            KeyError: If the blob does not exist.
        """
        async for chunk in self._stream_path(self._blob_path(sha256)):
            yield chunk

    async def stream_checkpoint(self, checkpoint_id: str) -> AsyncIterator[bytes]:
        """Yield checkpoint content in chunks.

        Args:
            checkpoint_id: Unique checkpoint identifier.

        Yields:
            Chunks of checkpoint content.

        Raises:
            KeyError: If the checkpoint does not exist.
        """
        async for chunk in self._stream_path(self._checkpoint_path(checkpoint_id)):
            yield chunk

    async def stream_content_file(self, rel_path: str) -> AsyncIterator[bytes]:
        """Yield content-tree file content in chunks.

        Args:
            rel_path: Relative workspace path.

        Yields:
            Chunks of file content.

        Raises:
            KeyError: If the file does not exist.
        """
        async for chunk in self._stream_path(self._content_path(rel_path)):
            yield chunk

    # ------------------------------------------------------------------
    # Flat content-tree operations (content mirror / dual-source sync)
    # ------------------------------------------------------------------
    #
    # Unlike the CAS/manifest layout above, these address a PLAIN file tree
    # rooted at `root` (e.g. s3://<bucket>/workspaces/<wsId>/): paths map
    # 1:1 to relative workspace paths. Used by the content-mirror half of
    # dual-source sync: materialize workspace files at open, mirror dirty
    # file writes/deletes back at checkpoint.

    def _content_path(self, rel_path: str) -> str:
        """Map a relative workspace path to its content-tree storage path."""
        validated = validate_relative_path(rel_path, name="content_path")
        return f"{self._root}/{validated}"

    async def list_content_files(self) -> list[str]:
        """List all files in the content tree as relative workspace paths.

        Returns an empty list when the tree does not exist yet.
        """
        try:
            entries = await self._to_thread(self._fs.find, self._root)
        except FileNotFoundError:
            return []
        rel_paths: list[str] = []
        prefix = f"{self._root}/"
        for entry in entries:
            name = entry if isinstance(entry, str) else str(entry.get("name", ""))
            if not name:
                continue
            if name.endswith("/"):
                continue  # directory entries (some filesystems emit them)
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix) :]
            if rel:
                rel_paths.append(rel)
        rel_paths.sort()
        return rel_paths

    async def get_content_file(self, rel_path: str) -> bytes | None:
        """Return content-tree file bytes by relative path, or `None`."""
        path = self._content_path(rel_path)
        try:
            return await self._to_thread(self._fs.cat, path)
        except FileNotFoundError:
            return None

    async def put_content_file(self, rel_path: str, data: bytes) -> None:
        """Write bytes to the content-tree path (plain overwrite)."""
        path = self._content_path(rel_path)
        await self._to_thread(self._write_bytes, path, data)

    async def put_content_file_from_file(self, rel_path: str, src: Path) -> None:
        """Write a content-tree file by streaming a local source file.

        Args:
            rel_path: Relative workspace path.
            src: Local source file path.
        """
        path = self._content_path(rel_path)
        await self._to_thread(self._stream_from_file, path, src, expected_sha256=None)

    async def delete_content_file(self, rel_path: str) -> None:
        """Delete the content-tree object, if present (idempotent).

        Uses ``rm_file`` (single-object ``DeleteObject``) rather than
        ``rm`` (bulk ``DeleteObjects``) because some S3-compatible
        services (e.g. Tencent COS) reject ``DeleteObjects`` without a
        ``Content-MD5`` header that s3fs does not send.
        """
        path = self._content_path(rel_path)
        try:
            await self._to_thread(self._fs.rm_file, path)
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Shut down the dedicated thread pool.

        Queued-but-unstarted work is dropped; the workspace close sequence
        drains the uploader BEFORE closing the backend, so in-flight
        uploads have already completed. Idempotent.
        """
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def aclose(self) -> None:
        """Async close shim — see `close`."""
        self.close()

    async def __aenter__(self) -> FsspecSyncBackend:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.close()
