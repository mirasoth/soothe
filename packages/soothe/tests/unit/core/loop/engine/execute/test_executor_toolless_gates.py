"""Toolless-step gates: completion LLM calls are skipped for direct replies.

A successful zero-tool-call step is a direct reply (conversational turn):
the close report and cognition summary add LLM latency without information,
and finalize passes the streamed answer through without report validation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from soothe.sloop.engine.execute.executor import Executor
from soothe.sloop.engine.execute.step_wave_types import _StreamCollectChunk
from soothe.sloop.state.schemas import StepAction, StepExecutionRecord
from soothe.sloop.stations.completion.finalize import _toolless_single_leaf


def _make_executor() -> Executor:
    ce = MagicMock()
    ce.ledger = MagicMock()
    agent = MagicMock()
    agent.execution_astream = MagicMock(return_value=iter(()))
    agent.execution_aget_state = AsyncMock(return_value=MagicMock())
    agent.aget_state = AsyncMock(return_value=MagicMock())
    agent.can_read_graph_state = False
    return Executor(agent, context_engine=ce)


def _plain_step(step_id: str = "step-t") -> StepAction:
    return StepAction(id=step_id, description="say hello", kind="action")


@pytest.mark.asyncio
async def test_assess_step_close_skipped_for_toolless_success() -> None:
    """A successful zero-tool-call reply skips the close-report LLM call."""
    executor = _make_executor()

    async def fake_stream_and_collect(
        _stream: Any, **_kwargs: Any
    ) -> AsyncIterator[_StreamCollectChunk]:
        yield _StreamCollectChunk.finalized(
            output="Hi! I'm Soothe.",
            main_tool_count=0,
            messages=[AIMessage(content="Hi! I'm Soothe.")],
            delegate_final="",
            outcomes=[],
            has_error=False,
            subgraph_tool_count=0,
        )

    with (
        patch.object(executor, "_stream_and_collect", side_effect=fake_stream_and_collect),
        patch(
            "soothe.sloop.eval.step_close_report.assess_step_close",
            new=AsyncMock(),
        ) as mock_close,
    ):
        result = await executor._execute_step_collecting_events(_plain_step(), "thread-1")

    mock_close.assert_not_called()
    assert result.step_result is not None
    assert result.step_result.success is True
    assert "step_close_report" not in result.step_result.outcome


@pytest.mark.asyncio
async def test_assess_step_close_runs_for_tool_step() -> None:
    """Steps that used tools keep the close assessment (deferred-work evidence)."""
    executor = _make_executor()

    async def fake_stream_and_collect(
        _stream: Any, **_kwargs: Any
    ) -> AsyncIterator[_StreamCollectChunk]:
        yield _StreamCollectChunk.finalized(
            output="edited the file",
            main_tool_count=1,
            messages=[AIMessage(content="edited the file")],
            delegate_final="",
            outcomes=[{"has_error": False, "tool_name": "edit"}],
            has_error=False,
            subgraph_tool_count=0,
        )

    with (
        patch.object(executor, "_stream_and_collect", side_effect=fake_stream_and_collect),
        patch(
            "soothe.sloop.eval.step_close_report.assess_step_close",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    goal_portion_complete=True,
                    early_exit=False,
                    deferred_items=[],
                    recommendations=[],
                    model_dump=lambda: {"goal_portion_complete": True},
                )
            ),
        ) as mock_close,
    ):
        result = await executor._execute_step_collecting_events(_plain_step(), "thread-1")

    mock_close.assert_awaited_once()
    assert result.step_result is not None
    assert "step_close_report" in result.step_result.outcome


@pytest.mark.asyncio
async def test_assess_step_close_runs_for_failed_toolless_step() -> None:
    """A failed toolless step (empty output) keeps the fail-closed assessment."""
    executor = _make_executor()

    async def fake_stream_and_collect(
        _stream: Any, **_kwargs: Any
    ) -> AsyncIterator[_StreamCollectChunk]:
        yield _StreamCollectChunk.finalized(
            output="",
            main_tool_count=0,
            messages=[],
            delegate_final="",
            outcomes=[],
            has_error=False,
            subgraph_tool_count=0,
        )

    with (
        patch.object(executor, "_stream_and_collect", side_effect=fake_stream_and_collect),
        patch(
            "soothe.sloop.eval.step_close_report.assess_step_close",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    goal_portion_complete=False,
                    early_exit=True,
                    deferred_items=[],
                    recommendations=[],
                    model_dump=lambda: {"goal_portion_complete": False, "early_exit": True},
                )
            ),
        ) as mock_close,
    ):
        result = await executor._execute_step_collecting_events(_plain_step(), "thread-1")

    mock_close.assert_awaited_once()
    assert result.step_result is not None
    assert result.step_result.success is False


@pytest.mark.asyncio
async def test_summarize_step_completion_report_skipped_for_toolless() -> None:
    """Toolless successes skip the cognition summary — the answer was streamed."""
    executor = _make_executor()
    model = MagicMock()
    model.ainvoke = AsyncMock()
    executor._fast_model = model
    executor._config = MagicMock()

    result = _ExecuteStepResultForTest(success=True, tool_calls=0, subgraph=0)

    summary = await executor._summarize_step_completion_report(
        _plain_step(), result, SimpleNamespace(thread_id="t1", workspace=None)
    )

    assert summary is None
    model.ainvoke.assert_not_called()


@pytest.mark.asyncio
async def test_summarize_step_completion_report_runs_for_tool_step() -> None:
    """Tool-using steps still get the cognition summary."""
    executor = _make_executor()
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content="I edited the file."))
    executor._fast_model = model
    executor._config = MagicMock()
    executor._config.agent.loop.step_completion_report_max_words = 50

    result = _ExecuteStepResultForTest(success=True, tool_calls=1, subgraph=0)

    from soothe.sloop.state.schemas import LoopState

    state = LoopState(thread_id="t1", goal="say hello")
    summary = await executor._summarize_step_completion_report(_plain_step(), result, state)

    assert summary == "I edited the file."
    model.ainvoke.assert_awaited_once()


class _ExecuteStepResultForTest:
    """Minimal stand-in shaped like `_ExecuteStepResult` for the summary gate."""

    def __init__(self, *, success: bool, tool_calls: int, subgraph: int) -> None:
        self.step_result = StepExecutionRecord(
            step_id="step-t",
            success=success,
            duration_ms=10,
            thread_id="thread-1",
            tool_call_count=tool_calls,
            subgraph_tool_call_count=subgraph,
        )
        self.messages: list[Any] = []
        self.delegate_final = ""
        self.output = "done"


def test_toolless_single_leaf_true_for_single_completed_toolless_node() -> None:
    node = SimpleNamespace(
        status="completed",
        parent_step_id=None,
        execution=SimpleNamespace(tool_call_count=0, subagent_task_completions=0),
    )
    assert _toolless_single_leaf({"s1": node}) is True


def test_toolless_single_leaf_false_for_tool_step() -> None:
    node = SimpleNamespace(
        status="completed",
        parent_step_id=None,
        execution=SimpleNamespace(tool_call_count=2, subagent_task_completions=0),
    )
    assert _toolless_single_leaf({"s1": node}) is False


def test_toolless_single_leaf_false_for_multi_node_dag() -> None:
    node = SimpleNamespace(
        status="completed",
        parent_step_id=None,
        execution=SimpleNamespace(tool_call_count=0, subagent_task_completions=0),
    )
    child = SimpleNamespace(
        status="completed",
        parent_step_id="s1",
        execution=SimpleNamespace(tool_call_count=0, subagent_task_completions=0),
    )
    assert _toolless_single_leaf({"s1": node, "s2": child}) is False


def test_toolless_single_leaf_false_when_no_execution() -> None:
    node = SimpleNamespace(status="completed", parent_step_id=None, execution=None)
    assert _toolless_single_leaf({"s1": node}) is False
