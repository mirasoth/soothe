"""Integration tests for `FsspecSyncBackend` against a real S3-compatible endpoint.

These tests exercise the content-authoritative workspace-sync backend
(manifest optimistic concurrency, content-tree ops, streaming) against a
live object-store.  Any S3-compatible service works: AWS S3, MinIO,
Tencent COS, Cloudflare R2, etc.

Credentials are read **only** from environment variables — never hardcoded.

Required environment variables::

    SOOTHE_TEST_S3_ENDPOINT   # e.g. https://cos.ap-nanjing.myqcloud.com
    SOOTHE_TEST_S3_BUCKET     # e.g. soothe-test-1537107313
    SOOTHE_TEST_S3_KEY        # access key id
    SOOTHE_TEST_S3_SECRET     # secret access key

Optional::

    SOOTHE_TEST_S3_REGION     # e.g. ap-nanjing (defaults to "auto")

Run::

    SOOTHE_TEST_S3_ENDPOINT=https://cos.ap-nanjing.myqcloud.com \
    SOOTHE_TEST_S3_BUCKET=soothe-test-1537107313 \
    SOOTHE_TEST_S3_KEY=... \
    SOOTHE_TEST_S3_SECRET=... \
    uv run pytest --run-integration tests/integration/workspace/sync/test_s3_integration.py -v
"""

from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any

import pytest

from soothe.workspace.sync.backends.fsspec import FsspecSyncBackend
from soothe.workspace.sync.errors import ConcurrentModificationError
from soothe.workspace.sync.factory import construct_sync_backend
from soothe.workspace.sync.workspace_sync import Manifest, ManifestEntry

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

_ENDPOINT = os.environ.get("SOOTHE_TEST_S3_ENDPOINT", "")
_BUCKET = os.environ.get("SOOTHE_TEST_S3_BUCKET", "")
_KEY = os.environ.get("SOOTHE_TEST_S3_KEY", "")
_SECRET = os.environ.get("SOOTHE_TEST_S3_SECRET", "")
_REGION = os.environ.get("SOOTHE_TEST_S3_REGION", "auto")


def _factory_config() -> dict[str, object]:
    """Storage options compatible with `construct_sync_backend`'s allowlist.

    Includes `config_kwargs` and `client_kwargs` so the factory can
    configure virtual-hosted-style addressing and region for S3-compatible
    services like Tencent COS.

    `request_checksum_calculation="when_required"` disables botocore's
    default integrity checksums (1.36+): those switch request bodies to
    aws-chunked framing, which Tencent COS rejects with `You must
    provide the Content-Length HTTP header`.  End-to-end integrity is
    provided by the content-tree manifest (sha256 on every artifact
    entry), not transport checksums.
    """
    return {
        "endpoint_url": _ENDPOINT,
        "key": _KEY,
        "secret": _SECRET,
        "config_kwargs": {
            "s3": {"addressing_style": "virtual"},
            "request_checksum_calculation": "when_required",
        },
        "client_kwargs": {"region_name": _REGION},
    }


def _is_configured() -> bool:
    return bool(_ENDPOINT and _BUCKET and _KEY and _SECRET)


def _cleanup_prefix(fs: Any, root: str) -> None:
    """Best-effort cleanup of all objects under `root`.

    Tencent COS rejects `DeleteObjects` (bulk delete) because s3fs does
    not send `Content-MD5`.  We fall back to individual `rm` calls
    which use `DeleteObject` (no MD5 required).
    """
    try:
        entries = fs.find(root)
    except (FileNotFoundError, OSError):
        return
    for entry in entries:
        name = entry if isinstance(entry, str) else str(entry.get("name", ""))
        if not name:
            continue
        try:
            fs.rm_file(name)
        except Exception:
            pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_s3_fs() -> Any:
    """Construct a real `S3FileSystem` configured for Tencent COS.

    Tencent COS requires:
    - Virtual-hosted-style addressing (`addressing_style='virtual'`)
      so the bucket name becomes a subdomain of the regional endpoint.
    - `Content-MD5` on `DeleteObjects` (bulk delete); s3fs does not
      send it, so recursive `rm` fails.  Cleanup uses individual
      `rm_file` calls instead.
    """
    import s3fs  # type: ignore[import-untyped]

    return s3fs.S3FileSystem(
        key=_KEY,
        secret=_SECRET,
        config_kwargs={"s3": {"addressing_style": "virtual"}},
        client_kwargs={
            "endpoint_url": _ENDPOINT,
            "region_name": _REGION,
        },
    )


# Unique per-session prefix to avoid collisions with previous runs.
_SESSION_PREFIX = f"soothe-test/{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def s3_uri() -> str:
    """Return an S3 URI with a session-unique prefix under the test bucket."""
    return f"s3://{_BUCKET}/{_SESSION_PREFIX}"


@pytest.fixture
def s3_root(s3_uri: str) -> str:
    """Root path within the S3 filesystem (`<bucket>/<prefix>`).

    For S3, the root passed to `FsspecSyncBackend` must include the
    bucket name so that fsspec path operations resolve correctly.
    """
    return s3_uri[len("s3://") :]


@pytest.fixture
def backend(s3_root: str) -> FsspecSyncBackend:
    """Construct a real `FsspecSyncBackend` connected to the live endpoint.

    Constructs `S3FileSystem` directly so `region_name` can be passed
    via `client_kwargs` (the factory's allowlist does not include
    `client_kwargs`).  Skips when env vars are not set.
    """
    if not _is_configured():
        pytest.skip(
            "Set SOOTHE_TEST_S3_ENDPOINT, SOOTHE_TEST_S3_BUCKET, "
            "SOOTHE_TEST_S3_KEY, SOOTHE_TEST_S3_SECRET to run S3 integration tests"
        )
    fs = _make_s3_fs()
    fs.use_cache = False
    b = FsspecSyncBackend(fs=fs, root=s3_root)
    yield b
    _cleanup_prefix(fs, s3_root)
    b.close()


@pytest.fixture
def run_id() -> str:
    """Unique run ID per test to avoid cross-test interference."""
    return f"run-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Manifest operations (commit records, optimistic concurrency)
# ---------------------------------------------------------------------------


class TestS3ManifestOps:
    """Manifest write/read and optimistic concurrency control."""

    @pytest.mark.asyncio
    async def test_put_get_manifest(
        self,
        backend: FsspecSyncBackend,
        run_id: str,
    ) -> None:
        manifest = Manifest(
            run_id=run_id,
            version=1,
            artifacts=[ManifestEntry(path="input/data.csv", sha256="abc123", size=100)],
        )
        await backend.put_manifest(run_id, manifest)

        result = await backend.get_manifest(run_id)
        assert result is not None
        assert result.run_id == run_id
        assert result.version == 1
        assert len(result.artifacts) == 1
        assert result.artifacts[0].path == "input/data.csv"

    @pytest.mark.asyncio
    async def test_get_manifest_missing(self, backend: FsspecSyncBackend) -> None:
        result = await backend.get_manifest("nonexistent-run-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_manifest_optimistic_concurrency_success(
        self,
        backend: FsspecSyncBackend,
        run_id: str,
    ) -> None:
        m1 = Manifest(run_id=run_id, version=1)
        await backend.put_manifest(run_id, m1)

        m2 = Manifest(run_id=run_id, version=2)
        result = await backend.put_manifest(run_id, m2, if_match="1")
        assert result.version == 2

    @pytest.mark.asyncio
    async def test_manifest_optimistic_concurrency_conflict(
        self,
        backend: FsspecSyncBackend,
        run_id: str,
    ) -> None:
        m1 = Manifest(run_id=run_id, version=1)
        await backend.put_manifest(run_id, m1)

        m2 = Manifest(run_id=run_id, version=2)
        with pytest.raises(ConcurrentModificationError, match="version mismatch"):
            await backend.put_manifest(run_id, m2, if_match="99")


# ---------------------------------------------------------------------------
# Content-tree operations (authoritative file bytes)
# ---------------------------------------------------------------------------


class TestS3ContentTree:
    """Plain file-tree CRUD used by the content-authoritative sync."""

    @pytest.mark.asyncio
    async def test_put_get_content_file(self, backend: FsspecSyncBackend) -> None:
        data = b"content tree file"
        await backend.put_content_file("src/main.py", data)

        result = await backend.get_content_file("src/main.py")
        assert result == data

    @pytest.mark.asyncio
    async def test_get_content_file_missing(self, backend: FsspecSyncBackend) -> None:
        assert await backend.get_content_file("nonexistent.py") is None

    @pytest.mark.asyncio
    async def test_list_content_files(self, backend: FsspecSyncBackend) -> None:
        await backend.put_content_file("a.txt", b"a")
        await backend.put_content_file("dir/b.txt", b"b")
        await backend.put_content_file("dir/c.txt", b"c")

        files = await backend.list_content_files()
        assert "a.txt" in files
        assert "dir/b.txt" in files
        assert "dir/c.txt" in files

    @pytest.mark.asyncio
    async def test_delete_content_file(self, backend: FsspecSyncBackend) -> None:
        await backend.put_content_file("temp.txt", b"x")
        assert await backend.get_content_file("temp.txt") is not None

        await backend.delete_content_file("temp.txt")
        assert await backend.get_content_file("temp.txt") is None

    @pytest.mark.asyncio
    async def test_delete_content_file_idempotent(self, backend: FsspecSyncBackend) -> None:
        await backend.delete_content_file("never-existed.txt")


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class TestS3Streaming:
    """Chunked streaming reads for content-tree files."""

    @pytest.mark.asyncio
    async def test_stream_content_file(self, backend: FsspecSyncBackend) -> None:
        data = b"S" * 20_000_000  # exercises the ranged multi-chunk path
        await backend.put_content_file("output/big.bin", data)

        chunks: list[bytes] = []
        async for chunk in backend.stream_content_file("output/big.bin"):
            chunks.append(chunk)

        assert b"".join(chunks) == data

    @pytest.mark.asyncio
    async def test_stream_content_file_missing(self, backend: FsspecSyncBackend) -> None:
        with pytest.raises(KeyError):
            async for _chunk in backend.stream_content_file("missing.bin"):
                pass


# ---------------------------------------------------------------------------
# Factory integration
# ---------------------------------------------------------------------------


class TestS3Factory:
    """Verify `construct_sync_backend` produces a working backend from a URI."""

    @pytest.mark.asyncio
    async def test_factory_construct_and_roundtrip(
        self,
        s3_uri: str,
    ) -> None:
        if not _is_configured():
            pytest.skip("S3 integration env vars not set")

        backend = construct_sync_backend(s3_uri, config=_factory_config())
        try:
            data = b"factory roundtrip"
            await backend.put_content_file("factory/roundtrip.txt", data)
            assert await backend.get_content_file("factory/roundtrip.txt") == data
        finally:
            assert isinstance(backend, FsspecSyncBackend)
            _cleanup_prefix(backend._fs, backend._root)
            backend.close()

    def test_factory_rejects_local_scheme(self) -> None:
        """SSRF guard: only s3/gs/az schemes are accepted."""
        with pytest.raises(ValueError, match="unsupported workspace_sync_source scheme"):
            construct_sync_backend("file:///etc/passwd")
