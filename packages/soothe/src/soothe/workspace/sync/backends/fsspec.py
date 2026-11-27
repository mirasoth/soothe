"""Concrete `WorkspaceSyncBackend` backed by any fsspec filesystem.

One adapter supports S3 (`s3fs`), GCS (`gcsfs`), Azure Blob (`adlfs`),
local, and memory filesystems. All fsspec calls run in a dedicated thread
pool. One instance serves one per-run root: the plain content tree at
`<root>/<relative workspace path>` and the commit record at
`<root>/.soothe/manifest.json`.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from soothe.workspace.sync.errors import ConcurrentModificationError
from soothe.workspace.sync.paths import validate_path_component, validate_relative_path
from soothe.workspace.sync.workspace_sync import Manifest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

_DEFAULT_MAX_WORKERS = 8
_STREAM_CHUNK_SIZE = 65536

# s3fs's default put chunksize (50 MiB) makes it whole-buffer any file
# smaller than 100 MiB; an explicit 8 MiB chunksize switches to
# multipart (each part a known-length bytes buffer — required by S3
# compatible stores like Tencent COS that reject chunked
# Transfer-Encoding) and caps per-part memory.
_S3_PUT_CHUNK_SIZE = 8 * 1024 * 1024
# s3fs defaults to max_concurrency=10 upload parts in flight; with the
# 8 MiB chunk size that is 80 MiB of raw chunk buffers plus serializer
# copies. 2 keeps the in-flight window at ~16 MiB.
_S3_PUT_MAX_CONCURRENCY = 2
# Range-GET size for streaming reads from s3 (same sizing rationale).
_S3_RANGE_CHUNK_SIZE = 8 * 1024 * 1024

# Filesystems whose writes require the parent directory to exist.
# Object stores (s3/gs/az) create keys under implicit prefixes — calling
# `makedirs` there only costs a bucket probe per write.
_DIR_BEARING_PROTOCOLS = frozenset({"local", "file", "memory", "dir"})


class FsspecSyncBackend:
    """`WorkspaceSyncBackend` backed by an fsspec `AbstractFileSystem`.

    Args:
        fs: An fsspec filesystem instance (e.g. `MemoryFileSystem`,
            `S3FileSystem`, `LocalFileSystem`).
        root: Per-run root path within the filesystem — everything the
            engine addresses for that run lives under it (content tree +
            `.soothe/` commit records).
        max_workers: Thread pool size for I/O offloading.
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

    def _manifest_path(self, run_id: str) -> str:
        """Map a run ID to its manifest storage path.

        The backend is per-run rooted, so the manifest lives under the
        engine-private `.soothe/` key space of the run's own root —
        `run_id` is validated for misuse defense but not part of the path.

        Args:
            run_id: Run identifier (validated, unused for pathing).

        Returns:
            The manifest key under this backend's root.
        """
        validate_path_component(run_id, name="run_id")
        return f"{self._root}/.soothe/manifest.json"

    def _content_path(self, rel_path: str) -> str:
        """Map a relative workspace path to its content-tree storage path."""
        validated = validate_relative_path(rel_path, name="content_path")
        return f"{self._root}/{validated}"

    def _ensure_parent_dir(self, path: str) -> None:
        """Create the parent directory for `path` when required.

        Object stores (s3/gs/az) create keys implicitly; directory-
        bearing filesystems (local/memory) need explicit parents.
        """
        if not self._needs_parent_dirs:
            return
        parent = path.rsplit("/", 1)[0]
        if parent:
            self._fs.makedirs(parent, exist_ok=True)

    def _write_bytes(self, path: str, data: bytes) -> None:
        """Write bytes to `path`, creating parents when required."""
        self._ensure_parent_dir(path)
        self._fs.pipe(path, data)

    def _stream_from_file(self, dest: str, src: Path) -> None:
        """Copy a local file to `dest` with bounded memory.

        Runs as a single thread job.  The transfer uses
        `fs.put(src, dest)` — the source size is known up front, so
        object stores can set Content-Length and use multipart upload
        (s3fs's `open('wb')` incremental writes send chunked
        Transfer-Encoding, which S3-compatible stores like Tencent COS
        reject).

        Args:
            dest: Destination path on the filesystem.
            src: Local source file path.
        """
        self._ensure_parent_dir(dest)
        protocol = self._fs.protocol
        protocols = protocol if isinstance(protocol, (tuple, list)) else (protocol,)
        put_kwargs: dict[str, Any] = {}
        if "s3" in protocols:
            put_kwargs["chunksize"] = _S3_PUT_CHUNK_SIZE
            put_kwargs["max_concurrency"] = _S3_PUT_MAX_CONCURRENCY
        self._fs.put(str(src), dest, **put_kwargs)

    async def _to_thread(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a sync function in the dedicated thread pool."""
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, lambda: fn(*args, **kwargs)
        )

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
        """Write or update the manifest for `run_id` (the commit point).

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

    def _read_chunk(self, f: Any) -> bytes:
        """Read one chunk from an open remote file handle."""
        return f.read(_STREAM_CHUNK_SIZE)

    async def _stream_path(self, path: str) -> AsyncIterator[bytes]:
        """Yield a remote object's content in bounded-size chunks.

        Each chunk read runs in the backend's thread pool so the event
        loop is never blocked.  For s3 filesystems, ranged reads
        (`cat_file(start, end)`) are used — s3fs's `open`+`read`
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

    async def list_content_files(self) -> list[str]:
        """List all files in the content tree as relative workspace paths.

        Returns an empty list when the tree does not exist yet.  The
        engine-private `.soothe/` key space (commit records) is not part
        of the content tree and is excluded.
        """
        try:
            entries = await self._to_thread(self._fs.find, self._root)
        except FileNotFoundError:
            return []
        rel_paths: list[str] = []
        prefix = f"{self._root}/"
        skip_prefixes = (f"{self._root}/.soothe/",)
        for entry in entries:
            name = entry if isinstance(entry, str) else str(entry.get("name", ""))
            if not name:
                continue
            if name.endswith("/"):
                continue
            if not name.startswith(prefix):
                continue
            if name.startswith(skip_prefixes):
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
        await self._to_thread(self._stream_from_file, path, src)

    async def delete_content_file(self, rel_path: str) -> None:
        """Delete the content-tree object, if present (idempotent).

        Uses `rm_file` (single-object `DeleteObject`) rather than
        `rm` (bulk `DeleteObjects`) because some S3-compatible
        services (e.g. Tencent COS) reject `DeleteObjects` without a
        `Content-MD5` header that s3fs does not send.
        """
        path = self._content_path(rel_path)
        try:
            await self._to_thread(self._fs.rm_file, path)
        except FileNotFoundError:
            pass

    def close(self) -> None:
        """Shut down the dedicated thread pool (idempotent).

        Queued-but-unstarted work is dropped. The workspace commits before
        closing the backend, so in-flight uploads have already completed.
        """
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def aclose(self) -> None:
        """Async close shim — see `close`."""
        self.close()

    async def __aenter__(self) -> FsspecSyncBackend:
        """Return this backend as the async context manager value."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Close the backend on context exit."""
        self.close()
