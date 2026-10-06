"""Helpers for classifying loop IDs that belong to internal worker pools.

.. deprecated::
    The autopilot worker prefix and its classifier are deprecated. The
    session-layer filter that consumed them has been removed. The only
    remaining caller is ``auto_resume`` skip logic, which will be migrated
    to a worker-pool ownership check in a follow-up.
"""

from __future__ import annotations

import warnings

# Loop IDs assigned to internal autopilot worker subprocesses use this prefix.
# Deprecated: will be removed once auto_resume migrates off prefix-based checks.
_AUTOPILOT_WORKER_PREFIX = "autopilot__"


def is_autopilot_worker_loop_id(loop_id: str | None) -> bool:
    """Return True when *loop_id* belongs to an internal autopilot worker.

    .. deprecated::
        Prefix-based worker classification is deprecated. The session filter
        that relied on it has been removed. Only ``auto_resume`` still calls
        this function; it will be replaced by a worker-pool ownership check.
    """
    warnings.warn(
        "is_autopilot_worker_loop_id is deprecated and will be removed in a "
        "future release; use worker-pool ownership metadata instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return bool(loop_id and str(loop_id).startswith(_AUTOPILOT_WORKER_PREFIX))
