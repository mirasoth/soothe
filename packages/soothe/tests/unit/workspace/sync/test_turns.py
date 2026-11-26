"""Tests for the turn-scoped workspace sync facade (``TurnScopedWorkspaces``).

Orchestration tests (shared open, reconcile gating, last-turn-out close,
best-effort failure semantics, manifest protection) inject a mocked
``WorkspaceManager`` with duck-typed workspace fakes — the semantics ported
from flowjet's resolver tests. The round-trip test uses real
``MemoryFileSystem`` backends and re-opens with a fresh facade instance to
pin the crash-recovery contract end to end.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fsspec.implementations.memory import MemoryFileSystem

from soothe.workspace.sync.backends.fsspec import FsspecSyncBackend
from soothe.workspace.sync.turns import TurnScopedWorkspaces

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
    facade = TurnScopedWorkspaces(
        workspaces_root=tmp_path / "ws",
        backend=MagicMock(),
    )
    facade._manager = manager
    return facade


# ---------------------------------------------------------------------------
# open_turn / close_turn orchestration
# ---------------------------------------------------------------------------


class TestOpenCloseTurns:
    async def test_open_turn_materializes_and_tracks(self, tmp_path: Path) -> None:
        root = tmp_path / "ws" / "run-1"
        root.mkdir(parents=True)
        ws = _fake_workspace(root)
        manager = MagicMock()
        manager.open = AsyncMock(return_value=ws)
        facade = _facade_with_manager(tmp_path, manager)

        assert await facade.open_turn("run-1") == root

        manager.open.assert_awaited_once_with("run-1")
        ws.materialize_content.assert_awaited_once_with(protected=[], reconcile=True)
        ws.start_tracking.assert_called_once()

    async def test_concurrent_turns_share_one_open_and_skip_reconcile(self, tmp_path: Path) -> None:
        """Every turn materializes; reconcile only while sole active turn."""
        root = tmp_path / "ws" / "shared"
        root.mkdir(parents=True)
        ws = _fake_workspace(root)
        manager = MagicMock()
        manager.open = AsyncMock(return_value=ws)
        facade = _facade_with_manager(tmp_path, manager)

        await facade.open_turn("shared")
        assert ws.materialize_content.call_args.kwargs["reconcile"] is True

        await facade.open_turn("shared")
        assert ws.materialize_content.call_args.kwargs["reconcile"] is False
        # One open per run id, not per turn.
        assert manager.open.await_count == 1

    async def test_close_turn_checkpoints_and_closes_on_last_turn_out(self, tmp_path: Path) -> None:
        root = tmp_path / "ws" / "shared"
        root.mkdir(parents=True)
        ws = _fake_workspace(root)
        manager = MagicMock()
        manager.open = AsyncMock(return_value=ws)
        manager.close = AsyncMock()
        facade = _facade_with_manager(tmp_path, manager)

        await facade.open_turn("shared")
        await facade.open_turn("shared")
        await facade.close_turn("shared")
        # Peer turn still active: checkpoint runs but the workspace stays open.
        ws.checkpoint.assert_awaited_once()
        manager.close.assert_not_awaited()

        await facade.close_turn("shared")
        assert ws.checkpoint.await_count == 2
        manager.close.assert_awaited_once_with("shared")

    async def test_close_turn_without_open_is_noop(self, tmp_path: Path) -> None:
        manager = MagicMock()
        manager.close = AsyncMock()
        facade = _facade_with_manager(tmp_path, manager)

        await facade.close_turn("never-opened")
        manager.close.assert_not_awaited()

    async def test_close_turn_swallows_checkpoint_errors(self, tmp_path: Path) -> None:
        root = tmp_path / "ws" / "run-err"
        root.mkdir(parents=True)
        ws = _fake_workspace(root)
        ws.checkpoint = AsyncMock(side_effect=RuntimeError("boom"))
        manager = MagicMock()
        manager.open = AsyncMock(return_value=ws)
        manager.close = AsyncMock()
        facade = _facade_with_manager(tmp_path, manager)

        await facade.open_turn("run-err")
        await facade.close_turn("run-err")  # must not raise
        manager.close.assert_awaited_once_with("run-err")

    async def test_close_all_delegates_to_manager(self, tmp_path: Path) -> None:
        manager = MagicMock()
        manager.close_all = AsyncMock()
        facade = _facade_with_manager(tmp_path, manager)

        await facade.close_all()
        manager.close_all.assert_awaited_once()
        assert facade._entries == {}

    async def test_content_materialization_failure_is_non_fatal(self, tmp_path: Path) -> None:
        root = tmp_path / "ws" / "run-content"
        root.mkdir(parents=True)
        ws = _fake_workspace(root)
        ws.materialize_content = AsyncMock(side_effect=RuntimeError("content boom"))
        manager = MagicMock()
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
        manager = MagicMock()
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
            content_root = FsspecSyncBackend(fs=fs, root="/content")
            await content_root.put_content_file("materials/m1/notes.md", b"material v1")

            def content_factory(run_id: str) -> FsspecSyncBackend:
                return FsspecSyncBackend(fs=fs, root="/content")

            engine = TurnScopedWorkspaces(
                workspaces_root=tmp_path / "ws",
                backend=FsspecSyncBackend(fs=fs, root="/sync"),
                content_backend_factory=content_factory,
            )

            root = await engine.open_turn("run-1")
            # Host material materialized into the agent-visible workspace.
            assert (root / "materials" / "m1" / "notes.md").read_bytes() == b"material v1"

            # The agent writes an output file.
            out = root / "output" / "analysis.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"agent analysis")

            await engine.close_turn("run-1")
            # Dirty writes mirrored to the content tree; manifest committed.
            assert await content_root.get_content_file("output/analysis.md") == b"agent analysis"
            assert fs.exists("/sync/runs/run-1/manifest.json")

            # A fresh engine (crash/restart) restores both trees.
            engine2 = TurnScopedWorkspaces(
                workspaces_root=tmp_path / "ws2",
                backend=FsspecSyncBackend(fs=fs, root="/sync"),
                content_backend_factory=content_factory,
            )
            root2 = await engine2.open_turn("run-1")
            assert (root2 / "materials" / "m1" / "notes.md").read_bytes() == b"material v1"
            assert (root2 / "output" / "analysis.md").read_bytes() == b"agent analysis"
            await engine2.close_turn("run-1")
        finally:
            fs.store.clear()
