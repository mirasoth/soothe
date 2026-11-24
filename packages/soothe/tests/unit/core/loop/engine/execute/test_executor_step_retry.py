"""In-execution step retry (same-thread recovery) in the parallel wave runner.

A failed step (failed record without pause/fatal/timeout classification) is
retried automatically on the step's prior fork thread. Prior-attempt stats are
folded into the finalized record and a :class:`StepRetryNotice` is emitted per
retry so UIs can render live recovery progress.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from soothe.config import SootheConfig
from soothe.context.engine import ContextEngine
from soothe.context.store_sqlite import SqliteContextPersistence
from soothe.sloop.engine.execute.executor import Executor, _ExecuteStepResult
from soothe.sloop.engine.execute.step_wave_types import StepRetryNotice
from soothe.sloop.engine.execute.thread_selection import _select_thread_for_step
from soothe.sloop.state.schemas import LoopState, StepAction, StepExecutionRecord


def _make_ce() -> ContextEngine:
    """Create a ContextEngine with sqlite :memory: backend for tests."""
    return ContextEngine(
        persistence=SqliteContextPersistence(loop_id="test", db_path=Path(":memory:"))
    )


def _make_retry_config(max_attempts: int) -> SootheConfig:
    """SootheConfig with fast backoff so retry tests stay quick."""
    cfg = SootheConfig()
    cfg.agent.loop.step_retry_max_attempts = max_attempts
    cfg.agent.loop.step_retry_backoff_base_seconds = 0.01
    cfg.agent.loop.step_retry_backoff_max_seconds = 0.02
    return cfg


def _failed_record(step_id: str, *, error_type: str | None = "execution") -> StepExecutionRecord:
    return StepExecutionRecord(
        step_id=step_id,
        success=False,
        outcome={"type": "generic"},
        error="boom",
        error_type=error_type,  # type: ignore[arg-type]
        duration_ms=50,
        thread_id="t-retry",
        tool_call_count=3,
    )


class _CollectCalls:
    """Scriptable stand-in for ``_execute_step_collecting_events``."""

    def __init__(self, script: list[_ExecuteStepResult]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        step: StepAction,
        thread_id: str,
        workspace: str | None = None,
        *,
        live_event_queue: asyncio.Queue[Any] | None = None,
        **kwargs: Any,
    ) -> _ExecuteStepResult:
        del thread_id, workspace, live_event_queue
        self.calls.append({"step_id": step.id, **kwargs})
        return self.script.pop(0)


@pytest.mark.asyncio
async def test_failed_step_retries_and_recovers_with_prior_stats() -> None:
    """First attempt fails, second succeeds: recovery stats land on the record."""
    calls = _CollectCalls(
        [
            _ExecuteStepResult(step_result=_failed_record("s0")),
            _ExecuteStepResult(
                step_result=StepExecutionRecord(
                    step_id="s0",
                    success=True,
                    outcome={"type": "generic"},
                    duration_ms=40,
                    thread_id="t-retry",
                    tool_call_count=5,
                )
            ),
        ]
    )
    executor = Executor(
        MagicMock(), max_parallel_steps=2, config=_make_retry_config(3), context_engine=_make_ce()
    )
    executor._execute_step_collecting_events = calls  # type: ignore[method-assign]

    state = LoopState(goal="g", thread_id="t-retry", iteration=0, max_iterations=4)
    step = StepAction(id="s0", description="flaky step")

    results: list[Any] = []
    async for item in executor._execute_parallel([step], state):
        results.append(item)

    assert len(calls.calls) == 2
    notices = [r for r in results if isinstance(r, StepRetryNotice)]
    assert len(notices) == 1
    assert notices[0].step_id == "s0"
    assert notices[0].attempt == 1
    assert notices[0].max_attempts == 4
    records = [r for r in results if isinstance(r, StepExecutionRecord)]
    assert len(records) == 1
    rec = records[0]
    assert rec.success
    assert rec.retry_attempt == 2
    assert rec.recovered_by_retry
    assert rec.prior_attempts_tool_calls == 3
    assert rec.prior_attempts_duration_ms == 50


@pytest.mark.asyncio
async def test_retry_reenters_prior_fork_thread() -> None:
    """Retry attempt passes reuse_prior_thread=True (same-thread recovery)."""
    calls = _CollectCalls(
        [
            _ExecuteStepResult(step_result=_failed_record("s0")),
            _ExecuteStepResult(step_result=_failed_record("s0")),
            _ExecuteStepResult(step_result=_failed_record("s0")),
            _ExecuteStepResult(step_result=_failed_record("s0")),
            _ExecuteStepResult(step_result=_failed_record("s0")),
        ]
    )
    executor = Executor(
        MagicMock(), max_parallel_steps=1, config=_make_retry_config(4), context_engine=_make_ce()
    )
    executor._execute_step_collecting_events = calls  # type: ignore[method-assign]

    state = LoopState(goal="g", thread_id="t-retry", iteration=0, max_iterations=4)
    step = StepAction(id="s0", description="always fails")

    async for _ in executor._execute_parallel([step], state):
        pass

    assert len(calls.calls) == 5
    assert calls.calls[0]["reuse_prior_thread"] is False
    for call in calls.calls[1:]:
        assert call["reuse_prior_thread"] is True


@pytest.mark.asyncio
async def test_retry_exhaustion_yields_single_failed_record_with_stats() -> None:
    """All attempts fail: one failed record with summed prior-attempt stats."""
    calls = _CollectCalls([_ExecuteStepResult(step_result=_failed_record("s0"))] * 3)
    executor = Executor(
        MagicMock(), max_parallel_steps=1, config=_make_retry_config(2), context_engine=_make_ce()
    )
    executor._execute_step_collecting_events = calls  # type: ignore[method-assign]

    state = LoopState(goal="g", thread_id="t-retry", iteration=0, max_iterations=4)
    step = StepAction(id="s0", description="always fails")

    results: list[Any] = []
    async for item in executor._execute_parallel([step], state):
        results.append(item)

    assert len(calls.calls) == 3
    records = [r for r in results if isinstance(r, StepExecutionRecord)]
    assert len(records) == 1
    rec = records[0]
    assert not rec.success
    assert rec.retry_attempt == 3
    assert not rec.recovered_by_retry
    assert rec.prior_attempts_tool_calls == 6  # 3 tools per failed attempt
    assert rec.prior_attempts_duration_ms == 100
    notices = [r for r in results if isinstance(r, StepRetryNotice)]
    assert [n.attempt for n in notices] == [1, 2]


@pytest.mark.asyncio
async def test_paused_step_is_not_retried() -> None:
    """A clarification-paused step finalizes without retry attempts."""
    calls = _CollectCalls(
        [_ExecuteStepResult(step_result=_failed_record("s0"), paused_by_clarification=True)]
    )
    executor = Executor(
        MagicMock(), max_parallel_steps=1, config=_make_retry_config(5), context_engine=_make_ce()
    )
    executor._execute_step_collecting_events = calls  # type: ignore[method-assign]

    state = LoopState(goal="g", thread_id="t-pause", iteration=0, max_iterations=4)
    step = StepAction(id="s0", description="needs user input")

    results: list[Any] = []
    async for item in executor._execute_parallel([step], state):
        results.append(item)

    assert len(calls.calls) == 1
    # Paused payloads are not yielded as failed records (drain loop drops them).
    assert not [r for r in results if isinstance(r, StepExecutionRecord)]
    assert not [r for r in results if isinstance(r, StepRetryNotice)]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", ["fatal", "timeout"])
async def test_breaker_and_timeout_failures_are_not_retried(error_type: str) -> None:
    """Fatal and dispatch-timeout failures keep their single-attempt semantics."""
    calls = _CollectCalls([_ExecuteStepResult(step_result=_failed_record("s0", error_type=None))])
    executor = Executor(
        MagicMock(), max_parallel_steps=1, config=_make_retry_config(5), context_engine=_make_ce()
    )
    executor._execute_step_collecting_events = calls  # type: ignore[method-assign]

    state = LoopState(goal="g", thread_id="t-final", iteration=0, max_iterations=4)
    step = StepAction(id="s0", description="already retried upstream")
    # Patch the error type on the record (Literal field — build then mutate).
    calls.script[0].step_result.error_type = error_type  # type: ignore[union-attr]

    results: list[Any] = []
    async for item in executor._execute_parallel([step], state):
        results.append(item)

    assert len(calls.calls) == 1
    records = [r for r in results if isinstance(r, StepExecutionRecord)]
    assert len(records) == 1
    assert records[0].error_type == error_type


@pytest.mark.asyncio
async def test_step_retry_disabled_means_single_attempt() -> None:
    """With step_retry_max_attempts=0, a failed record is not retried."""
    calls = _CollectCalls([_ExecuteStepResult(step_result=_failed_record("s0"))])
    executor = Executor(
        MagicMock(), max_parallel_steps=1, config=_make_retry_config(0), context_engine=_make_ce()
    )
    executor._execute_step_collecting_events = calls  # type: ignore[method-assign]

    state = LoopState(goal="g", thread_id="t-off", iteration=0, max_iterations=4)
    step = StepAction(id="s0", description="no retry configured")

    results: list[Any] = []
    async for item in executor._execute_parallel([step], state):
        results.append(item)

    assert len(calls.calls) == 1
    records = [r for r in results if isinstance(r, StepExecutionRecord)]
    assert len(records) == 1
    assert not records[0].success
    assert records[0].retry_attempt == 1


@pytest.mark.asyncio
async def test_exception_retries_then_finalizes_via_exception() -> None:
    """Escaping exceptions are retried; exhaustion re-raises into the drain loop."""
    calls = _CollectCalls([])

    async def exploding_collect(
        step: StepAction,
        thread_id: str,
        workspace: str | None = None,
        *,
        live_event_queue: asyncio.Queue[Any] | None = None,
        **kwargs: Any,
    ) -> _ExecuteStepResult:
        del step, thread_id, workspace, live_event_queue, kwargs
        calls.calls.append({})
        raise RuntimeError("model pool exhausted")

    executor = Executor(
        MagicMock(), max_parallel_steps=1, config=_make_retry_config(2), context_engine=_make_ce()
    )
    executor._execute_step_collecting_events = exploding_collect  # type: ignore[method-assign]

    state = LoopState(goal="g", thread_id="t-exc", iteration=0, max_iterations=4)
    step = StepAction(id="s0", description="explodes")

    results: list[Any] = []
    async for item in executor._execute_parallel([step], state):
        results.append(item)

    assert len(calls.calls) == 3
    notices = [r for r in results if isinstance(r, StepRetryNotice)]
    assert [n.attempt for n in notices] == [1, 2]
    assert all("model pool exhausted" in n.error_preview for n in notices)
    # The final exception surfaces through the drain loop's failed-record path.
    records = [r for r in results if isinstance(r, StepExecutionRecord)]
    assert len(records) == 1
    assert not records[0].success


def test_select_thread_reuses_prior_thread_for_retry() -> None:
    """reuse_prior_thread re-enters the step's prior fork thread."""
    state = LoopState(goal="g", thread_id="main", iteration=0, max_iterations=4)
    state.step_thread_ids["s0"] = "fork-abc"
    step = StepAction(id="s0", description="step")

    reused = _select_thread_for_step(step, "main", loop_state=state, reuse_prior_thread=True)
    assert reused == "fork-abc"

    # Without the flag, a fresh thread is assigned.
    fresh = _select_thread_for_step(step, "main", loop_state=state)
    assert fresh != "fork-abc"

    # No prior mapping → fresh thread even with the flag.
    state.step_thread_ids.pop("s1", None)
    orphan = StepAction(id="s1", description="new step")
    assert _select_thread_for_step(orphan, "main", loop_state=state, reuse_prior_thread=True) != (
        "fork-abc"
    )
