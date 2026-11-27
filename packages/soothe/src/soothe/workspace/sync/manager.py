"""Workspace manager — owns the open-workspace set for a sync root.

Creates workspace directories, wires each run's `DirtyTracker`, builds the
per-run backend, and loads the last committed manifest at open so a fresh
process never resets manifest state.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from soothe.workspace.sync.dirty_tracker import DirtyTracker
from soothe.workspace.sync.reserved import merge_watched_subdirs
from soothe.workspace.sync.workspace import Workspace

if TYPE_CHECKING:
    from collections.abc import Sequence

    from soothe.workspace.sync.workspace_sync import Manifest, WorkspaceSyncBackend

logger = logging.getLogger(__name__)

# Run IDs double as workspace directory names, so they must be one safe
# path component.
_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


class WorkspaceManager:
    """Owns the open workspaces for one sync root.

    `open` builds per-run resources; `close` and `close_all` release them.
    Hosts obtain `Workspace` handles from `open` and serialize turns per
    run id.

    Args:
        workspaces_root: Local root directory holding per-run workspaces.
        backend_factory: Maps a run id to its backend — one instance per
            run serving both the content tree and the manifest. `None`
            opens the run local-only.
        watched_subdirs: Additional workspace-relative subtrees tracked
            by the dirty tracker beyond `input`/`working`/`output`.
    """

    def __init__(
        self,
        *,
        workspaces_root: Path,
        backend_factory: Callable[[str], WorkspaceSyncBackend | None] | None = None,
        watched_subdirs: Sequence[str] | None = None,
    ) -> None:
        """Initialize the manager with its root and backend factory."""
        self._workspaces_root = Path(workspaces_root)
        self._backend_factory = backend_factory
        self._watched_subdirs = merge_watched_subdirs(watched_subdirs)
        self._workspaces: dict[str, Workspace] = {}

    @property
    def workspaces_root(self) -> Path:
        """Return the local root directory holding per-run workspaces."""
        return self._workspaces_root

    def _workspace_root(self, run_id: str) -> Path:
        """Return the workspace directory for a run id.

        Args:
            run_id: Unique run identifier (also the directory name).

        Returns:
            The workspace root path.

        Raises:
            ValueError: When the run id is not a single safe path
                component.
        """
        if not _RUN_ID_RE.match(run_id):
            raise ValueError(f"run id {run_id!r} must be a single safe path component")
        return self._workspaces_root / run_id

    def _create_workspace_dirs(self, run_id: str) -> Path:
        """Create the workspace directory structure for a run.

        Args:
            run_id: Unique run identifier.

        Returns:
            Path to the created workspace root.
        """
        root = self._workspace_root(run_id)
        (root / "input").mkdir(parents=True, exist_ok=True)
        (root / "working").mkdir(parents=True, exist_ok=True)
        (root / "output").mkdir(parents=True, exist_ok=True)
        for subdir in self._watched_subdirs:
            (root / subdir).mkdir(parents=True, exist_ok=True)
        return root

    async def open(self, run_id: str) -> Workspace:
        """Open a workspace and load its last committed manifest.

        Args:
            run_id: Unique run identifier.

        Returns:
            A `Workspace` handle ready for materialization.
        """
        root = self._create_workspace_dirs(run_id)

        backend: WorkspaceSyncBackend | None = None
        if self._backend_factory is not None:
            try:
                backend = self._backend_factory(run_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Backend factory failed for run %s — remote sync disabled for this run",
                    run_id,
                )
                backend = None
        if backend is None:
            logger.warning(
                "Opening workspace %s without a remote backend — remote sync disabled",
                run_id,
            )

        dirty_tracker = DirtyTracker(
            workspace_root=root,
            watched_subdirs=self._watched_subdirs,
        )

        manifest: Manifest | None = None
        if backend is not None:
            manifest = await backend.get_manifest(run_id)
            if manifest is not None:
                logger.info(
                    "Loaded manifest for run %s at version %d (%d artifacts)",
                    run_id,
                    manifest.version,
                    len(manifest.artifacts),
                )

        ws = Workspace(
            run_id=run_id,
            root=root,
            backend=backend,
            dirty_tracker=dirty_tracker,
            manifest=manifest,
        )
        self._workspaces[run_id] = ws
        logger.info("Opened workspace for run %s at %s", run_id, root)
        return ws

    def get(self, run_id: str) -> Workspace | None:
        """Get an already-open workspace by run ID.

        Args:
            run_id: Unique run identifier.

        Returns:
            The workspace handle, or `None` if not open.
        """
        return self._workspaces.get(run_id)

    async def close(self, run_id: str) -> None:
        """Close and remove a workspace from the active set.

        Args:
            run_id: Unique run identifier.
        """
        ws = self._workspaces.pop(run_id, None)
        if ws is not None:
            await ws.close()

    async def close_all(self) -> None:
        """Close all open workspaces.

        One failing close is logged and the loop continues — a single
        bad workspace must not block the shutdown of the rest.
        """
        run_ids = list(self._workspaces.keys())
        for run_id in run_ids:
            ws = self._workspaces.pop(run_id, None)
            if ws is None:
                continue
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to close workspace %s", run_id)

    def __len__(self) -> int:
        """Return the number of open workspaces."""
        return len(self._workspaces)

    def __repr__(self) -> str:
        """Return a debug representation."""
        return f"WorkspaceManager(root={self._workspaces_root}, open={len(self._workspaces)})"
