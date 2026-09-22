"""Daemon health check that scans active loops for stall signatures.

A loop is considered stalled when ALL hold:

* ``status == "running"`` in the persistence layer
* ``updated_at`` is older than the configurable stall threshold
  (``loop_status_reconciliation.stall_timeout_minutes``, default 30 min)
* no live runner process is associated with the loop

The check queries the persistence layer directly (creating a short-lived
manager when the daemon is not in-process) so it works from both the CLI
doctor and an in-process daemon context. When stalled loops are found, a
``loop_stall_detected`` event is emitted to the daemon EventBus when one
is available, and the check returns WARNING.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from soothe.config import SootheConfig

from soothe_daemon.config import SootheDaemonConfig
from soothe_daemon.health.formatters import aggregate_status
from soothe_daemon.health.models import CategoryResult, CheckResult, CheckStatus

logger = logging.getLogger(__name__)

# Wire event type emitted when a stalled loop is detected. Published to the
# daemon EventBus topic ``loop:{loop_id}`` when a bus instance is available.
LOOP_STALL_DETECTED = "soothe.loop.stall_detected"

_DEFAULT_STALL_TIMEOUT_MINUTES = 30


def _parse_updated_at(raw: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp from a loop row's ``updated_at`` field."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        normalized = re.sub(r"Z$", "+00:00", raw.strip())
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_process_alive(pid: int) -> bool:
    """Return whether the process with *pid* is currently running."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _stall_timeout_minutes(daemon_config: SootheDaemonConfig | None) -> float:
    """Resolve the stall timeout from daemon config, falling back to default."""
    if daemon_config is None:
        return float(_DEFAULT_STALL_TIMEOUT_MINUTES)
    recon = getattr(daemon_config, "loop_status_reconciliation", None)
    if recon is None:
        return float(_DEFAULT_STALL_TIMEOUT_MINUTES)
    return float(getattr(recon, "stall_timeout_minutes", _DEFAULT_STALL_TIMEOUT_MINUTES))


def _loop_has_active_runner(daemon: Any, loop_id: str) -> bool:
    """Check whether the daemon has an active runner for *loop_id*.

    Mirrors the liveness probe in `auto_resume._loop_has_active_runner` so
    the stall scanner does not false-positive on loops with in-flight
    runners that simply have not checkpointed recently.
    """
    if loop_id in getattr(daemon, "_active_stream_loop_ids", set()):
        return True
    if loop_id in getattr(daemon, "_loops_with_active_query", set()):
        return True
    qe = getattr(daemon, "_query_engine", None)
    if qe is not None:
        runners = getattr(qe, "_active_runners", {}) or {}
        if loop_id in runners:
            return True
        starting = getattr(qe, "_loops_turn_starting", set()) or set()
        if loop_id in starting:
            return True
    return False


async def _emit_stall_event(
    daemon: Any,
    loop_id: str,
    *,
    updated_at: str | None,
    age_minutes: float,
) -> None:
    """Publish a ``loop_stall_detected`` event to the daemon EventBus.

    Best-effort: silently skips when no EventBus or daemon is available so
    the health check never fails due to event emission.
    """
    bus = getattr(daemon, "_event_bus", None)
    if bus is None:
        return
    topic = f"loop:{loop_id}"
    event = {
        "type": LOOP_STALL_DETECTED,
        "loop_id": loop_id,
        "updated_at": updated_at,
        "age_minutes": round(age_minutes, 1),
        "detected_at": datetime.now(UTC).isoformat(),
    }
    try:
        await bus.publish(topic, event)
    except Exception:
        logger.debug("loop_stall event publish failed loop=%s", loop_id, exc_info=True)


async def _list_running_loops(
    config: SootheConfig | None,
    daemon: Any | None,
) -> list[dict]:
    """Return running loop rows from the persistence layer.

    When *daemon* has an in-process persistence manager, reuse it. Otherwise
    create a short-lived manager to query the backend directly.
    """
    persistence = None
    owned = False
    if daemon is not None:
        persistence = getattr(daemon, "_persistence_manager", None)
    if persistence is None and config is not None:
        try:
            from soothe.sloop.checkpoints.manager import (
                StrangeLoopCheckpointPersistenceManager,
            )

            persistence = StrangeLoopCheckpointPersistenceManager(config=config)
            owned = True
        except Exception:
            logger.debug("Stall check: persistence manager init failed", exc_info=True)
            return []
    if persistence is None:
        return []
    try:
        list_loops = getattr(persistence, "list_loops", None)
        if list_loops is None:
            return []
        return await list_loops(status_filter="running", limit=500)
    except Exception:
        logger.debug("Stall check: list_loops failed", exc_info=True)
        return []
    finally:
        if owned:
            close = getattr(persistence, "close", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    logger.debug("Stall check: persistence close failed", exc_info=True)


async def check_loop_stall(
    config: SootheConfig | None = None,
    daemon_config: SootheDaemonConfig | None = None,
    *,
    daemon: Any | None = None,
    now: datetime | None = None,
) -> CategoryResult:
    """Scan running loops for stall signatures.

    A loop is flagged as stalled when its ``updated_at`` is older than the
    stall timeout AND no active runner is associated with it. Stalled loops
    trigger a ``loop_stall_detected`` EventBus event when a daemon bus is
    available.

    Args:
        config: SootheConfig for persistence backend selection.
        daemon_config: SootheDaemonConfig for the stall timeout value.
        daemon: Optional running daemon instance (for EventBus and active
            runner checks). When None, the check queries persistence
            directly and skips active-runner liveness probing.
        now: Override for the current timestamp (testing).

    Returns:
        CategoryResult with one check per stalled loop plus a summary.
    """
    now_utc = now or datetime.now(UTC)
    timeout_minutes = _stall_timeout_minutes(daemon_config)
    stale_before = now_utc - timedelta(minutes=timeout_minutes)

    rows = await _list_running_loops(config, daemon)
    if not rows:
        return CategoryResult(
            category="loop_stall",
            status=CheckStatus.OK,
            checks=[
                CheckResult(
                    name="loop_stall_scan",
                    status=CheckStatus.OK,
                    message="No running loops found",
                    details={
                        "timeout_minutes": timeout_minutes,
                        "scanned_count": 0,
                        "stalled_count": 0,
                        "scanned_at": now_utc.isoformat(),
                    },
                )
            ],
            message="No running loops to scan",
        )

    stalled: list[CheckResult] = []
    scanned = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        loop_id = str(row.get("loop_id") or "").strip()
        if not loop_id:
            continue
        scanned += 1
        updated_at_raw = row.get("updated_at")
        updated_at = _parse_updated_at(updated_at_raw)
        if updated_at is None or updated_at >= stale_before:
            continue
        if daemon is not None and _loop_has_active_runner(daemon, loop_id):
            continue
        age_minutes = max(0.0, (now_utc - updated_at).total_seconds() / 60.0)
        await _emit_stall_event(
            daemon,
            loop_id,
            updated_at=updated_at_raw if isinstance(updated_at_raw, str) else None,
            age_minutes=age_minutes,
        )
        stalled.append(
            CheckResult(
                name=f"loop_stall:{loop_id}",
                status=CheckStatus.WARNING,
                message=(
                    f"Loop {loop_id} stalled: no checkpoint update for "
                    f"{age_minutes:.1f} min (threshold {timeout_minutes:.0f} min)"
                ),
                details={
                    "loop_id": loop_id,
                    "status": row.get("status"),
                    "updated_at": updated_at_raw,
                    "age_minutes": round(age_minutes, 1),
                    "timeout_minutes": timeout_minutes,
                    "current_thread_id": row.get("current_thread_id"),
                    "remediation": ("Inspect loop state; consider manual resume or cancel"),
                },
            )
        )

    checks: list[CheckResult] = [
        CheckResult(
            name="loop_stall_scan",
            status=CheckStatus.OK if not stalled else CheckStatus.WARNING,
            message=(f"Scanned {scanned} running loop(s); {len(stalled)} stalled"),
            details={
                "timeout_minutes": timeout_minutes,
                "scanned_count": scanned,
                "stalled_count": len(stalled),
                "scanned_at": now_utc.isoformat(),
            },
        )
    ]
    checks.extend(stalled)

    overall = aggregate_status([c.status for c in checks])
    if stalled:
        message = f"{len(stalled)} stalled loop(s) detected"
    else:
        message = f"No stalls among {scanned} running loop(s)"

    return CategoryResult(
        category="loop_stall",
        status=overall,
        checks=checks,
        message=message,
    )


__all__ = ["LOOP_STALL_DETECTED", "check_loop_stall"]
