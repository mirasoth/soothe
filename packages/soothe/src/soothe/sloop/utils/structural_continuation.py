"""Structural loop-continuation controls."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from soothe.sloop.utils.continue_keyword import is_interrupt_resume_keyword

if TYPE_CHECKING:
    from soothe.sloop.state.execution_checkpoint import GoalIndexEntry

# Explicit multi-word loop-resume phrases (single-word keywords live in continue_keyword).
_LOOP_CONTINUATION_PHRASE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcontinue\s+(?:this|the|current)\s+loop\b", re.IGNORECASE),
    re.compile(r"\bresume\s+(?:this|the|current)\s+loop\b", re.IGNORECASE),
    re.compile(r"\bproceed\s+(?:with\s+)?(?:this|the|current)\s+loop\b", re.IGNORECASE),
    re.compile(r"\bcontinue\s+this\s+loop\s+to\s+finish\b", re.IGNORECASE),
    re.compile(r"\bretry\s+(?:this|the|current)\s+(?:goal|loop|task)\b", re.IGNORECASE),
)


def is_loop_continuation_phrase(text: str | None) -> bool:
    """True when *text* is an explicit multi-word loop-resume phrase."""
    normalized = (text or "").strip()
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in _LOOP_CONTINUATION_PHRASE_PATTERNS)


def is_loop_control_signal(text: str | None) -> bool:
    """Return True when *text* is a deterministic loop-control signal."""
    return is_interrupt_resume_keyword(text) or is_loop_continuation_phrase(text)


def _active_goal_record(checkpoint: Any) -> GoalIndexEntry | None:
    idx = getattr(checkpoint, "current_goal_index", -1)
    history = getattr(checkpoint, "goal_history", None) or []
    if not isinstance(idx, int) or idx < 0 or idx >= len(history):
        return None
    return history[idx]


def has_active_running_goal(checkpoint: Any | None) -> bool:
    """True when the checkpoint's active goal is still running."""
    if checkpoint is None:
        return False
    goal = _active_goal_record(checkpoint)
    return goal is not None and getattr(goal, "status", None) == "running"


def has_resumable_interrupted_goal(checkpoint: Any | None) -> bool:
    """True when the checkpoint holds a running/cancelled/interrupted goal that
    a retry/continue/resume can reactivate in place."""
    if checkpoint is None:
        return False
    goal = _active_goal_record(checkpoint)
    if goal is None:
        return False
    status = getattr(goal, "status", None)
    return status in ("running", "cancelled", "interrupted")


__all__ = [
    "has_active_running_goal",
    "has_resumable_interrupted_goal",
    "is_loop_continuation_phrase",
    "is_loop_control_signal",
]
