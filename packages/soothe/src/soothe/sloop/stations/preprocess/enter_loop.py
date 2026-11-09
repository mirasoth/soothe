"""Loop Graph `init_or_resume` node."""

from __future__ import annotations

import logging
from typing import Any

from soothe.sloop.engine.execute.thread_selection import resolve_user_requested_wire_subagent
from soothe.sloop.orchestrator.runtime_context import LoopRuntimeContext

logger = logging.getLogger(__name__)


def _graph_flags(
    *,
    intent_route: str,
    interaction_mode: str | None = None,
) -> dict[str, Any]:
    return {
        "intent_route": intent_route,
        "last_outcome": None,
        "dispatch_route": None,
        "reconcile_route": None,
        "root_eval_route": None,
        "interaction_mode": interaction_mode,
        "plan_rejected_terminal": None,
    }


async def node_init_or_resume(ctx: LoopRuntimeContext, _state: dict[str, Any]) -> dict[str, Any]:
    """Route wired-subagent requests to DELEGATE; everything else to DISPATCH."""
    wire = resolve_user_requested_wire_subagent(
        routing_classification=getattr(ctx.loop_state, "routing_classification", None),
        preferred_subagent=getattr(ctx, "preferred_subagent", None),
    )
    if wire:
        logger.info(
            "[Intent] Wired subagent branch selected (subagent=%s)",
            wire,
        )
        return _graph_flags(
            intent_route="wired_subagent",
            interaction_mode=getattr(ctx, "interaction_mode", None),
        )

    return _graph_flags(
        intent_route="continue_loop",
        interaction_mode=getattr(ctx, "interaction_mode", None),
    )
