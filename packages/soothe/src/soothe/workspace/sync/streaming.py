"""Capability-dispatch helpers for streaming backend operations.

The `WorkspaceSyncBackend` protocol defines optional streaming
capabilities (`put_blob_from_file`, `put_content_file_from_file`,
`stream_content_file`, `stream_blob`).  Concrete backends implement them
when they can stream; duck-typed backends (and test doubles) that only
provide the buffered surface keep working through the buffered
fallbacks in this module.

All fallbacks offload file reads to `asyncio.to_thread` so the event
loop is never blocked by disk I/O, and never hold more than one chunk
in memory for the streaming paths.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from soothe.workspace.sync.workspace_sync import WorkspaceSyncBackend

logger = logging.getLogger(__name__)

_STREAM_READ_CHUNK = 1024 * 1024  # 1 MiB


async def put_blob_from_file(
    backend: WorkspaceSyncBackend,
    sha256: str,
    src: str | Path,
) -> None:
    """Store a blob by streaming a local file, with buffered fallback.

    Args:
        backend: Storage backend.
        sha256: Expected content hash.
        src: Local source file path.

    Raises:
        FileNotFoundError: If ``src`` does not exist.
        IntegrityError: Backend-raised, when the streamed content does
            not hash to ``sha256``.
    """
    src_path = Path(src)
    method = getattr(backend, "put_blob_from_file", None)
    if method is not None:
        await method(sha256, src_path)
        return
    data = await asyncio.to_thread(src_path.read_bytes)
    await backend.put_blob(sha256, data)


async def put_content_file_from_file(
    backend: WorkspaceSyncBackend,
    rel_path: str,
    src: str | Path,
) -> None:
    """Write a content-tree file by streaming, with buffered fallback.

    Args:
        backend: Content-tree backend.
        rel_path: Relative workspace path.
        src: Local source file path.
    """
    src_path = Path(src)
    method = getattr(backend, "put_content_file_from_file", None)
    if method is not None:
        await method(rel_path, src_path)
        return
    data = await asyncio.to_thread(src_path.read_bytes)
    await backend.put_content_file(rel_path, data)


async def iter_blob_chunks(
    backend: WorkspaceSyncBackend,
    sha256: str,
) -> AsyncIterator[bytes]:
    """Yield a blob's content in chunks, with buffered fallback.

    Args:
        backend: Storage backend.
        sha256: Content hash.

    Yields:
        Chunks of blob content.

    Raises:
        KeyError: If the blob does not exist.
    """
    method = getattr(backend, "stream_blob", None)
    if method is not None:
        async for chunk in method(sha256):
            yield chunk
        return
    data = await backend.get_blob(sha256)
    if data is None:
        raise KeyError(f"Blob {sha256!r} not found")
    for offset in range(0, len(data), _STREAM_READ_CHUNK):
        yield data[offset : offset + _STREAM_READ_CHUNK]


async def iter_content_chunks(
    backend: WorkspaceSyncBackend,
    rel_path: str,
) -> AsyncIterator[bytes]:
    """Yield a content-tree file's content in chunks, with fallback.

    Args:
        backend: Content-tree backend.
        rel_path: Relative workspace path.

    Yields:
        Chunks of file content.

    Raises:
        KeyError: If the file does not exist.
    """
    method = getattr(backend, "stream_content_file", None)
    if method is not None:
        async for chunk in method(rel_path):
            yield chunk
        return
    data = await backend.get_content_file(rel_path)
    if data is None:
        raise KeyError(f"Content file {rel_path!r} not found")
    for offset in range(0, len(data), _STREAM_READ_CHUNK):
        yield data[offset : offset + _STREAM_READ_CHUNK]


__all__ = [
    "iter_blob_chunks",
    "iter_content_chunks",
    "put_blob_from_file",
    "put_content_file_from_file",
]
