"""Turn-scoped workspace sync facade for host orchestrators.

Hosts that bracket agent turns with open/close hooks map their session
identity to a run id and call `open_turn` / `close_turn`:

* `open_turn` opens the workspace, loads the last committed manifest,
  materializes the content tree, reconciles deletions, and starts dirty
  tracking.
* `close_turn` commits the dirty state to the content tree, writes the
  manifest (the atomic commit point), and closes the workspace. The
  result is a `TurnCommit` report so persistence failures reach the caller.

Hosts must serialize turns per run id (one in-flight open/close pair per
workspace); the engine keeps no concurrent-turn bookkeeping. A failed
commit leaves the workspace open so its dirty set survives to the next
turn's commit; a clean commit closes it and releases its per-run backend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from soothe.workspace.sync.manager import WorkspaceManager

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from soothe.workspace.sync.workspace_sync import Manifest, WorkspaceSyncBackend

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnCommit:
    """Result of one turn's persistence attempt.

    Attributes:
        persisted: True when the turn's state is durably committed (or
            there was nothing to persist).  False when the commit failed
            — the turn's writes remain local-only in the still-open
            workspace and are retried by the next turn.
        version: The new manifest version, or `None` when nothing was
            dirty/deleted or the workspace runs local-only.
        error: Failure detail when `persisted` is False, else `None`.
    """

    persisted: bool
    version: int | None = None
    error: str | None = None


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
    """Turn-scoped facade over `WorkspaceManager`.

    Args:
        workspaces_root: Root directory holding per-run workspaces.
        backend_factory: Callable mapping a run id to the run's
            backend — one instance per run serving both the plain
            content tree (the authoritative file store) and the
            engine-private manifest.  Returning `None` (or `None`
            factory) runs that workspace local-only.
        watched_subdirs: Workspace-relative subtrees tracked by the
            dirty tracker (and pre-created at workspace open), beyond
            the engine defaults (`input`, `working`, `output`).
    """

    def __init__(
        self,
        *,
        workspaces_root: Path,
        backend_factory: Callable[[str], WorkspaceSyncBackend | None] | None = None,
        watched_subdirs: Sequence[str] | None = None,
    ) -> None:
        """Initialize the facade and its underlying workspace manager."""
        self._manager = WorkspaceManager(
            workspaces_root=workspaces_root,
            backend_factory=backend_factory,
            watched_subdirs=watched_subdirs,
        )

    async def open_turn(self, run_id: str) -> Path:
        """Open a turn on a workspace and return its local root.

        Opens the workspace if it is not already open (a workspace stays
        open after a failed commit — its dirty set survives to the next
        commit), materializes the authoritative content tree (sha-skip
        keeps repeat turns cheap), reconciles deletions, and starts
        dirty tracking.

        Args:
            run_id: Unique run identifier (the workspace directory name).

        Returns:
            Local filesystem root of the workspace.
        """
        ws = self._manager.get(run_id)
        if ws is None:
            ws = await self._manager.open(run_id)

        # Materialize before the agent runs: manifest artifacts are
        # protected because a mid-commit crash can leave them local but
        # not yet stored in the content tree.
        try:
            await ws.materialize_content(
                protected=_artifact_paths(ws.manifest),
                reconcile=True,
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

    async def close_turn(self, run_id: str) -> TurnCommit:
        """Close a turn: commit, then release the workspace's resources.

        The commit runs first (turn completion = persisted); on success
        the workspace is closed and its backend released.  A failed
        commit returns `persisted=False` and keeps the workspace open
        so the dirty set survives to the next turn's commit.

        Args:
            run_id: Unique run identifier (the workspace directory name).

        Returns:
            The `TurnCommit` result for the turn.
        """
        ws = self._manager.get(run_id)
        if ws is None:
            return TurnCommit(persisted=True)

        try:
            version = await ws.checkpoint()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Checkpoint failed for run %s; workspace stays open", run_id, exc_info=True
            )
            return TurnCommit(persisted=False, error=str(exc))

        try:
            await self._manager.close(run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Workspace close failed for run %s", run_id, exc_info=True)
            return TurnCommit(
                persisted=True, version=version, error=f"workspace close failed: {exc}"
            )

        return TurnCommit(persisted=True, version=version)

    async def close_all(self) -> None:
        """Close all open workspaces on orchestrator shutdown."""
        await self._manager.close_all()

    @property
    def manager(self) -> WorkspaceManager:
        """Return the underlying workspace manager (test/introspection)."""
        return self._manager
