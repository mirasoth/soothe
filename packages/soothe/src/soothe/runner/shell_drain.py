"""Goal-runtime shell drain — kill a goal's spawned shell process groups."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import signal
import time
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from soothe_nano.toolkits.shell import (
        PROCESS_META_SCHEMA,
        process_start_token,
    )
except ImportError:  # pragma: no cover — soothe-nano without marker sidecars
    # Spawn-marker ownership sidecars ship with an unreleased soothe-nano;
    # with an older nano the markers carry no sidecar at all, so validation
    # degrades to filename + protected-set checks. Loud, one-time warning —
    # never a silent downgrade. The dependency floor is bumped together
    # with the nano release (release-train timing).
    PROCESS_META_SCHEMA = None
    process_start_token = None
    logger.warning(
        "soothe-nano predates spawn-marker ownership sidecars; the shell "
        "drain falls back to filename + protected-set validation only "
        "(upgrade soothe-nano)"
    )

# Default SIGTERM→SIGKILL grace period when draining shell spawns.
_DRAIN_GRACE_SECONDS = 2.0
_BG_LOG_PID_RE = re.compile(r"bg-(\d+)\.log$")
_FG_SESSION_PID_RE = re.compile(r"fg-(\d+)\.session$")
# Bound on the ancestor walk (cycle / runaway guards).
_MAX_ANCESTOR_STEPS = 64


def _process_group_id(pid: int) -> int | None:
    """Return `pid`'s process group id, or `None` if it cannot be resolved."""
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def _parent_of(pid: int) -> int | None:
    """Return `pid`'s parent PID; `None` when the walk cannot continue.

    Uses `/proc/<pid>/status` (Linux) or `ps -o ppid=` (macOS/BSD) so the
    walk covers grandparents too — a drain must never kill the process
    ancestry of the runner itself (the daemon and its launcher).
    """
    proc_stat = Path("/proc") / str(pid) / "status"
    if proc_stat.is_file():
        try:
            for line in proc_stat.read_text(encoding="utf-8").splitlines():
                if line.startswith("PPid:"):
                    return int(line.split()[1])
        except (OSError, ValueError, IndexError):
            return None
        return None
    try:
        import subprocess

        out = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=True,
            timeout=2.0,
        ).stdout.strip()
        return int(out) if out.isdigit() else None
    except Exception:  # noqa: BLE001 — `ps` missing or PID gone
        return None


def _protected_process_ids() -> set[int]:
    """PIDs/PGIDs that a workspace drain must never signal.

    Covers this process, its process group, and the full parent chain (with
    each ancestor's group). Marker files live inside the agent workspace, so
    their content is untrusted input: without this guard a forged marker
    naming the daemon (or its launcher) would make the drain kill the very
    process running it — the one kill path the agent's own `kill_process`
    guards already block.
    """
    protected: set[int] = set()
    for pid in (os.getpid(), os.getpgrp(), os.getppid()):
        protected.add(pid)
        pgid = _process_group_id(pid)
        if pgid is not None:
            protected.add(pgid)
    parent = os.getppid()
    seen: set[int] = set()
    steps = 0
    while parent > 1 and parent not in seen and steps < _MAX_ANCESTOR_STEPS:
        seen.add(parent)
        protected.add(parent)
        pgid = _process_group_id(parent)
        if pgid is not None:
            protected.add(pgid)
        nxt = _parent_of(parent)
        if nxt is None:
            break
        parent = nxt
        steps += 1
    return protected


def _kill_pgid(pgid: int, *, sig: int) -> bool:
    """Send `sig` to a process group. True if delivered, False if gone."""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        logger.warning("drain_goal_runtime: no permission to signal pgid=%d", pgid)
        return False
    except OSError as exc:
        logger.debug("drain_goal_runtime: killpg(%d, %d) failed: %s", pgid, sig, exc)
        return False
    return True


def _pid_alive(pid: int) -> bool:
    """True if `pid` exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def _unlink_marker(marker: Path) -> None:
    """Remove a spawn marker together with its ownership sidecar."""
    with contextlib.suppress(OSError):
        marker.unlink(missing_ok=True)
        marker.with_name(marker.name + ".meta").unlink(missing_ok=True)


def _marker_owner_valid(marker: Path, *, pid: int, pgid: int) -> bool:
    """True when the marker's ownership sidecar matches the live process.

    Marker filenames (and sidecars) live in the agent-writable workspace, so
    a name alone must never authorize a kill: the spawn-time identity —
    recorded pgid plus the OS start-time token — must match the process the
    marker names. A missing sidecar (pre-upgrade marker, or a spawn that
    could not record one) refuses the kill; the marker is kept for
    inspection. With a soothe-nano that predates sidecars entirely, there is
    nothing to validate and the marker passes (filename + protected-set
    checks still apply).
    """
    if process_start_token is None:  # old nano without sidecar support
        return True
    meta_path = marker.with_name(marker.name + ".meta")
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning(
            "drain_goal_runtime: refusing to reap pid=%d (marker=%s) — no "
            "ownership sidecar; the marker does not name a tool-spawned "
            "process",
            pid,
            marker.name,
        )
        return False
    except (OSError, ValueError):
        logger.warning(
            "drain_goal_runtime: refusing to reap pid=%d (marker=%s) — "
            "ownership sidecar is unreadable",
            pid,
            marker.name,
        )
        return False

    if not isinstance(raw, dict) or raw.get("schema") != PROCESS_META_SCHEMA:
        logger.warning(
            "drain_goal_runtime: refusing to reap pid=%d (marker=%s) — "
            "ownership sidecar schema mismatch",
            pid,
            marker.name,
        )
        return False
    if raw.get("pid") != pid:
        logger.warning(
            "drain_goal_runtime: refusing to reap pid=%d (marker=%s) — sidecar pid mismatch (%r)",
            pid,
            marker.name,
            raw.get("pid"),
        )
        return False
    recorded_pgid = raw.get("pgid")
    if isinstance(recorded_pgid, int) and recorded_pgid != pgid:
        logger.warning(
            "drain_goal_runtime: refusing to signal pgid=%d (marker=%s) — "
            "sidecar pgid mismatch (%d)",
            pgid,
            marker.name,
            recorded_pgid,
        )
        return False
    recorded_start = raw.get("start_time")
    if isinstance(recorded_start, str) and recorded_start:
        live_start = process_start_token(pid)
        if live_start is not None and live_start != recorded_start:
            logger.warning(
                "drain_goal_runtime: refusing to reap pid=%d (marker=%s) — "
                "process start-time mismatch (PID recycled or forged marker)",
                pid,
                marker.name,
            )
            return False
    return True


def _reap_tracked_shell_pids(
    marker_dir: Path,
    *,
    glob_pattern: str,
    pid_re: re.Pattern[str],
    grace_seconds: float,
) -> int:
    """SIGTERM→SIGKILL process groups named by marker files under `marker_dir`."""
    if not marker_dir.is_dir():
        return 0
    protected = _protected_process_ids()
    reaped = 0
    for marker in marker_dir.glob(glob_pattern):
        match = pid_re.search(marker.name)
        if match is None:
            continue
        pid = int(match.group(1))
        if pid in protected:
            logger.warning(
                "drain_goal_runtime: refusing to reap protected pid=%d "
                "(marker=%s) — the marker does not name a spawned shell",
                pid,
                marker.name,
            )
            continue
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            _unlink_marker(marker)
            continue
        except (OSError, PermissionError):
            continue
        if pgid in protected:
            logger.warning(
                "drain_goal_runtime: refusing to signal protected pgid=%d "
                "(marker=%s) — the marker does not name a spawned shell",
                pgid,
                marker.name,
            )
            continue
        if not _pid_alive(pid):
            _unlink_marker(marker)
            continue
        if not _marker_owner_valid(marker, pid=pid, pgid=pgid):
            continue
        if not _kill_pgid(pgid, sig=signal.SIGTERM):
            continue
        reaped += 1
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.05)
        if _pid_alive(pid):
            _kill_pgid(pgid, sig=signal.SIGKILL)
        _unlink_marker(marker)
    return reaped


def drain_goal_runtime(workspace: str, *, grace_seconds: float = _DRAIN_GRACE_SECONDS) -> int:
    """Kill shell processes this goal spawned (`run_command` + `run_background`).

    Workspace-scoped: only touches PIDs whose markers live under THIS workspace.
    Not a global `ps` scan. Marker filenames are untrusted input (they live in
    the agent-writable workspace), so the drain refuses to signal this process
    or any of its ancestors/ancestor groups (see `_protected_process_ids`) and
    requires the spawn-time ownership sidecar (pgid + start-time token,
    `_marker_owner_valid`) to match before signaling. Safe at goal completion
    and on cancel.

    Args:
        workspace: Workspace path that owns the spawned shell markers.
        grace_seconds: SIGTERM→SIGKILL grace period.

    Returns:
        Count of process groups reaped.
    """
    if not workspace:
        return 0
    root = Path(workspace).expanduser() / ".soothe"
    reaped = _reap_tracked_shell_pids(
        root / "foreground",
        glob_pattern="fg-*.session",
        pid_re=_FG_SESSION_PID_RE,
        grace_seconds=grace_seconds,
    )
    reaped += _reap_tracked_shell_pids(
        root / "background",
        glob_pattern="bg-*.log",
        pid_re=_BG_LOG_PID_RE,
        grace_seconds=grace_seconds,
    )
    if reaped:
        logger.info(
            "drain_goal_runtime: reaped %d shell process group(s) under %s",
            reaped,
            workspace,
        )
    return reaped


__all__ = ["drain_goal_runtime"]
