"""Wire binding of step retry/recovery events into STEP_PROGRESS cards."""

from __future__ import annotations

from soothe.display.card_binder import (
    convert_event_to_message_data,
    merge_step_progress,
)
from soothe.display.transcript_types import MessageType


def _event_row(event_type: str, payload: dict[str, object]) -> dict[str, object]:
    """Persisted activity-event row (kind=event) with the typed payload."""
    return {
        "kind": "event",
        "timestamp": "2026-01-01T00:00:00Z",
        "data": {"type": event_type, **payload},
    }


_RETRY_EVENT = _event_row(
    "soothe.cognition.strange_loop.step.retry",
    {
        "step_id": "s1",
        "attempt": 2,
        "max_attempts": 5,
        "error_preview": "model pool exhausted",
        "prior_tool_calls": 12,
        "prior_duration_ms": 27_000,
        "next_backoff_ms": 4_000,
    },
)

_COMPLETED_RECOVERED_EVENT = _event_row(
    "soothe.cognition.strange_loop.step.completed",
    {
        "step_id": "s1",
        "description": "flaky step",
        "success": True,
        "summary": "done",
        "duration_ms": 40_000,
        "tool_call_count": 5,
        "retry_attempt": 3,
        "recovered_by_retry": True,
        "prior_attempts_tool_calls": 12,
        "prior_attempts_duration_ms": 27_000,
    },
)

_STARTED_EVENT = _event_row(
    "soothe.cognition.strange_loop.step.started",
    {"step_id": "s1", "description": "flaky step"},
)


def test_step_retry_event_binds_to_running_card_with_retry_meta() -> None:
    card = convert_event_to_message_data(_RETRY_EVENT)
    assert card is not None
    assert card.type == MessageType.STEP_PROGRESS
    assert card.step_progress_id == "s1"
    assert card.step_progress_phase == "running"
    assert card.step_retry_attempt == 2
    assert card.step_retry_max == 5
    assert card.step_prior_tool_calls == 12
    assert card.step_prior_duration_ms == 27_000


def test_step_retry_event_without_attempt_is_dropped() -> None:
    event = {**_RETRY_EVENT, "data": {**_RETRY_EVENT["data"], "attempt": 0}}
    assert convert_event_to_message_data(event) is None


def test_recovered_completion_carries_recovery_meta() -> None:
    card = convert_event_to_message_data(_COMPLETED_RECOVERED_EVENT)
    assert card is not None
    assert card.step_progress_phase == "success"
    assert card.step_retry_attempt == 3
    assert card.step_recovered is True
    assert card.step_prior_tool_calls == 12
    assert card.step_prior_duration_ms == 27_000


def test_plain_completion_has_no_retry_meta() -> None:
    event = {
        **_COMPLETED_RECOVERED_EVENT,
        "data": {
            k: v
            for k, v in _COMPLETED_RECOVERED_EVENT["data"].items()
            if not k.startswith(("retry_", "recovered_", "prior_attempts_"))
        },
    }
    card = convert_event_to_message_data(event)
    assert card is not None
    assert card.step_retry_attempt is None
    assert card.step_recovered is None
    assert card.step_prior_tool_calls is None


def test_merge_keeps_latest_retry_attempt_and_recovery() -> None:
    started = convert_event_to_message_data(_STARTED_EVENT)
    retry = convert_event_to_message_data(_RETRY_EVENT)
    completed = convert_event_to_message_data(_COMPLETED_RECOVERED_EVENT)
    assert started is not None and retry is not None and completed is not None

    merged = merge_step_progress(started, retry)
    assert merged.step_retry_attempt == 2
    assert merged.step_retry_max == 5
    assert merged.step_progress_phase == "running"
    assert merged.step_progress_description == "flaky step"

    merged = merge_step_progress(merged, completed)
    assert merged.step_retry_attempt == 3
    assert merged.step_recovered is True
    assert merged.step_progress_phase == "success"
    # The started card's description survives the merge chain.
    assert merged.step_progress_description == "flaky step"
    assert merged.step_summary == "done"
