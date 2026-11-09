"""Verify resumed step cards against the daemon's durable step state.

A `step.started` event without a matching `step.completed` leaves a display
card in the `running` phase. After a worker or daemon restart that card would
spin forever on the next resume. The ContextEngine goal DAG holds the durable
per-step outcome; an active runner or a parked clarification is the only
reason a step may legitimately still be running.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from soothe.display.transcript_types import MessageData, MessageType

logger = logging.getLogger(__name__)

_STALE_STEP_SUMMARY = "Interrupted - the daemon is no longer running this step"
_UNFINISHED_STEP_SUMMARY = "Interrupted - the step did not complete"

_TERMINAL_PHASE_BY_STATUS: dict[str, str] = {
    "completed": "success",
    "failed": "error",
    "skipped": "interrupted",
    "decomposed": "interrupted",
    "superseded": "interrupted",
}


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """Durable outcome of one step from the ContextEngine goal DAG."""

    status: str
    duration_ms: int = 0
    tool_call_count: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class StepStatusContext:
    """Daemon truth used to verify running step cards for one loop."""

    outcomes: Mapping[str, StepOutcome]
    runner_active: bool
    clarification_pending: bool

    @property
    def step_may_run(self) -> bool:
        """True when a non-terminal step can still be executing."""
        return self.runner_active or self.clarification_pending


def has_running_step_cards(cards: Iterable[MessageData]) -> bool:
    """Return True when any step card is still in the `running` phase."""
    return any(
        card.type == MessageType.STEP_PROGRESS and card.step_progress_phase == "running"
        for card in cards
    )


def _terminal_fields(outcome: StepOutcome) -> dict[str, Any] | None:
    """Return display fields for a durable terminal step outcome."""
    phase = _TERMINAL_PHASE_BY_STATUS.get(outcome.status)
    if phase == "success":
        return {
            "step_progress_phase": "success",
            "step_success": True,
            "step_duration_ms": outcome.duration_ms,
            "step_tool_call_count": outcome.tool_call_count,
            "step_summary": "",
        }
    if phase == "error":
        return {
            "step_progress_phase": "error",
            "step_success": False,
            "step_duration_ms": outcome.duration_ms,
            "step_tool_call_count": outcome.tool_call_count,
            "step_summary": outcome.error or "Step failed",
        }
    if phase == "interrupted":
        return {
            "step_progress_phase": "interrupted",
            "step_summary": _UNFINISHED_STEP_SUMMARY,
        }
    return None


def reconcile_step_cards(
    cards: Iterable[MessageData],
    context: StepStatusContext,
    *,
    allow_running: bool,
) -> list[MessageData]:
    """Replace stale `running` step phases with the daemon's step outcome.

    Args:
        cards: Persisted display cards in transcript order.
        context: Authoritative step outcomes and loop liveness.
        allow_running: Whether cards in this group may still be live. Frozen
            goal snapshots are historical, so they pass False.

    Returns:
        Cards in the same order. Corrected cards are new `MessageData`
        instances; the input ledger state is never mutated.
    """
    result: list[MessageData] = []
    for card in cards:
        if card.type != MessageType.STEP_PROGRESS or card.step_progress_phase != "running":
            result.append(card)
            continue
        step_id = str(card.step_progress_id or "").strip()
        outcome = context.outcomes.get(step_id) if step_id else None
        fields = _terminal_fields(outcome) if outcome is not None else None
        if fields is None:
            if allow_running and context.step_may_run:
                result.append(card)
                continue
            fields = {
                "step_progress_phase": "interrupted",
                "step_summary": _STALE_STEP_SUMMARY,
            }
        result.append(replace(card, **fields))
    return result


async def _load_step_outcomes(daemon: Any, loop_id: str) -> dict[str, StepOutcome]:
    """Read per-step outcomes from the persisted ContextEngine goal DAG."""
    config = getattr(daemon, "_config", None)
    if config is None:
        return {}
    try:
        from soothe.context.store_factory import resolve_context_engine_persistence

        persistence = resolve_context_engine_persistence(config, loop_id)
        dag = await persistence.load_dag()
    except Exception:
        logger.debug("Failed to load step DAG for loop %s", loop_id[:16], exc_info=True)
        return {}
    if dag is None:
        return {}

    goals = getattr(dag, "goals", None)
    if isinstance(goals, dict):
        goal_list: Iterable[Any] = goals.values()
    elif isinstance(goals, (list, tuple)):
        goal_list = goals
    else:
        return {}

    outcomes: dict[str, StepOutcome] = {}
    for goal in goal_list:
        nodes = getattr(getattr(goal, "steps", None), "nodes", None)
        if not isinstance(nodes, dict):
            continue
        for step_id, node in nodes.items():
            execution = getattr(node, "execution", None)
            outcomes[str(step_id)] = StepOutcome(
                status=str(getattr(node, "status", "") or ""),
                duration_ms=int(getattr(execution, "duration_ms", 0) or 0),
                tool_call_count=int(getattr(execution, "tool_call_count", 0) or 0),
                error=getattr(execution, "error", None) or None,
            )
    return outcomes


async def load_step_status_context(daemon: Any, loop_id: str) -> StepStatusContext:
    """Read authoritative step outcomes and loop liveness from the daemon."""
    from soothe_daemon.runtime.auto_resume import (
        _loop_has_active_runner,
        peek_clarification_pending,
    )

    runner_active = bool(_loop_has_active_runner(daemon, loop_id))
    clarification_pending = False
    if not runner_active:
        try:
            clarification_pending = bool(await peek_clarification_pending(daemon, loop_id))
        except Exception:
            logger.debug("Clarification probe failed for loop %s", loop_id[:16], exc_info=True)

    return StepStatusContext(
        outcomes=await _load_step_outcomes(daemon, loop_id),
        runner_active=runner_active,
        clarification_pending=clarification_pending,
    )


async def reconcile_step_card_groups(
    daemon: Any,
    loop_id: str,
    groups: Sequence[tuple[Iterable[MessageData], bool]],
) -> list[list[MessageData]]:
    """Verify running step cards across card groups with one daemon truth read.

    Args:
        daemon: Daemon instance owning the runner and persistence config.
        loop_id: Loop whose cards are being replayed.
        groups: `(cards, allow_running)` pairs; `allow_running` is False for
            frozen historical goal snapshots.

    Returns:
        One reconciled card list per group, in input order.
    """
    materialized = [(list(cards), allow) for cards, allow in groups]
    if not any(has_running_step_cards(cards) for cards, _ in materialized):
        return [cards for cards, _ in materialized]

    context = await load_step_status_context(daemon, loop_id)
    return [
        reconcile_step_cards(cards, context, allow_running=allow) for cards, allow in materialized
    ]


__all__ = [
    "StepOutcome",
    "StepStatusContext",
    "has_running_step_cards",
    "load_step_status_context",
    "reconcile_step_card_groups",
    "reconcile_step_cards",
]
