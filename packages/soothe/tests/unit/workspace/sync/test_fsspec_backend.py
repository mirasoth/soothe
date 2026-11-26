"""Tests for the ``FsspecSyncBackend`` using ``MemoryFileSystem``.

These tests verify the backend's correctness without requiring an actual
S3/MinIO/GCS endpoint.  ``MemoryFileSystem`` provides a zero-I/O in-process
filesystem that implements the full fsspec ``AbstractFileSystem`` interface.
"""

from __future__ import annotations

import hashlib

import pytest
from fsspec.implementations.memory import MemoryFileSystem

from soothe.workspace.sync.backends.fsspec import FsspecSyncBackend
from soothe.workspace.sync.errors import ConcurrentModificationError
from soothe.workspace.sync.workspace_sync import Manifest, ManifestEntry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ROOT = "/test-workspace-sync"


@pytest.fixture
def fs() -> MemoryFileSystem:
    """Fresh in-memory filesystem for each test."""
    mem = MemoryFileSystem()
    yield mem
    # Clean up after each test
    try:
        mem.rm(ROOT, recursive=True)
    except FileNotFoundError:
        pass
    mem.store.clear()


@pytest.fixture
def backend(fs: MemoryFileSystem) -> FsspecSyncBackend:
    """Fresh backend instance for each test."""
    b = FsspecSyncBackend(fs=fs, root=ROOT, max_workers=2)
    yield b
    b.close()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Manifest operations (commit records, optimistic concurrency)
# ---------------------------------------------------------------------------


class TestManifestOps:
    """Tests for get_manifest, put_manifest (optimistic concurrency)."""

    async def test_put_and_get_manifest(self, backend: FsspecSyncBackend) -> None:
        manifest = Manifest(
            run_id="run-001",
            version=1,
            artifacts=[ManifestEntry(path="input/paper.pdf", sha256="abc123", size=100)],
        )

        await backend.put_manifest("run-001", manifest)

        result = await backend.get_manifest("run-001")
        assert result is not None
        assert result.run_id == "run-001"
        assert result.version == 1
        assert len(result.artifacts) == 1
        assert result.artifacts[0].path == "input/paper.pdf"
        assert result.artifacts[0].sha256 == "abc123"
        assert result.artifacts[0].size == 100

    async def test_get_manifest_missing(self, backend: FsspecSyncBackend) -> None:
        result = await backend.get_manifest("nonexistent-run")
        assert result is None

    async def test_put_manifest_optimistic_concurrency_success(
        self,
        backend: FsspecSyncBackend,
    ) -> None:
        # Write version 1
        m1 = Manifest(run_id="run-002", version=1)
        await backend.put_manifest("run-002", m1)

        # Update to version 2 with if_match=1
        m2 = Manifest(run_id="run-002", version=2)
        result = await backend.put_manifest("run-002", m2, if_match="1")
        assert result.version == 2

    async def test_put_manifest_optimistic_concurrency_conflict(
        self,
        backend: FsspecSyncBackend,
    ) -> None:
        # Write version 1
        m1 = Manifest(run_id="run-003", version=1)
        await backend.put_manifest("run-003", m1)

        # Try to write version 2 with stale if_match=5
        m2 = Manifest(run_id="run-003", version=2)
        with pytest.raises(ConcurrentModificationError, match="version mismatch"):
            await backend.put_manifest("run-003", m2, if_match="5")

    async def test_put_manifest_if_match_requires_existing(
        self,
        backend: FsspecSyncBackend,
    ) -> None:
        """if_match on a run with no manifest is a lost race, not a create."""
        m = Manifest(run_id="run-ghost", version=1)
        with pytest.raises(ConcurrentModificationError, match="was expected"):
            await backend.put_manifest("run-ghost", m, if_match="0")


# ---------------------------------------------------------------------------
# Flat content-tree operations (authoritative file bytes)
# ---------------------------------------------------------------------------


class TestContentOps:
    """Tests for the flat content-tree primitives."""

    async def test_put_and_get_content_file(self, backend: FsspecSyncBackend) -> None:
        await backend.put_content_file("materials/notes.md", b"hello world")
        assert await backend.get_content_file("materials/notes.md") == b"hello world"

    async def test_get_content_file_missing(self, backend: FsspecSyncBackend) -> None:
        assert await backend.get_content_file("missing.txt") is None

    async def test_put_content_file_overwrites(self, backend: FsspecSyncBackend) -> None:
        await backend.put_content_file("materials/a.txt", b"v1")
        await backend.put_content_file("materials/a.txt", b"v2")
        assert await backend.get_content_file("materials/a.txt") == b"v2"

    async def test_list_content_files_sorted(self, backend: FsspecSyncBackend) -> None:
        await backend.put_content_file("output/b.md", b"b")
        await backend.put_content_file("output/a.md", b"a")
        await backend.put_content_file("input/c.txt", b"c")
        assert await backend.list_content_files() == [
            "input/c.txt",
            "output/a.md",
            "output/b.md",
        ]

    async def test_list_content_files_empty_when_missing(self, backend: FsspecSyncBackend) -> None:
        assert await backend.list_content_files() == []

    async def test_delete_content_file_idempotent(self, backend: FsspecSyncBackend) -> None:
        await backend.put_content_file("output/gone.md", b"x")
        await backend.delete_content_file("output/gone.md")
        assert await backend.get_content_file("output/gone.md") is None
        # Deleting a missing path is a no-op.
        await backend.delete_content_file("output/gone.md")

    async def test_put_content_file_from_file(self, backend: FsspecSyncBackend, tmp_path) -> None:
        src = tmp_path / "upload.bin"
        src.write_bytes(b"streamed payload")
        await backend.put_content_file_from_file("output/upload.bin", src)
        assert await backend.get_content_file("output/upload.bin") == b"streamed payload"


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class TestStreaming:
    """Tests for stream_content_file."""

    async def test_stream_content_file(self, backend: FsspecSyncBackend) -> None:
        data = b"y" * 200_000  # larger than chunk size
        await backend.put_content_file("output/big.bin", data)

        chunks: list[bytes] = []
        async for chunk in backend.stream_content_file("output/big.bin"):
            chunks.append(chunk)

        assert b"".join(chunks) == data

    async def test_stream_content_file_missing(self, backend: FsspecSyncBackend) -> None:
        with pytest.raises(KeyError):
            async for _chunk in backend.stream_content_file("missing.bin"):
                pass


# ---------------------------------------------------------------------------
# Security: path traversal prevention (S1)
# ---------------------------------------------------------------------------


class TestPathTraversalSecurity:
    """S1: path traversal protection in backend path construction."""

    async def test_put_content_file_rejects_traversal(self, backend: FsspecSyncBackend) -> None:
        with pytest.raises(ValueError, match="path traversal"):
            await backend.put_content_file("../../etc/passwd", b"data")

    async def test_get_content_file_rejects_absolute(self, backend: FsspecSyncBackend) -> None:
        with pytest.raises(ValueError, match="absolute"):
            await backend.get_content_file("/etc/passwd")

    async def test_delete_content_file_rejects_null_bytes(self, backend: FsspecSyncBackend) -> None:
        with pytest.raises(ValueError, match="null"):
            await backend.delete_content_file("output\x00evil.txt")

    async def test_manifest_path_rejects_traversal(
        self,
        backend: FsspecSyncBackend,
    ) -> None:
        m = Manifest(run_id="../escape", version=1)
        with pytest.raises(ValueError, match="invalid run_id"):
            await backend.put_manifest("../escape", m)
