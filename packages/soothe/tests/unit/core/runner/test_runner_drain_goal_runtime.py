"""Tests for workspace-scoped shell process draining at goal completion/cancel.

Covers ``drain_goal_runtime`` — lifecycle teardown that kills a goal's
``run_command`` (foreground sessions) and ``run_background`` (bg-logs)
grandchildren before the completion/cancel path finishes.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from unittest.mock import patch

import pytest

import soothe.runner.shell_drain as shell_drain
from soothe.runner.shell_drain import drain_goal_runtime

# The ownership-sidecar validation contract pairs with the soothe-nano
# sidecar support (Unreleased after 2.0.12). Under a locked PyPI
# soothe-nano (e.g. after `uv sync`) the drain degrades by design, so the
# sidecar-specific regressions only run when the paired nano is present.
_NANO_SIDECAR_API = shell_drain.process_start_token is not None


def _write_meta(marker: Path, *, pid: int, pgid: int, start_time: object = None) -> None:
    """Write the ownership sidecar the spawning tools record (schema 1)."""
    meta = {"schema": 1, "pid": pid, "pgid": pgid, "start_time": start_time}
    marker.with_name(marker.name + ".meta").write_text(
        json.dumps(meta, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _make_bg_logs(workspace: Path, pids: list[int], *, meta: bool = True) -> Path:
    """Create {workspace}/.soothe/background/bg-{pid}.log for each pid."""
    bg_dir = workspace / ".soothe" / "background"
    bg_dir.mkdir(parents=True, exist_ok=True)
    for pid in pids:
        marker = bg_dir / f"bg-{pid}.log"
        marker.write_text(f"[soothe] command: sleep {pid}\n")
        if meta:
            _write_meta(marker, pid=pid, pgid=pid)
    return bg_dir


def _make_fg_sessions(workspace: Path, pids: list[int], *, meta: bool = True) -> Path:
    """Create {workspace}/.soothe/foreground/fg-{pid}.session for each pid."""
    fg_dir = workspace / ".soothe" / "foreground"
    fg_dir.mkdir(parents=True, exist_ok=True)
    for pid in pids:
        marker = fg_dir / f"fg-{pid}.session"
        marker.write_text("[soothe] run_command started\n")
        if meta:
            _write_meta(marker, pid=pid, pgid=pid)
    return fg_dir


def test_drain_no_workspace_returns_zero() -> None:
    assert drain_goal_runtime("") == 0


def test_drain_missing_background_dir_returns_zero(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert drain_goal_runtime(str(workspace)) == 0


def test_drain_no_bg_logs_returns_zero(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _make_bg_logs(workspace, [])
    assert drain_goal_runtime(str(workspace)) == 0


def test_drain_kills_process_groups_sigterm_then_sigkill(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _make_bg_logs(workspace, [111, 222])

    killpg_calls: list[tuple[int, int]] = []

    def _fake_getpgid(pid: int) -> int:
        # Each PID is its own process group leader (start_new_session).
        return pid

    def _fake_killpg(pgid: int, sig: int) -> None:
        killpg_calls.append((pgid, sig))

    def _pid_alive(pid: int) -> bool:
        # Pretend SIGTERM doesn't kill, SIGKILL does.
        already_killed = any(p == pid and s == signal.SIGKILL for p, s in killpg_calls)
        return not already_killed

    with (
        patch("soothe.runner.shell_drain.os.getpgid", side_effect=_fake_getpgid),
        patch("soothe.runner.shell_drain.os.killpg", side_effect=_fake_killpg),
        patch("soothe.runner.shell_drain._pid_alive", side_effect=_pid_alive),
        patch("soothe.runner.shell_drain.time.sleep"),
    ):
        reaped = drain_goal_runtime(str(workspace), grace_seconds=0.0)

    # Two process groups reaped.
    assert reaped == 2
    # SIGTERM sent first for each, then SIGKILL (since SIGTERM "fails").
    term_calls = [c for c in killpg_calls if c[1] == signal.SIGTERM]
    kill_calls = [c for c in killpg_calls if c[1] == signal.SIGKILL]
    assert {c[0] for c in term_calls} == {111, 222}
    assert {c[0] for c in kill_calls} == {111, 222}
    # Log files unlinked after drain.
    bg_dir = workspace / ".soothe" / "background"
    assert not (bg_dir / "bg-111.log").exists()
    assert not (bg_dir / "bg-222.log").exists()


def test_drain_kills_foreground_run_command_sessions(tmp_path: Path) -> None:
    """In-flight run_command markers are reaped the same way as bg logs."""
    workspace = tmp_path / "ws"
    _make_fg_sessions(workspace, [777, 888])

    killpg_calls: list[tuple[int, int]] = []

    def _fake_getpgid(pid: int) -> int:
        return pid

    def _fake_killpg(pgid: int, sig: int) -> None:
        killpg_calls.append((pgid, sig))

    def _pid_alive(pid: int) -> bool:
        already_killed = any(p == pid and s == signal.SIGKILL for p, s in killpg_calls)
        return not already_killed

    with (
        patch("soothe.runner.shell_drain.os.getpgid", side_effect=_fake_getpgid),
        patch("soothe.runner.shell_drain.os.killpg", side_effect=_fake_killpg),
        patch("soothe.runner.shell_drain._pid_alive", side_effect=_pid_alive),
        patch("soothe.runner.shell_drain.time.sleep"),
    ):
        reaped = drain_goal_runtime(str(workspace), grace_seconds=0.0)

    assert reaped == 2
    assert {c[0] for c in killpg_calls if c[1] == signal.SIGTERM} == {777, 888}
    fg_dir = workspace / ".soothe" / "foreground"
    assert not (fg_dir / "fg-777.session").exists()
    assert not (fg_dir / "fg-888.session").exists()


def test_drain_reaps_foreground_and_background_together(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _make_fg_sessions(workspace, [101])
    _make_bg_logs(workspace, [202])

    killed: list[int] = []

    def _fake_getpgid(pid: int) -> int:
        return pid

    def _fake_killpg(pgid: int, sig: int) -> None:
        if sig == signal.SIGTERM:
            killed.append(pgid)

    def _pid_alive(pid: int) -> bool:
        return pid not in killed

    with (
        patch("soothe.runner.shell_drain.os.getpgid", side_effect=_fake_getpgid),
        patch("soothe.runner.shell_drain.os.killpg", side_effect=_fake_killpg),
        patch("soothe.runner.shell_drain._pid_alive", side_effect=_pid_alive),
        patch("soothe.runner.shell_drain.time.sleep"),
    ):
        reaped = drain_goal_runtime(str(workspace), grace_seconds=0.0)

    assert reaped == 2
    assert set(killed) == {101, 202}


def test_drain_skips_dead_processes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _make_bg_logs(workspace, [333])

    with (
        patch("soothe.runner.shell_drain.os.getpgid", return_value=333),
        patch(
            "soothe.runner.shell_drain._pid_alive",
            return_value=False,
        ),
        patch("soothe.runner.shell_drain.os.killpg") as mock_killpg,
    ):
        reaped = drain_goal_runtime(str(workspace), grace_seconds=0.0)

    assert reaped == 0
    mock_killpg.assert_not_called()


def test_drain_process_lookup_error_is_swallowed(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _make_bg_logs(workspace, [444])

    def _fake_killpg(pgid: int, sig: int) -> None:
        raise ProcessLookupError

    with (
        patch("soothe.runner.shell_drain.os.getpgid", return_value=444),
        patch("soothe.runner.shell_drain._pid_alive", return_value=True),
        patch("soothe.runner.shell_drain.os.killpg", side_effect=_fake_killpg),
        patch("soothe.runner.shell_drain.time.sleep"),
    ):
        reaped = drain_goal_runtime(str(workspace), grace_seconds=0.0)

    # killpg raised ProcessLookupError → counts as not delivered → not reaped.
    assert reaped == 0


def test_drain_only_touches_workspace_bg_logs(tmp_path: Path) -> None:
    """A PID whose bg-log is NOT under this workspace is never touched."""
    workspace = tmp_path / "ws"
    other = tmp_path / "other-ws"
    _make_bg_logs(workspace, [555])
    _make_bg_logs(other, [666])

    killed: list[int] = []

    def _fake_killpg(pgid: int, sig: int) -> None:
        killed.append(pgid)

    with (
        patch("soothe.runner.shell_drain.os.getpgid", side_effect=lambda p: p),
        patch("soothe.runner.shell_drain._pid_alive", return_value=False),
        patch("soothe.runner.shell_drain.os.killpg", side_effect=_fake_killpg),
    ):
        drain_goal_runtime(str(workspace), grace_seconds=0.0)

    # Only 555 is even considered (and since _pid_alive False, not reaped).
    assert 666 not in killed


def test_drain_refuses_to_kill_this_process_group(tmp_path: Path) -> None:
    """A forged marker naming THIS process is refused (review f994682f).

    Markers live in the agent-writable workspace, so their PIDs are
    untrusted input: without the protected-set guard a forged marker
    naming the daemon (the process running the drain) would kill it —
    the one kill path the agent's own `kill_process` guards block.
    """
    workspace = tmp_path / "ws"
    bg_dir = workspace / ".soothe" / "background"
    bg_dir.mkdir(parents=True)
    (bg_dir / f"bg-{os.getpid()}.log").write_text("[soothe] forged marker\n")

    killed: list[int] = []

    def _fake_killpg(pgid: int, sig: int) -> None:
        killed.append(pgid)

    with patch("soothe.runner.shell_drain.os.killpg", side_effect=_fake_killpg):
        reaped = drain_goal_runtime(str(workspace), grace_seconds=0.0)

    assert reaped == 0
    assert killed == []
    # The forged marker is left in place (not unlinked) so the refusal is
    # observable.
    assert (bg_dir / f"bg-{os.getpid()}.log").exists()


@pytest.mark.skipif(
    not _NANO_SIDECAR_API,
    reason="requires soothe-nano with spawn-marker ownership sidecars",
)
def test_drain_refuses_kill_without_ownership_sidecar(tmp_path: Path) -> None:
    """A marker with no ``.meta`` sidecar never authorizes a kill (review f994682f).

    Marker names live in the agent-writable workspace; the ownership record
    is written by the spawning tool at spawn time. An alive PID whose marker
    lacks it is left untouched (and the marker kept for inspection).
    """
    workspace = tmp_path / "ws"
    _make_bg_logs(workspace, [900], meta=False)

    killed: list[int] = []

    def _fake_killpg(pgid: int, sig: int) -> None:
        killed.append(pgid)

    with (
        patch("soothe.runner.shell_drain.os.getpgid", side_effect=lambda p: p),
        patch("soothe.runner.shell_drain._pid_alive", return_value=True),
        patch("soothe.runner.shell_drain.os.killpg", side_effect=_fake_killpg),
    ):
        reaped = drain_goal_runtime(str(workspace), grace_seconds=0.0)

    assert reaped == 0
    assert killed == []
    assert (workspace / ".soothe" / "background" / "bg-900.log").exists()


@pytest.mark.skipif(
    not _NANO_SIDECAR_API,
    reason="requires soothe-nano with spawn-marker ownership sidecars",
)
def test_drain_refuses_kill_on_forged_sidecar_pid(tmp_path: Path) -> None:
    """A sidecar copied from another marker fails the pid cross-check."""
    workspace = tmp_path / "ws"
    bg_dir = _make_bg_logs(workspace, [901])
    marker = bg_dir / "bg-901.log"
    # Forged: filename says 901, sidecar claims a different spawn pid.
    _write_meta(marker, pid=555, pgid=555)

    killed: list[int] = []

    def _fake_killpg(pgid: int, sig: int) -> None:
        killed.append(pgid)

    with (
        patch("soothe.runner.shell_drain.os.getpgid", side_effect=lambda p: p),
        patch("soothe.runner.shell_drain._pid_alive", return_value=True),
        patch("soothe.runner.shell_drain.os.killpg", side_effect=_fake_killpg),
    ):
        reaped = drain_goal_runtime(str(workspace), grace_seconds=0.0)

    assert reaped == 0
    assert killed == []


@pytest.mark.skipif(
    not _NANO_SIDECAR_API,
    reason="requires soothe-nano with spawn-marker ownership sidecars",
)
def test_drain_refuses_kill_on_start_time_mismatch(tmp_path: Path) -> None:
    """A recycled PID (or a forged sidecar) fails the start-time cross-check."""
    workspace = tmp_path / "ws"
    bg_dir = _make_bg_logs(workspace, [902])
    _write_meta(bg_dir / "bg-902.log", pid=902, pgid=902, start_time="recorded-at-spawn")

    killed: list[int] = []

    def _fake_killpg(pgid: int, sig: int) -> None:
        killed.append(pgid)

    with (
        patch("soothe.runner.shell_drain.os.getpgid", side_effect=lambda p: p),
        patch("soothe.runner.shell_drain._pid_alive", return_value=True),
        patch(
            "soothe.runner.shell_drain.process_start_token",
            return_value="different-start-time",
        ),
        patch("soothe.runner.shell_drain.os.killpg", side_effect=_fake_killpg),
    ):
        reaped = drain_goal_runtime(str(workspace), grace_seconds=0.0)

    assert reaped == 0
    assert killed == []


def test_drain_unlinks_marker_and_sidecar_for_dead_pids(tmp_path: Path) -> None:
    """A dead PID's marker AND its sidecar are cleaned up together."""
    workspace = tmp_path / "ws"
    bg_dir = _make_bg_logs(workspace, [903])

    with (
        patch("soothe.runner.shell_drain.os.getpgid", side_effect=lambda p: p),
        patch("soothe.runner.shell_drain._pid_alive", return_value=False),
        patch("soothe.runner.shell_drain.os.killpg"),
    ):
        drain_goal_runtime(str(workspace), grace_seconds=0.0)

    assert not (bg_dir / "bg-903.log").exists()
    assert not (bg_dir / "bg-903.log.meta").exists()
