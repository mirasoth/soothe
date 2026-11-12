"""PCE event plumbing.

Bounded per-loop queue of step-boundary and budget-milestone events.
Stations and the executor emit fire-and-forget via `emit_pce_event`;
the PCE monitor drains the queue. Events are wake-up hints — dropping
one under overflow is safe (the next event or the elapsed timeout
re-triggers the pass).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_QUEUE_MAXSIZE = 256

PCE_EVENT_KINDS = (
    "step_started",
    "step_completed",
    "step_failed",
    "decompose_committed",
    "close_report_filed",
    "tool_milestone",
    "elapsed_milestone",
)

# Step lifecycle events that escalate a typesafe verdict to an LLM pass.
BOUNDARY_EVENT_KINDS = frozenset(
    {
        "step_started",
        "step_completed",
        "step_failed",
        "decompose_committed",
        "close_report_filed",
    }
)


@dataclass
class PceEvent:
    """Wake-up hint for the PCE monitor."""

    kind: str
    step_id: str | None = None
    goal_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class PceEventQueue:
    """Bounded queue that drops the oldest event on overflow."""

    def __init__(self, maxsize: int = _QUEUE_MAXSIZE) -> None:
        self._queue: asyncio.Queue[PceEvent] = asyncio.Queue(maxsize=maxsize)

    def put(self, event: PceEvent) -> None:
        """Enqueue, dropping the oldest entry when full (never blocks)."""
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait(event)

    async def get(self) -> PceEvent:
        return await self._queue.get()

    def drain(self, limit: int = 64) -> list[PceEvent]:
        """Pop up to `limit` queued events without waiting."""
        out: list[PceEvent] = []
        while len(out) < limit:
            try:
                out.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return out

    def empty(self) -> bool:
        return self._queue.empty()


# Per-loop registries so deep executor/station code can emit without
# threading a queue handle through the graph state, and so stations can
# look up the running monitor (e.g. the root_eval gate settle).
_LOOP_QUEUES: dict[str, PceEventQueue] = {}
_LOOP_MONITORS: dict[str, Any] = {}


def register_loop(loop_id: str, queue: PceEventQueue) -> None:
    _LOOP_QUEUES[loop_id] = queue


def unregister_loop(loop_id: str) -> None:
    _LOOP_QUEUES.pop(loop_id, None)
    _LOOP_MONITORS.pop(loop_id, None)


def register_monitor(loop_id: str, monitor: Any) -> None:
    _LOOP_MONITORS[loop_id] = monitor


def lookup_monitor(loop_id: str | None) -> Any | None:
    return _LOOP_MONITORS.get(loop_id or "")


def emit_pce_event(
    loop_id: str | None,
    kind: str,
    *,
    step_id: str | None = None,
    goal_id: str | None = None,
    **payload: Any,
) -> None:
    """Fire-and-forget event emission; never raises into the caller."""
    if not loop_id or kind not in PCE_EVENT_KINDS:
        return
    queue = _LOOP_QUEUES.get(loop_id)
    if queue is None:
        return
    try:
        queue.put(PceEvent(kind=kind, step_id=step_id, goal_id=goal_id, payload=payload))
    except Exception:  # noqa: BLE001 — emission must never block execution
        logger.debug("[pce] event emission failed (loop=%s kind=%s)", loop_id, kind)


def reset_pce_registry() -> None:
    """Clear the loop registries (test isolation)."""
    _LOOP_QUEUES.clear()
    _LOOP_MONITORS.clear()
