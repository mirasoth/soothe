"""Tests for instant/complete/correct TUI token display fixes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from soothe_client.appkit.turn import PRIORITY_HIGH
from soothe_client.core.events import STRANGE_LOOP_PLAN_PHASE

from soothe_cli.runtime.state.session_stats import SessionStats
from soothe_cli.runtime.token_events_debug import TokenEventTrace
from soothe_cli.runtime.token_usage import TokenUsage
from soothe_cli.runtime.turn.prepare import (
    _MAIN_LOOP_CUSTOM_TYPES,
    TurnPrepareState,
    _message_priority,
    prepare_turn_chunk,
)
from soothe_cli.tui.widgets.context_data import (
    TokenUsageSnapshot,
    format_token_usage,
)


def test_usage_only_message_gets_high_priority() -> None:
    chunk = {
        "type": "AIMessageChunk",
        "content": "",
        "usage_metadata": {"input_tokens": 1, "total_tokens": 1},
    }
    assert _message_priority(chunk, is_summarization=False) == PRIORITY_HIGH


def test_plan_phase_event_bypasses_batcher() -> None:
    assert STRANGE_LOOP_PLAN_PHASE in _MAIN_LOOP_CUSTOM_TYPES

    state = TurnPrepareState(ev_stats=MagicMock(), presentation=MagicMock())
    prepared = prepare_turn_chunk(
        state, ((), "custom", {"type": STRANGE_LOOP_PLAN_PHASE, "label": "Interpreting goal"})
    )
    assert prepared is not None
    assert prepared.skip is False
    assert prepared.priority == PRIORITY_HIGH


def test_prepare_usage_chunk_is_high_priority() -> None:
    state = TurnPrepareState(ev_stats=MagicMock(), presentation=MagicMock())
    body = {
        "type": "AIMessageChunk",
        "content": "",
        "usage_metadata": {"input_tokens": 3, "total_tokens": 3},
    }
    prepared = prepare_turn_chunk(state, ((), "messages", (body, {})))
    assert prepared is not None
    assert prepared.priority == PRIORITY_HIGH


def test_session_stats_record_full_breakdown() -> None:
    stats = SessionStats()
    stats.record_request("model-a", TokenUsage(100, 20, 80, 5, 120))
    stats.record_request("model-a", TokenUsage(10, 2, 0, 0, 12))

    assert stats.request_count == 2
    assert stats.input_tokens == 110
    assert stats.output_tokens == 22
    assert stats.total_tokens == 132
    assert stats.cache_read_tokens == 80
    assert stats.cache_creation_tokens == 5
    assert stats.per_model["model-a"].cache_read_tokens == 80


def test_session_stats_merge_folds_cache_fields() -> None:
    a = SessionStats(total_tokens=10, cache_read_tokens=5)
    b = SessionStats(total_tokens=3, cache_creation_tokens=2)
    a.merge(b)
    assert a.total_tokens == 13
    assert a.cache_read_tokens == 5
    assert a.cache_creation_tokens == 2


def test_total_only_usage_is_not_recorded_as_input() -> None:
    stats = SessionStats()
    stats.record_request("m", TokenUsage(0, 0, 0, 0, 77))
    assert stats.input_tokens == 0
    assert stats.total_tokens == 77


def test_token_event_trace_counts_completed_total() -> None:
    trace = TokenEventTrace()
    trace.note_completed(total_tokens_used=500, has_total_field=True)
    trace.finish_turn(
        loop_id="l",
        baseline=0,
        goal_run=500,
        display_total=500,
        turn_input=100,
        turn_output=20,
        approximate=False,
    )
    assert trace.completed_events == 1
    assert trace.completed_with_total_field == 1
    assert trace.anomalies == []


def test_context_snapshot_shows_cache_breakdown() -> None:
    snapshot = TokenUsageSnapshot(
        context_tokens=1200,
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=80,
        cache_creation_tokens=5,
    )
    text = format_token_usage(snapshot)
    assert "cache read 80" in text
    assert "cache write 5" in text
    assert "↑100 · ↓20" in text


def test_context_snapshot_without_cache_has_no_cache_text() -> None:
    text = format_token_usage(
        TokenUsageSnapshot(context_tokens=12, input_tokens=10, output_tokens=2)
    )
    assert "cache" not in text


def test_backend_completed_event_applies_authoritative_total() -> None:
    from soothe_cli.tui.textual_adapter import _apply_backend_loop_tokens_event

    applied: list[tuple[int, str]] = []
    adapter = SimpleNamespace(
        _token_event_trace=TokenEventTrace(),
        _apply_authoritative_loop_tokens=lambda total, source: applied.append((total, source)),
    )
    _apply_backend_loop_tokens_event(
        adapter,
        {"type": "soothe.cognition.strange_loop.completed", "total_tokens_used": 640},
        source="completed",
    )
    assert applied == [(640, "completed")]
    assert adapter._token_event_trace.completed_with_total_field == 1
