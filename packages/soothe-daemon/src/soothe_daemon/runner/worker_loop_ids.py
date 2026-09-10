"""Helpers for classifying loop IDs that belong to internal worker pools.

These helpers were relocated from the retired ``soothe_autopilot.workers.pool``
module so the daemon no longer depends on the deleted package.
"""

from __future__ import annotations

# Loop IDs assigned to internal autopilot worker subprocesses use this prefix.
# The prefix is a stable contract: persistence layers, session filters, and the
# auto-resume classifier all key off it to skip / gate these internal loops.
_AUTOPILOT_WORKER_PREFIX = "autopilot__"


def is_autopilot_worker_loop_id(loop_id: str | None) -> bool:
    """Return True when *loop_id* belongs to an internal autopilot worker.

    Internal worker loops are never user-facing sessions and must not be
    exposed via subscribe / auto-resume paths.
    """
    return bool(loop_id and str(loop_id).startswith(_AUTOPILOT_WORKER_PREFIX))
