"""Tests for structural loop-continuation controls."""

from __future__ import annotations

from datetime import UTC, datetime

from soothe.sloop.state.execution_checkpoint import GoalIndexEntry
from soothe.sloop.utils.structural_continuation import (
    has_active_running_goal,
    has_resumable_interrupted_goal,
    is_loop_continuation_phrase,
    is_loop_control_signal,
)


def _goal(*, status: str = "running") -> GoalIndexEntry:
    now = datetime.now(UTC)
    return GoalIndexEntry(
        goal_id="goal-0",
        status=status,  # type: ignore[arg-type]
        thread_id="loop-1",
        started_at=now,
        completed_at=None,
        duration_ms=0,
        tokens_used=0,
    )


def _checkpoint(
    *,
    status: str,
    goal_status: str = "running",
    current_goal_index: int = 0,
    empty_history: bool = False,
):
    from types import SimpleNamespace

    return SimpleNamespace(
        status=status,
        current_goal_index=current_goal_index,
        goal_history=[] if empty_history else [_goal(status=goal_status)],
    )


def test_loop_continuation_phrase_matches_common_resume_text() -> None:
    assert is_loop_continuation_phrase("continue this loop")
    assert is_loop_continuation_phrase("Continue current loop")
    assert is_loop_continuation_phrase("continue this loop to finish all")
    assert is_loop_continuation_phrase("resume the loop")


def test_loop_continuation_phrase_matches_embedded_resume_text() -> None:
    assert is_loop_continuation_phrase("Finish the integration tests, then continue the loop")
    assert is_loop_continuation_phrase("Run the suite again. continue this loop")


def test_loop_continuation_phrase_rejects_unrelated_text() -> None:
    assert not is_loop_continuation_phrase("continue cleaning")
    assert not is_loop_continuation_phrase("thanks")
    assert not is_loop_continuation_phrase("")


def test_is_loop_control_signal_includes_keyword_and_phrase() -> None:
    assert is_loop_control_signal("continue")
    assert is_loop_control_signal("retry")
    assert is_loop_control_signal("continue this loop")
    assert is_loop_control_signal("retry this goal")
    assert not is_loop_control_signal("hello")


def test_has_active_running_goal() -> None:
    assert has_active_running_goal(_checkpoint(status="running", goal_status="running"))
    assert not has_active_running_goal(_checkpoint(status="idle", goal_status="completed"))


def test_has_resumable_interrupted_goal() -> None:
    assert has_resumable_interrupted_goal(_checkpoint(status="idle", goal_status="running"))
    assert has_resumable_interrupted_goal(_checkpoint(status="running", goal_status="running"))
    assert has_resumable_interrupted_goal(_checkpoint(status="idle", goal_status="cancelled"))
    # Regression: an interrupted goal (user cancel via mark_goal_interrupted) is
    # resumable so a retry/continue/resume re-activates it in place.
    assert has_resumable_interrupted_goal(_checkpoint(status="idle", goal_status="interrupted"))
    assert not has_resumable_interrupted_goal(_checkpoint(status="idle", goal_status="completed"))
