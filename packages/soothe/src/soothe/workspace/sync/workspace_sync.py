"""Data models and the async workspace-to-storage backend protocol.

One per-run backend root holds the plain content tree (the authoritative
file store) and the engine-private commit record at
`.soothe/manifest.json`. Concrete transports implement
`WorkspaceSyncBackend`; the workspace and turn facades stay transport-agnostic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class ManifestEntry(BaseModel):
    """Single entry inside a `Manifest` referencing one file.

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


@runtime_checkable
class WorkspaceSyncBackend(Protocol):
    """Async protocol for workspace-to-storage synchronization.

    Implementations provide the concrete transport (S3, GCS, local FS, ...)
    while keeping the workspace manager's dirty-tracking / commit algorithm
    backend-agnostic.  One backend instance serves one per-run root with
    two key spaces:

    * the *content tree* — a plain file tree addressed by relative
      workspace path, holding the authoritative file bytes;
    * the *engine-private* `.soothe/` key space holding
      `manifest.json` — the commit record.

    Every method is async and idempotent.
    """

    async def get_manifest(self, run_id: str) -> Manifest | None:
        """Fetch the latest manifest for `run_id`.

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
        """Write or update the manifest for `run_id` (the commit point).

        Args:
            run_id: Target run identifier.
            manifest: The manifest to write.  Its `version` field
                controls optimistic concurrency.
            if_match: If provided, the backend MUST reject the write
                when the stored manifest version does not match, or
                when a manifest was expected but none exists (version
                guard).

        Returns:
            The written manifest.
        """
        ...

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

        Streaming counterpart of `put_content_file`; callers
        dispatch via
        `soothe.workspace.sync.streaming.put_content_file_from_file`.

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

    async def stream_content_file(self, rel_path: str) -> AsyncIterator[bytes]:
        """Yield chunks of a content-tree file.

        Default implementation delegates to `get_content_file`
        and yields the full payload in one chunk.  Raise `KeyError`
        when the path is absent.  Callers dispatch via
        `soothe.workspace.sync.streaming.iter_content_chunks`.

        Args:
            rel_path: Relative workspace path.

        Yields:
            Chunks of file content.
        """
        data = await self.get_content_file(rel_path)
        if data is None:
            raise KeyError(f"Content file {rel_path!r} not found")
        yield data

    async def aclose(self) -> None:
        """Release backend resources (threads, connections).

        Called when the owning workspace closes. Backends without owned
        resources are no-ops; backends handed out per run by a factory
        MUST free their resources here, and factories that return shared
        backends MUST make this a no-op.
        """
        ...
