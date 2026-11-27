"""Unit tests for execute-step completion cognition summaries."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage

from soothe.sloop.engine.execute.executor import Executor
from soothe.sloop.engine.execute.step_completion_report import (
    summarize_step_completion_report,
)
from soothe.sloop.engine.execute.step_wave_types import _ExecuteStepResult
from soothe.sloop.state.schemas import StepAction, StepExecutionRecord


@pytest.mark.asyncio
async def test_summarize_step_completion_report_returns_llm_text() -> None:
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content="I finished reviewing the API docs."))

    result = await summarize_step_completion_report(
        human_content="EXECUTION TASK:\nReview API docs",
        ai_content="Here is the API summary.",
        fast_model=model,
        max_words=50,
    )

    assert result == "I finished reviewing the API docs."
    messages = model.ainvoke.await_args.args[0]
    assert len(messages) == 2
    assert "at most 50 words" in messages[0].content
    assert "EXECUTION TASK:\nReview API docs" in messages[1].content
    assert "Here is the API summary." in messages[1].content
    assert "never restate completion status" in messages[0].content


@pytest.mark.asyncio
async def test_summarize_step_completion_report_does_not_truncate_over_limit() -> None:
    long_text = " ".join(f"word{i}" for i in range(60))
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content=long_text))

    result = await summarize_step_completion_report(
        human_content="EXECUTION TASK:\ndo work",
        ai_content="done",
        fast_model=model,
        max_words=50,
    )

    assert result == long_text


@pytest.mark.asyncio
async def test_summarize_step_completion_report_discards_runaway_reply() -> None:
    """A reply far past the budget (model continued the step output) is dropped."""
    runaway = " ".join(f"word{i}" for i in range(400))
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content=runaway))

    result = await summarize_step_completion_report(
        human_content="EXECUTION TASK:\ndo work",
        ai_content="done",
        fast_model=model,
        max_words=50,
    )

    assert result is None


@pytest.mark.asyncio
async def test_summarize_step_completion_report_uses_config_word_limit() -> None:
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content="I finished the step."))
    config = MagicMock()
    config.agent.loop.step_completion_report_max_words = 50

    await summarize_step_completion_report(
        human_content="EXECUTION TASK:\ndo work",
        ai_content="done",
        fast_model=model,
        soothe_config=config,
    )

    messages = model.ainvoke.await_args.args[0]
    assert "at most 50 words" in messages[0].content


@pytest.mark.asyncio
async def test_summarize_step_completion_report_empty_pair_returns_none() -> None:
    model = MagicMock()
    model.ainvoke = AsyncMock()

    result = await summarize_step_completion_report(
        human_content="",
        ai_content="",
        fast_model=model,
        max_words=50,
    )

    assert result is None
    model.ainvoke.assert_not_called()


@pytest.mark.asyncio
async def test_summarize_step_completion_report_llm_failure_returns_none() -> None:
    model = MagicMock()
    model.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))

    result = await summarize_step_completion_report(
        human_content="EXECUTION TASK:\ndo work",
        ai_content="done",
        fast_model=model,
        max_words=50,
    )

    assert result is None


# ── Executor-level kind gating ────────────────────────────────


def _make_executor() -> Executor:
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content="I summarized the step."))
    config = MagicMock()
    config.agent.loop.step_completion_report_max_words = 50
    return Executor(MagicMock(), fast_model=model, config=config)


def _step_result(step_id: str) -> _ExecuteStepResult:
    return _ExecuteStepResult(
        step_result=StepExecutionRecord(
            step_id=step_id,
            success=True,
            duration_ms=9000,
            thread_id="thread-1",
            tool_call_count=1,
        ),
        messages=[AIMessage(content="Coverage verdict recorded (complete).")],
    )


@pytest.mark.asyncio
async def test_executor_skips_summary_for_eval_steps() -> None:
    """kind="eval" steps skip the summarization call: the coverage_verdict
    already is the cognition line, and the call sits on the critical path
    between steps."""
    executor = _make_executor()
    step = StepAction(
        id="EVAL-01",
        description="Evaluate user-goal coverage",
        full_description="Assess whether the ORIGINAL USER GOAL is fully achieved.",
        kind="eval",
    )

    summary = await executor._summarize_step_completion_report(
        step, _step_result("EVAL-01"), MagicMock()
    )

    assert summary is None
    executor._fast_model.ainvoke.assert_not_called()


@pytest.mark.asyncio
async def test_executor_summarizes_action_steps_with_tools() -> None:
    """Negative control: an action step with the same shape goes to the LLM."""
    executor = _make_executor()
    executor._compose_execute_step_envelope = lambda step, **_: "EXECUTION TASK:\nDo the work"
    step = StepAction(
        id="ACT-01",
        description="Do the work",
        full_description="Do the work described.",
        kind="action",
    )

    summary = await executor._summarize_step_completion_report(
        step, _step_result("ACT-01"), MagicMock()
    )

    assert summary == "I summarized the step."
    executor._fast_model.ainvoke.assert_awaited_once()
