"""Workspace manager — owns the open-workspace set for a sync root.

The manager creates local workspace directories, wires each run's
`DirtyTracker`, resolves the per-run content backend, and loads the
run's manifest (the engine-owned artifact set of the last committed
turn) from the remote backend at open — a fresh process never resets
manifest state (F-L2).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from soothe.workspace.sync.dirty_tracker import DirtyTracker
from soothe.workspace.sync.workspace import Workspace

if TYPE_CHECKING:
    from collections.abc import Sequence

    from soothe.workspace.sync.workspace_sync import Manifest, WorkspaceSyncBackend

logger = logging.getLogger(__name__)

# Workspace directory names double as sync run IDs: a single safe path
# component (mirrored by hosts mapping their session/workspace ids to
# run ids).
_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")

# Subtrees pre-created at workspace open; the dirty tracker watches
# these plus root-level files.  Additional subtrees (e.g.
# "deliverables") come from the manager's `watched_subdirs`.
_DEFAULT_WATCHED_SUBDIRS = ("input", "working", "output")


def _normalize_watched_subdirs(
    watched_subdirs: Sequence[str] | None,
) -> tuple[str, ...]:
    """Normalize the watched-subdir configuration.

    Args:
        watched_subdirs: Configured additional subtrees, or `None`.

    Returns:
        The full watched-subdir tuple (defaults + configured extras).

    Raises:
        ValueError: When an entry is not a single safe path component.
    """
    subdirs = list(_DEFAULT_WATCHED_SUBDIRS)
    for entry in watched_subdirs or ():
        if not isinstance(entry, str) or not _RUN_ID_RE.match(entry) or "/" in entry:
            raise ValueError(f"watched subdir {entry!r} must be a single safe path component")
        if entry not in subdirs:
            subdirs.append(entry)
    return tuple(subdirs)


class WorkspaceManager:
    """Owns the set of open workspaces for one manifest-sync root.

    Args:
        workspaces_root: Local root directory holding per-run
            workspaces.
        backend: Remote manifest backend (commit records).  `None`
            opens workspaces in local-only mode (no remote commits).
        content_backend_factory: Optional callable mapping a run id to
            the run's content backend (the plain file tree — the
            authoritative file store).  Returning `None` disables
            content sync for that run.
        watched_subdirs: Additional workspace-relative subtrees tracked
            by the dirty tracker (beyond `input`/`working`/`output`).
    """

    def __init__(
        self,
        *,
        workspaces_root: Path,
        backend: WorkspaceSyncBackend | None = None,
        content_backend_factory: Callable[[str], WorkspaceSyncBackend | None] | None = None,
        watched_subdirs: Sequence[str] | None = None,
    ) -> None:
        """Initialize the manager with its root, backend, and factories."""
        self._workspaces_root = Path(workspaces_root)
        self._backend = backend
        self._content_backend_factory = content_backend_factory
        self._watched_subdirs = _normalize_watched_subdirs(watched_subdirs)
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
        (root / ".workspace").mkdir(parents=True, exist_ok=True)
        # Configured extra subtrees (e.g. "deliverables") are pre-created so
        # agents can rely on them and the dirty tracker walks them from the
        # first scan. Unsafe entries fail loud here (ValueError).
        for subdir in _normalize_watched_subdirs(self._watched_subdirs):
            (root / subdir).mkdir(parents=True, exist_ok=True)
        return root

    async def open(self, run_id: str) -> Workspace:
        """Open a workspace for a run using the manager's backend.

        Creates the workspace directory structure, wires the dirty
        tracker and per-run content backend, and loads any existing
        remote manifest (the last committed artifact set) so a fresh
        process never resets manifest state.

        Args:
            run_id: Unique run identifier.

        Returns:
            A `Workspace` handle ready for materialization.
        """
        root = self._create_workspace_dirs(run_id)

        if self._backend is None:
            logger.warning(
                "Opening workspace %s without a remote backend — remote commits disabled",
                run_id,
            )

        content_backend: WorkspaceSyncBackend | None = None
        if self._content_backend_factory is not None:
            try:
                content_backend = self._content_backend_factory(run_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Content backend factory failed for run %s — "
                    "content sync disabled for this run",
                    run_id,
                )
                content_backend = None

        dirty_tracker = DirtyTracker(
            workspace_root=root,
            watched_subdirs=self._watched_subdirs,
        )

        manifest: Manifest | None = None
        if self._backend is not None:
            manifest = await self._backend.get_manifest(run_id)
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
            backend=self._backend,
            content_backend=content_backend,
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
