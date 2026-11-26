"""Tests for the turn-scoped workspace sync facade (`TurnScopedWorkspaces`).

Single-writer contract: one in-flight
`open_turn`/`close_turn` pair per run id. Orchestration tests
(reuse-after-failed-commit, TurnCommit reporting, resource release,
best-effort failure semantics, manifest protection) inject a mocked
`WorkspaceManager` with duck-typed workspace fakes. The round-trip test
uses real `MemoryFileSystem` backends and re-opens with a fresh facade
instance to pin the crash-recovery contract end to end.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fsspec.implementations.memory import MemoryFileSystem

from soothe.workspace.sync.backends.fsspec import FsspecSyncBackend
from soothe.workspace.sync.turns import TurnCommit, TurnScopedWorkspaces

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _fake_workspace(root: Path) -> MagicMock:
    """Build a duck-typed Workspace mock with async lifecycle methods."""
    ws = MagicMock()
    ws.root = root
    ws.run_id = root.name
    ws.manifest = None
    ws.start_tracking = MagicMock()
    ws.checkpoint = AsyncMock()
    ws.materialize_content = AsyncMock(return_value=0)
    return ws


def _facade_with_manager(
    tmp_path: Path,
    manager: MagicMock,
) -> TurnScopedWorkspaces:
    facade = TurnScopedWorkspaces(workspaces_root=tmp_path / "ws")
    facade._manager = manager
    return facade


def _closed_manager() -> MagicMock:
    """Manager mock with no open workspace and async open/close."""
    manager = MagicMock()
    manager.get = MagicMock(return_value=None)
    manager.open = AsyncMock()
    manager.close = AsyncMock()
    manager.close_all = AsyncMock()
    return manager


# ---------------------------------------------------------------------------
# open_turn / close_turn orchestration
# ---------------------------------------------------------------------------


class TestOpenCloseTurns:
    async def test_open_turn_materializes_and_tracks(self, tmp_path: Path) -> None:
        root = tmp_path / "ws" / "run-1"
        root.mkdir(parents=True)
        ws = _fake_workspace(root)
        manager = _closed_manager()
        manager.open = AsyncMock(return_value=ws)
        facade = _facade_with_manager(tmp_path, manager)

        assert await facade.open_turn("run-1") == root

        manager.open.assert_awaited_once_with("run-1")
        ws.materialize_content.assert_awaited_once_with(protected=[], reconcile=True)
        ws.start_tracking.assert_called_once()

    async def test_open_turn_reuses_open_workspace(self, tmp_path: Path) -> None:
        """A workspace kept open (failed prior commit) is reused, not reopened."""
        root = tmp_path / "ws" / "shared"
        root.mkdir(parents=True)
        ws = _fake_workspace(root)
        manager = MagicMock()
        manager.get = MagicMock(return_value=ws)
        manager.open = AsyncMock()
        facade = _facade_with_manager(tmp_path, manager)

        await facade.open_turn("shared")
        await facade.open_turn("shared")

        manager.open.assert_not_awaited()
        assert ws.materialize_content.await_count == 2
        # Reconcile stays on — single writer, no peer-turn gating.
        assert all(
            c.kwargs.get("reconcile") is True for c in ws.materialize_content.await_args_list
        )

    async def test_close_turn_commits_and_releases(self, tmp_path: Path) -> None:
        root = tmp_path / "ws" / "run-1"
        root.mkdir(parents=True)
        ws = _fake_workspace(root)
        ws.checkpoint = AsyncMock(return_value=4)
        manager = MagicMock()
        manager.get = MagicMock(return_value=ws)
        manager.close = AsyncMock()
        facade = _facade_with_manager(tmp_path, manager)

        commit = await facade.close_turn("run-1")

        ws.checkpoint.assert_awaited_once()
        manager.close.assert_awaited_once_with("run-1")
        assert commit == TurnCommit(persisted=True, version=4, error=None)

    async def test_close_turn_without_open_is_noop(self, tmp_path: Path) -> None:
        manager = MagicMock()
        manager.get = MagicMock(return_value=None)
        manager.close = AsyncMock()
        facade = _facade_with_manager(tmp_path, manager)

        commit = await facade.close_turn("never-opened")
        manager.close.assert_not_awaited()
        assert commit == TurnCommit(persisted=True, version=None, error=None)

    async def test_failed_commit_reports_not_persisted_and_keeps_open(self, tmp_path: Path) -> None:
        """A failed checkpoint surfaces in the TurnCommit and the workspace
        stays open so the dirty set survives to the next commit."""
        root = tmp_path / "ws" / "run-err"
        root.mkdir(parents=True)
        ws = _fake_workspace(root)
        ws.checkpoint = AsyncMock(side_effect=RuntimeError("boom"))
        manager = MagicMock()
        manager.get = MagicMock(return_value=ws)
        manager.close = AsyncMock()
        facade = _facade_with_manager(tmp_path, manager)

        commit = await facade.close_turn("run-err")  # must not raise

        assert commit.persisted is False
        assert commit.version is None
        assert "boom" in (commit.error or "")
        # The workspace is NOT closed — the dirty set survives.
        manager.close.assert_not_awaited()

    async def test_retry_after_failed_commit_recovers(self, tmp_path: Path) -> None:
        root = tmp_path / "ws" / "run-retry"
        root.mkdir(parents=True)
        ws = _fake_workspace(root)
        ws.checkpoint = AsyncMock(side_effect=[RuntimeError("transient"), 2])
        manager = MagicMock()
        manager.get = MagicMock(return_value=ws)
        manager.close = AsyncMock()
        facade = _facade_with_manager(tmp_path, manager)

        failed = await facade.close_turn("run-retry")
        assert failed.persisted is False
        assert manager.close.await_count == 0

        # Next turn reuses the still-open workspace and commits cleanly.
        await facade.open_turn("run-retry")
        recovered = await facade.close_turn("run-retry")
        assert recovered == TurnCommit(persisted=True, version=2, error=None)
        manager.close.assert_awaited_once_with("run-retry")

    async def test_close_failure_after_clean_commit_reports_error(self, tmp_path: Path) -> None:
        """A commit that landed but whose resource release failed stays persisted."""
        root = tmp_path / "ws" / "run-close"
        root.mkdir(parents=True)
        ws = _fake_workspace(root)
        ws.checkpoint = AsyncMock(return_value=3)
        manager = MagicMock()
        manager.get = MagicMock(return_value=ws)
        manager.close = AsyncMock(side_effect=RuntimeError("close boom"))
        facade = _facade_with_manager(tmp_path, manager)

        commit = await facade.close_turn("run-close")

        assert commit.persisted is True
        assert commit.version == 3
        assert "close boom" in (commit.error or "")

    async def test_close_all_delegates_to_manager(self, tmp_path: Path) -> None:
        manager = MagicMock()
        manager.close_all = AsyncMock()
        facade = _facade_with_manager(tmp_path, manager)

        await facade.close_all()
        manager.close_all.assert_awaited_once()

    async def test_content_materialization_failure_is_non_fatal(self, tmp_path: Path) -> None:
        root = tmp_path / "ws" / "run-content"
        root.mkdir(parents=True)
        ws = _fake_workspace(root)
        ws.materialize_content = AsyncMock(side_effect=RuntimeError("content boom"))
        manager = _closed_manager()
        manager.open = AsyncMock(return_value=ws)
        facade = _facade_with_manager(tmp_path, manager)

        await facade.open_turn("run-content")  # must not raise
        ws.start_tracking.assert_called_once()


# ---------------------------------------------------------------------------
# Manifest protection at open
# ---------------------------------------------------------------------------


class TestManifestProtection:
    async def test_manifest_artifacts_are_protected_at_materialize(self, tmp_path: Path) -> None:
        root = tmp_path / "ws" / "run-prot"
        root.mkdir(parents=True)
        ws = _fake_workspace(root)
        ws.manifest = SimpleNamespace(
            artifacts=[SimpleNamespace(path="output/pending.md")],
            version=3,
        )
        manager = _closed_manager()
        manager.open = AsyncMock(return_value=ws)
        facade = _facade_with_manager(tmp_path, manager)

        await facade.open_turn("run-prot")

        kwargs = ws.materialize_content.call_args.kwargs
        assert kwargs["protected"] == ["output/pending.md"]
        assert kwargs["reconcile"] is True


# ---------------------------------------------------------------------------
# End-to-end round trip (MemoryFileSystem)
# ---------------------------------------------------------------------------


class TestRoundTrip:
    async def test_turn_round_trip_persists_and_restores(self, tmp_path: Path) -> None:
        fs = MemoryFileSystem()
        try:
            content_root = FsspecSyncBackend(fs=fs, root="/run", max_workers=2)
            await content_root.put_content_file("materials/m1/notes.md", b"material v1")

            def backend_factory(run_id: str) -> FsspecSyncBackend:
                return FsspecSyncBackend(fs=fs, root="/run", max_workers=2)

            engine = TurnScopedWorkspaces(
                workspaces_root=tmp_path / "ws",
                backend_factory=backend_factory,
            )

            root = await engine.open_turn("run-1")
            # Host material materialized into the agent-visible workspace.
            assert (root / "materials" / "m1" / "notes.md").read_bytes() == b"material v1"

            # The agent writes an output file.
            out = root / "output" / "analysis.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"agent analysis")

            commit = await engine.close_turn("run-1")
            assert commit.persisted is True
            assert commit.version == 1
            # Dirty writes mirrored to the content tree; manifest committed
            # under the engine-private key space of the run's own root.
            assert await content_root.get_content_file("output/analysis.md") == b"agent analysis"
            assert fs.exists("/run/.workspace/manifest.json")

            # A fresh engine (crash/restart) restores both trees.
            engine2 = TurnScopedWorkspaces(
                workspaces_root=tmp_path / "ws2",
                backend_factory=backend_factory,
            )
            root2 = await engine2.open_turn("run-1")
            assert (root2 / "materials" / "m1" / "notes.md").read_bytes() == b"material v1"
            assert (root2 / "output" / "analysis.md").read_bytes() == b"agent analysis"
            commit2 = await engine2.close_turn("run-1")
            assert commit2 == TurnCommit(persisted=True, version=None, error=None)
        finally:
            fs.store.clear()
