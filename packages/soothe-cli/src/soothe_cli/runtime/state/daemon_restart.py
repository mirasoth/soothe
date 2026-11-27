"""Daemon restart detection helpers (IG-DAEM-004).

Pure functions for comparing daemon identities across (re)connections. The
daemon's `daemon_info` RPC returns a `daemon_id` that is stable for one daemon
process lifetime (`"<pid>:<started_at>"`), so a changed epoch means the TUI is
talking to a restarted daemon.
"""

from __future__ import annotations

from typing import Any


def daemon_epoch(info: Any) -> str | None:
    """Extract the stable per-process daemon epoch from a `daemon_info` result.

    Returns None when the info payload is unusable (older daemon, RPC failure
    upstream) so callers treat the epoch as *unknown* rather than as a restart.
    """
    if not isinstance(info, dict):
        return None
    daemon_id = info.get("daemon_id")
    if isinstance(daemon_id, str) and daemon_id.strip():
        return daemon_id.strip()
    pid = info.get("pid")
    started_at = info.get("started_at")
    if pid is not None and isinstance(started_at, str) and started_at.strip():
        return f"{pid}:{started_at.strip()}"
    return None


def restart_detected(previous: str | None, current: str | None) -> bool:
    """True when a previously observed daemon epoch differs from the current one.

    Unknown epochs on either side never count as a restart.
    """
    if not previous or not current:
        return False
    return previous != current


__all__ = ["daemon_epoch", "restart_detected"]
