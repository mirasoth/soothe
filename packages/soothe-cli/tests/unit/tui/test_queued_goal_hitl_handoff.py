"""Queued goal handoff across HITL approval/clarification pauses.

A paused loop ends its turn stream before the goal is finished. Cleanup must
keep the local queue intact through that pause and send each goal exactly once
after the human-answer turn completes.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from soothe_cli.runtime.state.session_stats import SessionStats
from soothe_cli.tui.app._execution import _ExecutionMixin
from soothe_cli.tui.app._types import QueuedMessage


def _app(
    *,
    live: bool = False,
    qa_pending: bool = False,
    clarification_pending: bool = False,
    follow_on: bool = False,
) -> Any:  # noqa: ANN401
    """Build a minimal ``_ExecutionMixin`` stub with a real queue and gate."""
    app = object.__new__(_ExecutionMixin)
    app._ui_adapter = SimpleNamespace(
        _clarification_pending=clarification_pending,
        _plan_approve_follow_on_pending=follow_on,
    )
    app._daemon_session = SimpleNamespace(
        ensure_connected=AsyncMock(),
        fetch_loop_history=AsyncMock(
            return_value=SimpleNamespace(live_goal_index=0 if live else None)
        ),
        fetch_execution_state=AsyncMock(
            return_value=SimpleNamespace(
                status="running" if live else "idle",
                active_runner=True if live else False,
            )
        ),
    )
    app._assistant_id = "soothe"
    app._session_state = SimpleNamespace(loop_id="loop-1")
    app._image_tracker = None
    app._sandbox_type = None
    app._cwd = "/tmp"
    app._model_override = None
    app._model_params_override = None
    app._router_profile_override = None
    app._composer_mode = "auto"
    app._exit = False
    app._inflight_turn_stats = None
    app._inflight_turn_start = None
    app._session_stats = SessionStats()
    app._agent_running = False
    app._agent_worker = None
    app._turn_generation = 0
    app._shell_running = False
    app._processing_pending = False
    app._pending_messages: deque[QueuedMessage] = deque()
    app._has_active_qa_widget = lambda: qa_pending
    app._mount_message = AsyncMock()
    app._send_to_agent = AsyncMock()
    app._process_next_from_queue = AsyncMock()
    app._process_message = AsyncMock()
    app._maybe_drain_deferred = AsyncMock()
    app._set_spinner = AsyncMock()
    app._refresh_token_displays = lambda **_kw: None
    app._tokens_approximate = False
    app._chat_input = None
    app._primary_text_input = lambda: None
    app.focus_primary_input = lambda: None
    app._runtime_backend_ready = lambda: True
    app._refresh_queued_goal_tips = lambda: None
    app._try_recover_goal_completion_from_ledger = AsyncMock()
    app._attach_to_live_daemon_turn = AsyncMock()
    app._cleanup_agent_task = AsyncMock()
    return app


@pytest.mark.asyncio
async def test_cleanup_during_approval_does_not_consume_queue() -> None:
    """An unsubmitted approval card keeps the queue and the bar intact."""
    app = _app(qa_pending=True)
    goal = QueuedMessage(text="queued goal", mode="normal")
    app._pending_messages.append(goal)

    await _ExecutionMixin._cleanup_agent_task(app)

    assert list(app._pending_messages) == [goal]
    app._process_next_from_queue.assert_not_awaited()
    app._attach_to_live_daemon_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_with_clarification_flag_only_does_not_consume_queue() -> None:
    """A deferred clarification without a mounted card also blocks draining."""
    app = _app(clarification_pending=True)
    goal = QueuedMessage(text="queued goal", mode="normal")
    app._pending_messages.append(goal)

    await _ExecutionMixin._cleanup_agent_task(app)

    assert list(app._pending_messages) == [goal]
    app._process_next_from_queue.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_reopens_after_approval_is_answered() -> None:
    """The pause keeps the queue; the completed answer turn drains it once."""
    app = _app(qa_pending=True)
    goal = QueuedMessage(text="queued goal", mode="normal")
    app._pending_messages.append(goal)

    await _ExecutionMixin._cleanup_agent_task(app)
    assert list(app._pending_messages) == [goal]
    app._process_next_from_queue.assert_not_awaited()

    app._has_active_qa_widget = lambda: False
    await _ExecutionMixin._cleanup_agent_task(app)

    app._process_next_from_queue.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_next_from_queue_blocked_by_approval_card() -> None:
    """Defensive gate protects the second cleanup entry (background consumer)."""
    app = _app(qa_pending=True)
    goal = QueuedMessage(text="queued goal", mode="normal")
    app._pending_messages.append(goal)

    await _ExecutionMixin._process_next_from_queue(app)

    assert list(app._pending_messages) == [goal]
    app._process_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_next_from_queue_sends_local_goal_as_real_turn() -> None:
    """Once the gate opens, the queued goal carries the from_queue marker."""
    app = _app()
    app._pending_messages.append(QueuedMessage(text="queued goal", mode="normal"))

    await _ExecutionMixin._process_next_from_queue(app)

    app._process_message.assert_awaited_once_with("queued goal", "normal", from_queue=True)
    assert not app._pending_messages


@pytest.mark.asyncio
async def test_daemon_follow_on_attaches_without_consuming_local_goal() -> None:
    """A daemon-owned successor turn is observed, not used to drop local goals."""
    app = _app(live=True, follow_on=True)
    goal = QueuedMessage(text="queued goal", mode="normal")
    app._pending_messages.append(goal)

    await _ExecutionMixin._cleanup_agent_task(app)

    app._attach_to_live_daemon_turn.assert_awaited_once()
    app._process_next_from_queue.assert_not_awaited()
    assert list(app._pending_messages) == [goal]


@pytest.mark.asyncio
async def test_stale_turn_generation_skips_cleanup(monkeypatch: Any) -> None:  # noqa: ANN401
    """A superseded worker must not clear the running state of a newer turn."""
    app = _app()
    app._turn_generation = 2
    app._agent_running = True

    async def fake_execute(**_kwargs: Any) -> SessionStats:
        return SessionStats()

    monkeypatch.setattr("soothe_cli.tui.textual_adapter.execute_task_textual", fake_execute)

    await app._run_agent_task("old turn", turn_generation=1)

    app._cleanup_agent_task.assert_not_awaited()
    assert app._agent_running is True
