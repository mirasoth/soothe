"""Turn-scoped workspace sync facade for host orchestrators.

Hosts that bracket agent turns (one open before the agent runs, one close
after it finishes — flowjet's per-prompt lifecycle) map their session
identity to a run id and call ``open_turn`` / ``close_turn``.  Under the
content-authoritative representation (IG-CORE-010):

* ``open_turn`` opens the workspace (the manager loads the last
  committed manifest), materializes the content tree — the
  authoritative file store — into the local workspace, and starts dirty
  tracking;
* ``close_turn`` commits the dirty state: hash + upload to the content
  tree, mirror deletions, write the manifest (the atomic commit point).

Concurrent turns of the same run id share one open workspace: the
deletion reconcile only runs while the opening turn is the sole active
turn — deleting files under a peer's feet is worse than a delayed
delete — and the manager close belongs to the last turn out.

Failure semantics are best-effort: manifest load and content
materialization failures log a warning and continue with local state
only; commit and close failures never take the host's turn completion
down with them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from soothe.workspace.sync.manager import WorkspaceManager

if TYPE_CHECKING:
    from collections.abc import Sequence

    from soothe.workspace.sync.workspace import Workspace
    from soothe.workspace.sync.workspace_sync import Manifest, WorkspaceSyncBackend

logger = logging.getLogger(__name__)


@dataclass
class _OpenTurnEntry:
    """One open workspace — shared by all concurrent turns of the same run id.

    ``active_turns`` counts turns currently mid-run on this workspace; the
    manager lifecycle (``manager.close``) belongs to the last turn out.
    """

    ws: Workspace
    active_turns: int = 0


def _artifact_paths(manifest: Manifest | None) -> list[str]:
    """Return the relative paths of a manifest's artifacts (reconcile protection set).

    Args:
        manifest: Manifest loaded at open, or `None`.

    Returns:
        Relative workspace paths of the manifest's artifact entries.
    """
    if manifest is None:
        return []
    return [entry.path for entry in manifest.artifacts]


class TurnScopedWorkspaces:
    """Turn-scoped facade over :class:`WorkspaceManager`.

    Args:
        workspaces_root: Root directory holding per-run workspaces.
        backend: Remote manifest backend (commit records); `None`
            disables remote commits.
        content_backend_factory: Callable mapping a run id to the
            run's content backend — the plain file tree that is the
            authoritative file store.  Returning `None` disables
            content sync for a run.
        watched_subdirs: Workspace-relative subtrees tracked by the
            dirty tracker (and pre-created at workspace open), beyond
            the engine defaults (`input`, `working`, `output`).
    """

    def __init__(
        self,
        *,
        workspaces_root: Path,
        backend: WorkspaceSyncBackend | None = None,
        content_backend_factory: Callable[[str], WorkspaceSyncBackend | None] | None = None,
        watched_subdirs: Sequence[str] | None = None,
    ) -> None:
        """Initialize the facade and its underlying workspace manager."""
        self._manager = WorkspaceManager(
            workspaces_root=workspaces_root,
            backend=backend,
            content_backend_factory=content_backend_factory,
            watched_subdirs=watched_subdirs,
        )
        self._entries: dict[str, _OpenTurnEntry] = {}

    async def open_turn(self, run_id: str) -> Path:
        """Open a turn on a workspace and return its local root.

        Every turn unconditionally materializes the content tree —
        sha-skip keeps repeat turns cheap, including the reuse path
        where a peer turn already holds the workspace open. Deletion
        reconcile runs only while this turn is the sole active turn on
        the workspace.

        Args:
            run_id: Unique run identifier (the workspace directory name).

        Returns:
            Local filesystem root of the workspace.
        """
        entry = self._entries.get(run_id)
        if entry is None:
            ws = await self._manager.open(run_id)
            entry = _OpenTurnEntry(ws=ws)
            self._entries[run_id] = entry
        ws = entry.ws
        entry.active_turns += 1

        # Read path: materialize the authoritative content tree (e.g.
        # host-uploaded materials + prior agent outputs) into the
        # workspace BEFORE the agent runs.  Manifest artifacts are
        # protected — a mid-commit crash may have left engine-owned
        # files in the workspace but not yet in the content tree.  The
        # dirty tracker only watches the configured subtrees + root
        # files, so content-tree writes never pollute the dirty set.
        try:
            await ws.materialize_content(
                protected=_artifact_paths(ws.manifest),
                reconcile=entry.active_turns == 1,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Content materialization failed for run %s; continuing with "
                "local workspace state only",
                run_id,
                exc_info=True,
            )

        ws.start_tracking()
        return ws.root

    async def close_turn(self, run_id: str) -> None:
        """Close a turn: commit, and close the workspace on the last turn out.

        A mid-run peer's close commits the shared state but keeps the
        workspace open — the manager lifecycle belongs to the last turn
        out.  Commit and close failures are logged, not raised.

        Args:
            run_id: Unique run identifier (the workspace directory name).
        """
        entry = self._entries.get(run_id)
        if entry is None:
            return
        if entry.active_turns > 0:
            entry.active_turns -= 1
        try:
            await entry.ws.checkpoint()
        except Exception:  # noqa: BLE001
            logger.warning("Checkpoint failed for run %s", run_id, exc_info=True)
        if entry.active_turns > 0:
            return

        self._entries.pop(run_id, None)
        try:
            await self._manager.close(run_id)
        except Exception:  # noqa: BLE001
            logger.warning("Workspace close failed for run %s", run_id, exc_info=True)

    async def close_all(self) -> None:
        """Close all open workspaces on orchestrator shutdown."""
        self._entries.clear()
        await self._manager.close_all()
