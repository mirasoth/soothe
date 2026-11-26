"""WorkspaceSyncBackend protocol — async workspace-to-storage abstraction.

Defines the data models and abstract protocol used to synchronize agent
workspaces with durable object-storage backends (S3-compatible, MinIO, GCS,
local filesystem, etc.) under the content-authoritative representation
(IG-CORE-010): the plain content tree is the single authoritative file store
and the manifest is the atomic commit record.

The protocol lives in soothe so host consumers can construct
``Manifest`` objects without importing host code, while concrete backend
implementations live in the host package (soothe).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

# ---------------------------------------------------------------------------
# Data models (wire contracts)
# ---------------------------------------------------------------------------


class ManifestEntry(BaseModel):
    """Single entry inside a :class:`Manifest` referencing one file.

    Args:
        path: Relative workspace path.
        sha256: Content hash.
        size: Content size in bytes.
    """

    path: str = Field(description="Relative workspace path.")
    sha256: str = Field(description="Content hash.")
    size: int = Field(description="Content size in bytes.")


class Manifest(BaseModel):
    """The commit record between a run and its storage backend.

    Lists the engine-owned artifact set (path → content hash) as of the
    last committed turn.  The plain content tree holds the bytes; this
    manifest is the atomic commit marker and the reconcile protection
    source (paths listed here are never reconciled away during content
    materialization).

    Args:
        run_id: Unique run identifier.
        version: Commit counter (optimistic concurrency).
        artifacts: Engine-owned files as of the last commit.
    """

    run_id: str = Field(description="Unique run identifier.")
    version: int = Field(
        default=1,
        description="Commit counter (optimistic concurrency).",
    )
    artifacts: list[ManifestEntry] = Field(
        default_factory=list,
        description="Engine-owned files as of the last commit.",
    )


# ---------------------------------------------------------------------------
# Protocol (abstract storage-backend interface)
# ---------------------------------------------------------------------------


@runtime_checkable
class WorkspaceSyncBackend(Protocol):
    """Async protocol for workspace-to-storage synchronization.

    Implementations provide the concrete transport (S3, GCS, local FS, ...)
    while keeping the workspace manager's dirty-tracking / commit algorithm
    backend-agnostic.  Two key spaces exist:

    * the *manifest root* (the backend's own ``root``) holding
      ``runs/<run_id>/manifest.json`` — the engine-private commit records;
    * the *content tree* — a plain file tree addressed by relative
      workspace path, holding the authoritative file bytes.

    Every method is async and idempotent.
    """

    # -- manifest operations (commit records) ------------------------------

    async def get_manifest(self, run_id: str) -> Manifest | None:
        """Fetch the latest manifest for *run_id*.

        Args:
            run_id: Target run identifier.

        Returns:
            The current manifest, or `None` if none exists yet.
        """
        ...

    async def put_manifest(
        self,
        run_id: str,
        manifest: Manifest,
        *,
        if_match: str | None = None,
    ) -> Manifest:
        """Write or update the manifest for *run_id* (the commit point).

        Args:
            run_id: Target run identifier.
            manifest: The manifest to write.  Its ``version`` field
                controls optimistic concurrency.
            if_match: If provided, the backend MUST reject the write
                when the stored manifest version does not match, or
                when a manifest was expected but none exists (version
                guard).

        Returns:
            The written manifest.
        """
        ...

    # -- content-tree operations (authoritative file bytes) -----------------

    async def list_content_files(self) -> list[str]:
        """List all files in the content tree as relative workspace paths.

        Returns:
            Sorted relative workspace paths; empty when the tree does
            not exist yet.
        """
        ...

    async def get_content_file(self, rel_path: str) -> bytes | None:
        """Return content-tree file bytes by relative path, or `None`.

        Args:
            rel_path: Relative workspace path.

        Returns:
            File content, or `None` if absent.
        """
        ...

    async def put_content_file(self, rel_path: str, data: bytes) -> None:
        """Write bytes to the content-tree path (plain overwrite).

        Args:
            rel_path: Relative workspace path.
            data: File content.
        """
        ...

    async def put_content_file_from_file(self, rel_path: str, src: Path) -> None:
        """Write a content-tree file by streaming a local file.

        Streaming counterpart of :meth:`put_content_file`; callers
        dispatch via
        :func:`soothe.workspace.sync.streaming.put_content_file_from_file`.

        Args:
            rel_path: Relative workspace path.
            src: Local source file path.
        """
        ...

    async def delete_content_file(self, rel_path: str) -> None:
        """Delete the content-tree object, if present (idempotent).

        Args:
            rel_path: Relative workspace path.
        """
        ...

    # -- optional streaming (default impls buffer fully) ---------------------

    async def stream_content_file(self, rel_path: str) -> AsyncIterator[bytes]:
        """Yield chunks of a content-tree file.

        Default implementation delegates to :meth:`get_content_file`
        and yields the full payload in one chunk.  Raise `KeyError`
        when the path is absent.  Callers dispatch via
        :func:`soothe.workspace.sync.streaming.iter_content_chunks`.

        Args:
            rel_path: Relative workspace path.

        Yields:
            Chunks of file content.
        """
        data = await self.get_content_file(rel_path)
        if data is None:
            raise KeyError(f"Content file {rel_path!r} not found")
        yield data

    # -- lifecycle ------------------------------------------------------------

    async def aclose(self) -> None:
        """Release backend resources (threads, connections).

        Called when the owning workspace closes. Backends without owned
        resources are no-ops; backends handed out per run by a factory
        MUST free their resources here, and factories that return shared
        backends MUST make this a no-op.
        """
        ...
