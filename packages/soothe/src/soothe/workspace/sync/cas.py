"""Local content-addressed storage (CAS) cache.

Stores immutable blobs keyed by SHA-256.  Shared across runs on the same
host so repeated materialization of the same blob requires zero object-
data bandwidth.  Materialization uses reflink → copy (hardlink is probed
for capability reporting but never used — sharing the inode would let
in-place workspace edits corrupt the immutable cache).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import stat
import threading
import uuid
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from soothe.workspace.sync.paths import validate_path_component

if TYPE_CHECKING:
    from soothe.workspace.sync.workspace_sync import WorkspaceSyncBackend

logger = logging.getLogger(__name__)

_COPY_CHUNK_SIZE = 1024 * 1024  # 1 MiB


class LinkStrategy(StrEnum):
    """Materialization strategy for placing a CAS blob into the workspace.

    Attributes:
        REFLINK: Copy-on-write reflink.  Preferred when source and
            destination are on the same filesystem.
        HARDLINK: Hard link capability marker only — never used for
            materialization.  A hardlink shares the inode with the CAS
            blob, so an in-place edit of the workspace file would
            silently corrupt the immutable cache.
        COPY: Full byte copy.  Always works, but costs disk space.
    """

    REFLINK = "reflink"
    HARDLINK = "hardlink"
    COPY = "copy"


class CASCache:
    """Local content-addressed storage cache for workspace blobs.

    Immutable blobs are stored under `blobs/sha256/<first-2>/<hash>`.
    Shared across all runs on the same host.

    Args:
        cache_root: Directory where the CAS cache lives.

    Example:
        >>> cache = CASCache(cache_root="/data/agent-cache")
        >>> path = cache.store_blob("abc123...", b"hello")
    """

    def __init__(self, cache_root: str | Path) -> None:
        """Initialize the content-addressed blob cache and probe link strategy."""
        self._root = Path(cache_root)
        self._blobs_dir = self._root / "blobs" / "sha256"
        self._blobs_dir.mkdir(parents=True, exist_ok=True)
        self._link_strategy = _probe_link_strategy(self._root)
        logger.info(
            "CASCache initialized: root=%s, link_strategy=%s",
            self._root,
            self._link_strategy,
        )

    @property
    def root(self) -> Path:
        """Return the cache root directory."""
        return self._root

    @property
    def link_strategy(self) -> LinkStrategy:
        """Return the probed materialization strategy."""
        return self._link_strategy

    def blob_path(self, sha256: str) -> Path:
        """Return the local filesystem path for a blob.

        Args:
            sha256: Content hash (validated for path safety).

        Returns:
            `<root>/blobs/sha256/<first-2>/<hash>`
        """
        validate_path_component(sha256, name="sha256")
        return self._blobs_dir / sha256[:2] / sha256

    def has_blob(self, sha256: str) -> bool:
        """Return whether a blob is present in the local CAS cache."""
        path = self.blob_path(sha256)
        return path.is_file()

    def store_blob(self, sha256: str, data: bytes) -> Path:
        """Store raw bytes in the CAS cache under their content hash.

        Atomic write (write to temp, then rename).  Idempotent — no-op
        if the blob already exists.

        Args:
            sha256: Expected content hash (validated).
            data: Raw blob content.

        Returns:
            Path to the stored blob.

        Raises:
            IntegrityError: If the actual SHA-256 of `data` does not
                match `sha256`.
        """
        from soothe.workspace.sync.errors import IntegrityError

        validate_path_component(sha256, name="sha256")

        actual_hash = hashlib.sha256(data).hexdigest()
        if actual_hash != sha256:
            raise IntegrityError(
                f"CAS store hash mismatch: expected {sha256[:16]}..., got {actual_hash[:16]}..."
            )

        path = self.blob_path(sha256)
        if path.exists():
            return path  # idempotent

        path.parent.mkdir(parents=True, exist_ok=True)

        # Unique temp name: concurrent same-hash stores (multiple threads
        # or processes sharing the CAS root) must not interleave writes
        # or race on the rename.
        tmp_path = path.parent / (
            f"{path.name}.tmp-{os.getpid()}-{threading.get_ident():x}-{uuid.uuid4().hex[:8]}"
        )
        try:
            tmp_path.write_bytes(data)
            os.replace(tmp_path, path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        logger.debug("CAS stored blob: %s (%d bytes)", sha256[:12], len(data))
        return path

    def materialize(
        self,
        sha256: str,
        dest: str | Path,
    ) -> Path:
        """Materialize a CAS blob to a workspace path.

        Uses reflink when the filesystem supports it, otherwise a full
        copy.  Hardlink is never used: the workspace file and the CAS
        blob would share an inode, and an in-place edit of the workspace
        file would corrupt the immutable cache.  Creates the
        destination's parent directory if needed.

        Args:
            sha256: Content hash of the blob to materialize.
            dest: Destination path in the workspace.

        Returns:
            The destination `Path`.

        Raises:
            FileNotFoundError: If the blob is not in the CAS cache.
        """
        src = self.blob_path(sha256)
        if not src.exists():
            raise FileNotFoundError(f"CAS blob not found: {sha256[:12]}")

        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # If dest is a symlink, remove it (don't follow it).
        if dest_path.is_symlink():
            dest_path.unlink()

        if self._link_strategy is LinkStrategy.REFLINK:
            _reflink_or_copy(src, dest_path)
        else:
            _copy_file(src, dest_path)

        logger.debug(
            "CAS materialized: %s → %s (%s)",
            sha256[:12],
            dest_path,
            self._link_strategy,
        )
        return dest_path

    async def fetch_and_cache(
        self,
        sha256: str,
        backend: WorkspaceSyncBackend,
    ) -> Path | None:
        """Download a blob from the remote backend and cache it locally.

        Cache hit returns immediately.  On miss, streams the blob chunk
        by chunk into the cache (running hash verified on the way),
        so memory stays bounded for large blobs; backends without a
        streaming capability fall back to the buffered read.

        Args:
            sha256: Content hash to fetch.
            backend: Remote storage backend.

        Returns:
            Path to the cached blob, or `None` if the backend does not
            have the blob.
        """
        if self.has_blob(sha256):
            logger.debug("CAS cache hit: %s", sha256[:12])
            return self.blob_path(sha256)

        from soothe.workspace.sync.streaming import iter_blob_chunks

        path = self.blob_path(sha256)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.parent / f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        h = hashlib.sha256()
        # Chunks are awaited sequentially, so sequential writes through
        # one handle are race-free while staying off the event loop.
        f = await asyncio.to_thread(open, tmp_path, "wb")

        def _write(chunk: bytes) -> None:
            f.write(chunk)

        try:
            async for chunk in iter_blob_chunks(backend, sha256):
                await asyncio.to_thread(_write, chunk)
                h.update(chunk)
        except KeyError:
            await asyncio.to_thread(f.close)
            tmp_path.unlink(missing_ok=True)
            return None
        except Exception:
            await asyncio.to_thread(f.close)
            tmp_path.unlink(missing_ok=True)
            raise
        await asyncio.to_thread(f.close)

        if h.hexdigest() != sha256:
            tmp_path.unlink(missing_ok=True)
            from soothe.workspace.sync.errors import IntegrityError

            raise IntegrityError(
                f"CAS fetch hash mismatch for {sha256[:16]}: got {h.hexdigest()[:16]}..."
            )

        await asyncio.to_thread(os.replace, tmp_path, path)
        logger.debug("CAS streamed blob into cache: %s", sha256[:12])
        return path

    def cache_file(self, src: str | Path) -> tuple[str, Path]:
        """Cache a local file into CAS by streaming (single read pass).

        Copies the content to a temp file while hashing it, then moves
        the temp file to its content-addressed location.  When the blob
        already exists the temp copy is discarded — the hash still had
        to be computed to know the address, so one full read is the
        floor either way.

        Args:
            src: Local source file path.

        Returns:
            Tuple of (sha256 hexdigest, cached blob path).

        Raises:
            FileNotFoundError: If ``src`` does not exist.
        """
        src_path = Path(src)
        if not src_path.is_file():
            raise FileNotFoundError(f"cannot cache non-file: {src}")

        tmp_path = self._blobs_dir / f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        h = hashlib.sha256()
        try:
            with open(tmp_path, "wb") as fdst, open(src_path, "rb") as fsrc:
                while True:
                    chunk = fsrc.read(_COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    fdst.write(chunk)
                    h.update(chunk)
            sha256 = h.hexdigest()
            path = self.blob_path(sha256)
            if path.exists():
                tmp_path.unlink(missing_ok=True)
                return sha256, path
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_path, path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        logger.debug("CAS cached file: %s → %s", src_path, sha256[:12])
        return sha256, path

    def remove_blob(self, sha256: str) -> None:
        """Remove a blob from the CAS cache."""
        path = self.blob_path(sha256)
        path.unlink(missing_ok=True)

    def close(self) -> None:
        """No-op — the CAS cache uses the filesystem, no resources to close."""
        pass


# ---------------------------------------------------------------------------
# Filesystem capability probing
# ---------------------------------------------------------------------------


def _probe_link_strategy(cache_root: Path) -> LinkStrategy:
    """Probe the best available materialization strategy.

    Tests reflink first, then hardlink, then falls back to copy.

    Args:
        cache_root: The CAS cache root directory.

    Returns:
        The best available `LinkStrategy`.
    """
    src = cache_root / f".probe-{os.getpid()}.src"
    dst = cache_root / f".probe-{os.getpid()}.dst"

    try:
        src.write_bytes(b"probe")
        dst.unlink(missing_ok=True)

        if _try_reflink(src, dst):
            return LinkStrategy.REFLINK

        dst.unlink(missing_ok=True)

        try:
            os.link(src, dst)
            return LinkStrategy.HARDLINK
        except OSError:
            pass

        return LinkStrategy.COPY
    finally:
        src.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)


def _try_reflink(src: Path, dst: Path) -> bool:
    """Attempt a reflink (copy-on-write) from src to dst.

    Returns `True` on success, `False` if reflink is not supported.
    """
    if _try_reflink_darwin(src, dst):
        return True

    if _try_reflink_linux(src, dst):
        return True

    return False


def _try_reflink_darwin(src: Path, dst: Path) -> bool:
    """Attempt macOS clonefile(2) via ctypes."""
    try:
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

        clone_nofollow = 0x0001
        result = libc.clonefile(
            str(src).encode("utf-8"),
            str(dst).encode("utf-8"),
            clone_nofollow,
        )
        if result == 0:
            return True
        return False
    except Exception:
        return False


def _try_reflink_linux(src: Path, dst: Path) -> bool:
    """Attempt Linux FICLONE ioctl via fcntl."""
    try:
        import fcntl

        ficlone = 0x40049409

        with open(src, "rb") as src_fd, open(dst, "wb") as dst_fd:
            result = fcntl.ioctl(dst_fd.fileno(), ficlone, src_fd.fileno())
            if result == 0:
                return True
        return False
    except Exception:
        Path(dst).unlink(missing_ok=True)
        return False


def _reflink_or_copy(src: Path, dst: Path) -> None:
    """Try reflink, fall back to copy."""
    if not _try_reflink(src, dst):
        _copy_file(src, dst)


def _copy_file(src: Path, dst: Path) -> None:
    """Copy file contents in chunks."""
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            chunk = fsrc.read(_COPY_CHUNK_SIZE)
            if not chunk:
                break
            fdst.write(chunk)


# ---------------------------------------------------------------------------
# Symlink security
# ---------------------------------------------------------------------------


def is_symlink_escaping(path: str | Path, workspace_root: str | Path) -> bool:
    """Check whether a path is a symlink whose target escapes the workspace.

    Uses `os.lstat()` to detect symlinks without following them.  If the
    symlink target resolves outside the workspace root, returns `True`.

    Args:
        path: The file path to check.
        workspace_root: The workspace root directory.

    Returns:
        `True` if the path is an escaping symlink.
    """
    p = Path(path)
    if not p.is_symlink():
        return False

    workspace_root = Path(workspace_root).resolve()
    try:
        target = p.resolve(strict=False)
    except OSError:
        return True  # unresolvable symlink → treat as escaping

    try:
        target.relative_to(workspace_root)
        return False  # target is inside workspace
    except ValueError:
        return True  # target escapes workspace


def scan_escaping_symlinks(directory: str | Path, workspace_root: str | Path) -> list[Path]:
    """Scan a directory tree for symlinks escaping the workspace root.

    Args:
        directory: Directory to scan recursively.
        workspace_root: The workspace root directory.

    Returns:
        List of symlink paths whose targets escape the workspace.
    """
    escaping: list[Path] = []
    root = Path(workspace_root).resolve()

    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        for name in dirnames + filenames:
            full = Path(dirpath) / name
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                try:
                    target = full.resolve(strict=False)
                    target.relative_to(root)
                except (ValueError, OSError):
                    escaping.append(full)

    return escaping
