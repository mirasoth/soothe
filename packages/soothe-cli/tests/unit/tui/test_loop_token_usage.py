"""Tests for per-loop accumulated token usage in the TUI."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from soothe_cli.tui.app._ui import _UIMixin


class _TokenApp(_UIMixin):
    """Minimal host object for _UIMixin token helpers."""

    def __init__(self) -> None:
        self._lc_loop_id = "loop-a"
        self._loop_token_scope_id = None
        self._loop_baseline_tokens = 0
        self._loop_goal_run_tokens = 0
        self._loop_turn_committed = False
        self._tokens_approximate = False
        self._inflight_turn_stats = None
        self._loading_widget = None
        self._status_bar = SimpleNamespace(
            set_tokens=lambda count, approximate=False: None,
        )
        self._ui_adapter = None


def test_record_loop_turn_tokens_folds_into_baseline() -> None:
    app = _TokenApp()

    app._record_loop_turn_tokens(100, 50)
    assert app._loop_token_total() == 150
    assert app._loop_goal_run_tokens == 0

    app._begin_loop_turn_tokens()
    app._record_loop_turn_tokens(200, 25)
    assert app._loop_token_total() == 375


def test_reset_loop_token_usage_clears_on_loop_change() -> None:
    app = _TokenApp()
    app._record_loop_turn_tokens(1000, 500)
    app._reset_loop_token_usage("loop-b")

    assert app._loop_token_scope_id == "loop-b"
    assert app._loop_token_total() == 0


def test_seed_loop_token_baseline_then_accumulate() -> None:
    app = _TokenApp()
    app._lc_loop_id = "loop-resumed"
    app._seed_loop_token_baseline("loop-resumed", 900)

    assert app._loop_token_total() == 900

    app._record_loop_turn_tokens(100, 50)
    assert app._loop_token_total() == 1050


def test_begin_loop_turn_tokens_resets_goal_counters_only() -> None:
    app = _TokenApp()
    app._seed_loop_token_baseline("loop-a", 900)
    app._apply_authoritative_loop_tokens(200)
    app._begin_loop_turn_tokens()

    assert app._loop_token_total() == 900
    assert app._loop_goal_run_tokens == 0


def test_ensure_loop_token_scope_resets_when_loop_id_changes() -> None:
    app = _TokenApp()
    app._record_loop_turn_tokens(300, 100)
    app._lc_loop_id = "loop-b"
    app._record_loop_turn_tokens(10, 5)

    assert app._loop_token_scope_id == "loop-b"
    assert app._loop_token_total() == 15


def test_display_token_total_uses_inflight_without_double_counting_goal() -> None:
    app = _TokenApp()
    app._seed_loop_token_baseline("loop-a", 1000)
    app._apply_authoritative_loop_tokens(200)
    app._inflight_turn_stats = SimpleNamespace(input_tokens=250, output_tokens=50)

    assert app._display_token_total() == 1300


def test_refresh_token_displays_updates_thinking_row_and_status_bar() -> None:
    app = _TokenApp()
    app._seed_loop_token_baseline("loop-a", 500)
    loading = SimpleNamespace(set_token_usage=MagicMock())
    status = SimpleNamespace(set_tokens=MagicMock())
    app._loading_widget = loading
    app._status_bar = status
    app._apply_authoritative_loop_tokens(250)
    loading.set_token_usage.reset_mock()
    status.set_tokens.reset_mock()

    app._refresh_token_displays()

    loading.set_token_usage.assert_called_once_with(750, approximate=False)
    status.set_tokens.assert_called_once_with(750, approximate=False)


def test_loop_token_breakdown_splits_baseline_and_goal() -> None:
    app = _TokenApp()
    app._seed_loop_token_baseline("loop-a", 1000)
    app._apply_authoritative_loop_tokens(250)

    baseline, goal_run, display_total = app._loop_token_breakdown()

    assert baseline == 1000
    assert goal_run == 250
    assert display_total == 1250


def test_apply_authoritative_loop_tokens_does_not_mislabel_slots() -> None:
    app = _TokenApp()
    app._seed_loop_token_baseline("loop-a", 1000)
    app._apply_authoritative_loop_tokens(500)

    assert app._loop_token_total() == 1500
    assert app._display_token_total() == 1500
    assert app._loop_goal_run_tokens == 500


def test_authoritative_total_is_monotonic_within_goal() -> None:
    app = _TokenApp()
    app._apply_authoritative_loop_tokens(500)
    app._apply_authoritative_loop_tokens(300)  # stale/smaller event

    assert app._loop_goal_run_tokens == 500
    assert app._display_token_total() == 500


def test_turn_end_commit_does_not_double_count_stream() -> None:
    app = _TokenApp()
    app._inflight_turn_stats = SimpleNamespace(input_tokens=250, output_tokens=50)
    assert app._display_token_total() == 300

    app._record_loop_turn_tokens(250, 50)
    assert app._loop_baseline_tokens == 300
    assert app._loop_token_total() == 300  # inflight still set but not re-added


def test_completed_source_reconciles_final_total() -> None:
    app = _TokenApp()
    app._inflight_turn_stats = SimpleNamespace(input_tokens=100, output_tokens=20)
    app._apply_authoritative_loop_tokens(400, source="completed")

    assert app._display_token_total() == 400
    app._record_loop_turn_tokens(100, 20)
    assert app._loop_baseline_tokens == 400
