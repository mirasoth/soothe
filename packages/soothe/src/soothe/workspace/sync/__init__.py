"""Content-authoritative workspace sync with one backend root per run.

One per-run root holds the plain content tree (the authoritative file
store) and the engine-private manifest (the atomic commit record). This
package provides the backend protocol and data models, the fsspec adapter,
the dirty tracker, and the workspace/turn lifecycle facades.
"""

from soothe.workspace.sync.backends.fsspec import FsspecSyncBackend
from soothe.workspace.sync.dirty_tracker import DirtyTracker, FileEvent, FileEventKind
from soothe.workspace.sync.errors import (
    ConcurrentModificationError,
    IntegrityError,
    WorkspaceSyncError,
)
from soothe.workspace.sync.factory import construct_sync_backend
from soothe.workspace.sync.manager import WorkspaceManager
from soothe.workspace.sync.turns import TurnCommit, TurnScopedWorkspaces
from soothe.workspace.sync.workspace import Workspace
from soothe.workspace.sync.workspace_sync import Manifest, ManifestEntry, WorkspaceSyncBackend

__all__ = [
    "ConcurrentModificationError",
    "DirtyTracker",
    "FsspecSyncBackend",
    "FileEvent",
    "FileEventKind",
    "IntegrityError",
    "Manifest",
    "ManifestEntry",
    "TurnCommit",
    "TurnScopedWorkspaces",
    "Workspace",
    "WorkspaceManager",
    "WorkspaceSyncBackend",
    "WorkspaceSyncError",
    "construct_sync_backend",
]
