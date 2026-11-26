"""Tests for the CAS (content-addressed storage) cache.

These tests verify blob storage, retrieval, materialization strategies,
and symlink security (S2) using a temporary directory on the local
filesystem.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from soothe.workspace.sync.cas import (
    CASCache,
    LinkStrategy,
    is_symlink_escaping,
    scan_escaping_symlinks,
)
from soothe.workspace.sync.errors import IntegrityError


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    """Temporary CAS cache directory."""
    d = tmp_path / "agent-cache"
    d.mkdir()
    return d


@pytest.fixture
def cache(cache_dir: Path) -> CASCache:
    """Fresh CAS cache instance."""
    return CASCache(cache_dir)


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    """Temporary workspace directory."""
    d = tmp_path / "workspace"
    (d / "input").mkdir(parents=True)
    (d / "output").mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# Blob storage tests
# ---------------------------------------------------------------------------


class TestCASStoreBlob:
    """Tests for store_blob, has_blob, blob_path."""

    def test_blob_path_layout(self, cache: CASCache) -> None:
        """Blob path follows the sha256/<first-2>/<hash> layout."""
        h = _sha256(b"test")
        path = cache.blob_path(h)
        assert path.parent.name == h[:2]
        assert path.parent.parent.name == "sha256"
        assert path.name == h

    def test_store_and_has_blob(self, cache: CASCache) -> None:
        data = b"hello world"
        h = _sha256(data)
        assert not cache.has_blob(h)
        cache.store_blob(h, data)
        assert cache.has_blob(h)

    def test_store_blob_idempotent(self, cache: CASCache) -> None:
        data = b"test content"
        h = _sha256(data)
        cache.store_blob(h, data)
        cache.store_blob(h, data)  # second call is no-op
        assert cache.has_blob(h)

    def test_store_blob_integrity_mismatch(self, cache: CASCache) -> None:
        """S7: store_blob rejects data whose hash doesn't match."""
        real_hash = _sha256(b"real data")
        with pytest.raises(IntegrityError, match="hash mismatch"):
            cache.store_blob(real_hash, b"wrong data")
        assert not cache.has_blob(real_hash)

    def test_store_blob_path_traversal_rejected(self, cache: CASCache) -> None:
        """S1: sha256 with path traversal is rejected."""
        with pytest.raises(ValueError):
            cache.store_blob("../../etc/passwd", b"data")

    def test_blob_path_traversal_rejected(self, cache: CASCache) -> None:
        """S1: blob_path validates the hash."""
        with pytest.raises(ValueError):
            cache.blob_path("../escape")


# ---------------------------------------------------------------------------
# Materialization tests
# ---------------------------------------------------------------------------


class TestCASMaterialize:
    """Tests for materialize (reflink → hardlink → copy)."""

    def test_materialize_creates_file(self, cache: CASCache, workspace_dir: Path) -> None:
        data = b"materialize me"
        h = _sha256(data)
        cache.store_blob(h, data)

        dest = workspace_dir / "input" / "paper.pdf"
        cache.materialize(h, dest)

        assert dest.is_file()
        assert dest.read_bytes() == data

    def test_materialize_creates_parent_dirs(self, cache: CASCache, workspace_dir: Path) -> None:
        data = b"deep file"
        h = _sha256(data)
        cache.store_blob(h, data)

        dest = workspace_dir / "input" / "sub" / "dir" / "file.txt"
        cache.materialize(h, dest)
        assert dest.is_file()

    def test_materialize_missing_blob_raises(self, cache: CASCache, workspace_dir: Path) -> None:
        h = _sha256(b"nonexistent")
        dest = workspace_dir / "input" / "file.txt"
        with pytest.raises(FileNotFoundError, match="CAS blob not found"):
            cache.materialize(h, dest)

    def test_materialize_overwrites_symlink(self, cache: CASCache, workspace_dir: Path) -> None:
        """S2: materialize replaces symlinks at destination."""
        data = b"real content"
        h = _sha256(data)
        cache.store_blob(h, data)

        dest = workspace_dir / "input" / "file.txt"
        # Create a symlink at the destination.
        link_target = workspace_dir / "input" / "other.txt"
        link_target.write_text("other")
        os.symlink(link_target, dest)

        cache.materialize(h, dest)
        assert not dest.is_symlink()
        assert dest.read_bytes() == data

    def test_link_strategy_probed(self, cache: CASCache) -> None:
        """CASCache probes the link strategy at init."""
        assert cache.link_strategy in LinkStrategy

    def test_materialized_file_does_not_corrupt_cache(
        self, cache: CASCache, workspace_dir: Path
    ) -> None:
        """F-C5 regression: editing a materialized file leaves the cache intact.

        Hardlinks share the inode with the cached blob — an in-place
        workspace edit would corrupt the immutable cache.  Materialize
        must therefore never hand out a hardlink.
        """
        data = b"immutable original" * 100
        h = _sha256(data)
        cache.store_blob(h, data)
        blob_path = cache.blob_path(h)

        dest = workspace_dir / "input" / "edit-me.txt"
        cache.materialize(h, dest)
        assert not (
            dest.is_file() and blob_path.is_file() and dest.stat().st_ino == blob_path.stat().st_ino
        ), "materialize handed out a hardlink — workspace edits would corrupt the CAS"

        # In-place edit of the workspace copy.
        with open(dest, "r+b") as f:
            f.seek(0)
            f.write(b"MUTATED!!")

        # The cached blob is unchanged.
        assert blob_path.read_bytes() == data
        assert cache.store_blob(h, data) == blob_path  # idempotent, no error


# ---------------------------------------------------------------------------
# Streaming tests
# ---------------------------------------------------------------------------


class TestCASStreaming:
    """Tests for cache_file (single-pass hash + copy) and streaming fetch."""

    def test_cache_file_hashes_and_copies(self, cache: CASCache, workspace_dir: Path) -> None:
        src = workspace_dir / "output" / "big.bin"
        payload = os.urandom(3 * 1024 * 1024 + 7)  # multi-chunk
        src.write_bytes(payload)

        sha, blob_path = cache.cache_file(src)
        assert sha == _sha256(payload)
        assert blob_path.is_file()
        assert blob_path.read_bytes() == payload

    def test_cache_file_idempotent_keeps_existing(
        self, cache: CASCache, workspace_dir: Path
    ) -> None:
        payload = b"same content"
        src = workspace_dir / "output" / "a.txt"
        src.write_bytes(payload)
        sha1, path1 = cache.cache_file(src)
        sha2, path2 = cache.cache_file(src)
        assert sha1 == sha2
        assert path1 == path2

    def test_cache_file_rejects_missing(self, cache: CASCache, workspace_dir: Path) -> None:
        with pytest.raises(FileNotFoundError):
            cache.cache_file(workspace_dir / "output" / "ghost.bin")

    @pytest.mark.asyncio
    async def test_fetch_and_cache_streams_from_backend(
        self, cache: CASCache, workspace_dir: Path
    ) -> None:
        payload = os.urandom(1024 * 1024 + 123)
        h = _sha256(payload)

        class _StreamingBackend:
            async def stream_blob(self, sha256: str):
                for offset in range(0, len(payload), 65536):
                    yield payload[offset : offset + 65536]

        path = await cache.fetch_and_cache(h, _StreamingBackend())
        assert path is not None
        assert path.read_bytes() == payload

    @pytest.mark.asyncio
    async def test_fetch_and_cache_missing_returns_none(self, cache: CASCache) -> None:
        class _MissingBackend:
            async def stream_blob(self, sha256: str):
                raise KeyError(sha256)
                yield b""  # pragma: no cover — makes this an async generator

        h = _sha256(b"never-stored")
        assert await cache.fetch_and_cache(h, _MissingBackend()) is None

    @pytest.mark.asyncio
    async def test_fetch_and_cache_hash_mismatch_raises(self, cache: CASCache) -> None:
        payload = b"tampered in transit"

        class _LyingBackend:
            async def stream_blob(self, sha256: str):
                yield payload

        from soothe.workspace.sync.errors import IntegrityError

        h = _sha256(b"claimed different content")
        with pytest.raises(IntegrityError, match="hash mismatch"):
            await cache.fetch_and_cache(h, _LyingBackend())


# ---------------------------------------------------------------------------
# Symlink security tests (S2)
# ---------------------------------------------------------------------------


class TestSymlinkSecurity:
    """Tests for S2: symlink escape detection."""

    def test_is_symlink_escaping_false_for_regular_file(self, workspace_dir: Path) -> None:
        f = workspace_dir / "input" / "file.txt"
        f.write_text("content")
        assert not is_symlink_escaping(f, workspace_dir)

    def test_is_symlink_escaping_true_for_external_link(
        self, workspace_dir: Path, tmp_path: Path
    ) -> None:
        """Symlink to /etc/passwd escapes the workspace."""
        external = tmp_path / "secret.txt"
        external.write_text("secret")
        link = workspace_dir / "input" / "evil.txt"
        os.symlink(external, link)
        assert is_symlink_escaping(link, workspace_dir)

    def test_is_symlink_escaping_false_for_internal_link(self, workspace_dir: Path) -> None:
        """Symlink within the workspace is allowed."""
        target = workspace_dir / "input" / "real.txt"
        target.write_text("content")
        link = workspace_dir / "input" / "link.txt"
        os.symlink(target, link)
        assert not is_symlink_escaping(link, workspace_dir)

    def test_scan_escaping_symlinks_finds_all(self, workspace_dir: Path, tmp_path: Path) -> None:
        """Scan finds all escaping symlinks in a directory tree."""
        external = tmp_path / "external"
        external.mkdir()
        (external / "secret1.txt").write_text("s1")
        (external / "secret2.txt").write_text("s2")

        # Create escaping symlinks.
        os.symlink(external / "secret1.txt", workspace_dir / "input" / "evil1.txt")
        os.symlink(external / "secret2.txt", workspace_dir / "input" / "evil2.txt")

        # Create a safe internal symlink.
        safe_target = workspace_dir / "input" / "real.txt"
        safe_target.write_text("safe")
        os.symlink(safe_target, workspace_dir / "input" / "safe_link.txt")

        escaping = scan_escaping_symlinks(workspace_dir, workspace_dir)
        assert len(escaping) == 2
        paths = {p.name for p in escaping}
        assert paths == {"evil1.txt", "evil2.txt"}
