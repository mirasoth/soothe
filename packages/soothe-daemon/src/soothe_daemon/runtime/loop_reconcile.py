"""Reconcile filesystem loop directories with `agentloop_loops` rows."""

from __future__ import annotations

import logging
import shutil
from typing import Any

from soothe.sloop.checkpoints.directory_manager import (
    PersistenceDirectoryManager,
)

logger = logging.getLogger(__name__)

# Upper bound for the known-loop enumeration. `list_loops` has no offset/cursor
# pagination, so a result of exactly this many rows means the store may hold
# more loops than we enumerated — the known-id set is then potentially
# truncated and deletion must not run.
_ENUMERATION_CAP = 10000


async def reconcile_orphan_loop_directories(
    persistence_manager: Any,
    *,
    limit: int = 200,
) -> int:
    """Delete `data/loops/{loop_id}/` trees with no matching DB row.

    The known-id set is enumerated up to `_ENUMERATION_CAP` rows; when the
    store returns that many rows the set may be truncated, so deletion is
    skipped entirely and a warning is logged — loops we could not enumerate
    must never have their directories deleted.

    Args:
        persistence_manager: Daemon persistence manager with `list_loops`.
        limit: Maximum orphan directories to delete per invocation.

    Returns:
        Number of directories removed.
    """
    loops_dir = PersistenceDirectoryManager.get_loops_directory()
    if not loops_dir.is_dir():
        return 0

    list_loops = getattr(persistence_manager, "list_loops", None)
    if list_loops is None:
        return 0

    try:
        rows = await list_loops(limit=_ENUMERATION_CAP)
    except Exception:
        logger.warning("Failed to list loops for orphan reconcile", exc_info=True)
        return 0

    known_ids = {
        str(row.get("loop_id") or "").strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("loop_id") or "").strip()
    }

    if len(rows) >= _ENUMERATION_CAP:
        logger.warning(
            "Loop enumeration hit the %d-row cap; known-id set may be truncated — "
            "skipping orphan directory deletion this tick",
            _ENUMERATION_CAP,
        )
        return 0

    removed = 0
    for child in sorted(loops_dir.iterdir()):
        if not child.is_dir():
            continue
        loop_id = child.name
        if loop_id in known_ids:
            continue
        try:
            shutil.rmtree(child)
            removed += 1
            logger.info("Removed orphan loop directory: %s", loop_id)
        except Exception:
            logger.warning("Failed to remove orphan loop directory %s", child, exc_info=True)
        if removed >= limit:
            break
    return removed


__all__ = ["reconcile_orphan_loop_directories"]
