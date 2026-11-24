"""Unit tests for orphan loop-directory reconcile (enumeration guard)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import soothe.config as soothe_config

from soothe_daemon.runtime.loop_reconcile import (
    _ENUMERATION_CAP,
    reconcile_orphan_loop_directories,
)


@pytest.fixture
def loops_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point SOOTHE_HOME at tmp_path and create the loops directory."""
    home = tmp_path / "soothe-home"
    home.mkdir()
    monkeypatch.setattr(soothe_config, "SOOTHE_HOME", str(home))
    loops = home / "data" / "loops"
    loops.mkdir(parents=True)
    return loops


class _FakeStore:
    """Fake persistence manager with a bounded list_loops response."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def list_loops(
        self,
        status_filter: str | None = None,
        limit: int = 100,
        **_kw: Any,
    ) -> list[dict[str, Any]]:
        assert status_filter is None
        return self._rows[:limit]


def _mk_dir(loops_dir: Path, name: str) -> Path:
    d = loops_dir / name
    d.mkdir()
    (d / "checkpoint.json").write_text("{}")
    return d


@pytest.mark.asyncio
async def test_reconcile_removes_directories_without_rows(
    loops_dir: Path,
) -> None:
    """Directories without a matching DB row are removed."""
    _mk_dir(loops_dir, "loop-known")
    orphan = _mk_dir(loops_dir, "loop-orphan")

    removed = await reconcile_orphan_loop_directories(
        _FakeStore([{"loop_id": "loop-known"}]),
    )

    assert removed == 1
    assert not orphan.exists()
    assert (loops_dir / "loop-known").is_dir()


@pytest.mark.asyncio
async def test_reconcile_does_not_delete_beyond_enumeration_cap(
    loops_dir: Path,
) -> None:
    """When list_loops returns exactly the cap, deletion is skipped entirely.

    A full page means the store may hold more loops than were enumerated, so
    the known-id set is potentially truncated — deleting anything could
    destroy live loop data.
    """
    known = [{"loop_id": f"loop-{i:05d}"} for i in range(_ENUMERATION_CAP)]
    # Directory names are deliberately absent from the truncated known set:
    # with the pre-guard behavior they would all be rmtree'd.
    orphan = _mk_dir(loops_dir, "loop-not-enumerated")

    removed = await reconcile_orphan_loop_directories(_FakeStore(known))

    assert removed == 0
    assert orphan.is_dir()


@pytest.mark.asyncio
async def test_reconcile_skips_deletion_on_list_failure(loops_dir: Path) -> None:
    """A failing list_loops must never trigger deletion."""

    class _Boom:
        async def list_loops(self, **_kw: Any) -> list[dict[str, Any]]:
            raise RuntimeError("db gone")

    _mk_dir(loops_dir, "loop-orphan")

    removed = await reconcile_orphan_loop_directories(_Boom())

    assert removed == 0
    assert (loops_dir / "loop-orphan").is_dir()
