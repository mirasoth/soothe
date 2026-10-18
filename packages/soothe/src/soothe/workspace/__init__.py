"""Soothe workspace: loop resolution + explicit nano workspace re-exports."""

# Re-export facade — canonical source: soothe_nano.workspace.* submodules
from soothe_nano.workspace.workspace_api import (
    ResolvedWorkspace,
    ResolvedWorkspaceSource,
)
from soothe_nano.workspace.workspace_filesystem import (
    FrameworkFilesystem,
    NormalizedPathBackend,
    WorkspaceAwareBackend,
    WorkspaceBackendRegistry,
)
from soothe_nano.workspace.workspace_policy import WorkspaceResolver
from soothe_nano.workspace.workspace_runtime import (
    WorkspaceContext,
    WorkspaceRuntime,
)

from soothe.workspace.core_resolution import (
    WorkspacePrecedence,
    resolve_workspace,
)
from soothe.workspace.loop_workspace import (
    compute_scoped_workspace_dir_name,
    normalize_user_id,
    resolve_loop_workspace,
    resolve_persisted_loop_workspace,
)
from soothe.workspace.resolution import (
    cleanup_anonymous_workspaces,
    is_remote_workspace_uri,
    resolve_daemon_workspace,
    translate_client_path_to_container,
    translate_container_path_to_client,
    validate_client_workspace,
)
from soothe.workspace.scoped import user_id_for_hash

__all__ = [
    "FrameworkFilesystem",
    "NormalizedPathBackend",
    "ResolvedWorkspace",
    "ResolvedWorkspaceSource",
    "WorkspaceAwareBackend",
    "WorkspaceBackendRegistry",
    "WorkspaceContext",
    "WorkspacePrecedence",
    "WorkspaceResolver",
    "WorkspaceRuntime",
    "cleanup_anonymous_workspaces",
    "compute_scoped_workspace_dir_name",
    "is_remote_workspace_uri",
    "normalize_user_id",
    "resolve_daemon_workspace",
    "resolve_loop_workspace",
    "resolve_persisted_loop_workspace",
    "translate_client_path_to_container",
    "translate_container_path_to_client",
    "resolve_workspace",
    "user_id_for_hash",
    "validate_client_workspace",
]
