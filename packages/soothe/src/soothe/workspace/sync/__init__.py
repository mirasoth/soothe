"""Workspace sync subsystem — single-root content-authoritative sync.

Host-side workspace sync layer (IG-CORE-011): one per-run backend root
holds the plain content tree (the authoritative file store) and the
engine-private manifest (the atomic commit record).  The
`WorkspaceSyncBackend` protocol and data models live in
`soothe.workspace.sync.workspace_sync`; this package provides the concrete
`FsspecSyncBackend` adapter, dirty tracker, workspace manager, and the
turn-scoped `TurnScopedWorkspaces` facade for hosts that bracket agent
turns with open/close lifecycle hooks.
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
