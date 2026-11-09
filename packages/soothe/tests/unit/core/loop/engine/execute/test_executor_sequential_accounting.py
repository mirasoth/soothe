"""Executor parallel waves: one StepExecutionRecord per StepAction, chunked by max_parallel_steps."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from soothe.context.engine import ContextEngine
from soothe.context.store_sqlite import SqliteContextPersistence
from soothe.sloop.engine.execute.executor import Executor, StepWaveQueued, StepWaveStart
from soothe.sloop.state.schemas import (
    AgentDecision,
    LoopState,
    StepAction,
    StepExecutionRecord,
)


async def _empty_agent_stream() -> AsyncIterator[Any]:
    if False:  # pragma: no cover — makes this an async generator
        yield None


async def _content_agent_stream() -> AsyncIterator[Any]:
    """Stream that produces a valid AIMessage — simulates a successful model call."""
    yield (
        (),
        "messages",
        (AIMessage(content="Step completed successfully with results."), {}),
    )


def _make_mock_agent() -> MagicMock:
    mock_agent = MagicMock()
    # execution_astream is sync and returns an async iterator — not awaitable.
    # Produces a valid AIMessage so the step has meaningful output (success=True).
    mock_agent.execution_astream = MagicMock(side_effect=lambda *a, **k: _content_agent_stream())
    mock_agent.execution_aget_state = AsyncMock(return_value=MagicMock())
    mock_agent.aget_state = AsyncMock(return_value=MagicMock())
    return mock_agent


def _make_ce() -> ContextEngine:
    """Create a ContextEngine with sqlite :memory: backend for tests."""
    return ContextEngine(
        persistence=SqliteContextPersistence(loop_id="test", db_path=Path(":memory:"))
    )


@pytest.mark.asyncio
async def test_parallel_single_wave_yields_one_result_per_step() -> None:
    mock_agent = _make_mock_agent()

    executor = Executor(mock_agent, max_parallel_steps=4, context_engine=_make_ce())
    decision = AgentDecision(
        type="execute_steps",
        steps=[
            StepAction(id="a", description="first", expected_output="o1"),
            StepAction(id="b", description="second", expected_output="o2"),
        ],
        execution_mode="parallel",
        reasoning="r",
    )
    state = LoopState(goal="g", thread_id="t-main")
    out = [x async for x in executor.execute(decision, state) if isinstance(x, StepExecutionRecord)]

    assert len(out) == 2
    assert {r.step_id for r in out} == {"a", "b"}
    assert all(r.success for r in out)
    assert mock_agent.execution_astream.call_count == 2


@pytest.mark.asyncio
async def test_parallel_respects_max_parallel_steps_multiple_waves() -> None:
    mock_agent = _make_mock_agent()

    executor = Executor(mock_agent, max_parallel_steps=1, context_engine=_make_ce())
    decision = AgentDecision(
        type="execute_steps",
        steps=[
            StepAction(id="a", description="first", expected_output="o1"),
            StepAction(id="b", description="second", expected_output="o2"),
        ],
        execution_mode="parallel",
        reasoning="r",
    )
    state = LoopState(goal="g", thread_id="t-main")
    out = [x async for x in executor.execute(decision, state) if isinstance(x, StepExecutionRecord)]

    assert len(out) == 2
    assert mock_agent.execution_astream.call_count == 2


@pytest.mark.asyncio
async def test_plan_with_dependencies_drains_ready_chain() -> None:
    mock_agent = _make_mock_agent()

    executor = Executor(mock_agent, max_parallel_steps=4, context_engine=_make_ce())
    decision = AgentDecision(
        type="execute_steps",
        steps=[
            StepAction(id="01", description="discover files", expected_output="files"),
            StepAction(
                id="02",
                description="read files",
                expected_output="content",
                dependencies=["01"],
            ),
            StepAction(
                id="03",
                description="validate refs",
                expected_output="report",
                dependencies=["02"],
            ),
        ],
        execution_mode="parallel",
        reasoning="r",
    )
    state = LoopState(goal="g", thread_id="t-main")

    out = [x async for x in executor.execute(decision, state) if isinstance(x, StepExecutionRecord)]

    assert [r.step_id for r in out] == ["01", "02", "03"]
    assert mock_agent.execution_astream.call_count == 3


def test_ledger_execute_ai_content_single_step_fills_from_chunks_ig373() -> None:
    """Trailing empty AIMessage after chunks must not produce empty ledger body."""
    mock_agent = _make_mock_agent()
    executor = Executor(mock_agent, max_parallel_steps=4, context_engine=_make_ce())
    messages: list = [
        AIMessageChunk(content="Here are the first lines:\n"),
        AIMessageChunk(content="A\nB\n"),
        AIMessage(content=""),
    ]
    body = executor._extract_final_assistant_text_from_step_messages(messages)
    assert "Here are the first lines" in body
    assert "A\nB" in body


def test_ledger_ai_content_last_hop_with_chunk_only_hops() -> None:
    """Chunked narration from earlier hops must not leak into the ledger body.

    When per-hop text streams as AIMessageChunks and only the final hop
    materializes a non-chunk AIMessage, the ToolMessage boundary delimits
    the last hop (loop f326 regression)."""
    mock_agent = _make_mock_agent()
    executor = Executor(mock_agent, max_parallel_steps=4, context_engine=_make_ce())
    narration = "I'll analyze veritas arch — let me first explore the workspace."
    report = "# Veritas Architecture Analysis\n\nFull report body."
    messages: list = [
        AIMessageChunk(content=narration),
        ToolMessage(content="files", tool_call_id="t1"),
        AIMessageChunk(content="I found the subagent. Let me read files."),
        ToolMessage(content="contents", tool_call_id="t2"),
        AIMessageChunk(content=report),
        AIMessage(content=report),
    ]
    body = executor._extract_final_assistant_text_from_step_messages(messages)
    assert body == report


def test_last_hop_text_empty_when_step_capped_after_tools() -> None:
    """No AI text after the last ToolMessage -> empty extraction (no final turn)."""
    mock_agent = _make_mock_agent()
    executor = Executor(mock_agent, max_parallel_steps=4, context_engine=_make_ce())
    first = "First turn text."
    messages: list = [
        AIMessageChunk(content=first),
        AIMessage(content=first),
        AIMessage(
            content="second turn wants tools", tool_calls=[{"name": "grep", "args": {}, "id": "c1"}]
        ),
        ToolMessage(content="results", tool_call_id="c1"),
    ]
    turn = executor._messages_for_last_assistant_turn(messages)
    assert turn == []
    body = executor._extract_final_assistant_text_from_step_messages(messages)
    assert body == ""


def test_prior_assistant_text_excludes_final_hop() -> None:
    """Prior-hop text carries every hop before the last ToolMessage boundary."""
    mock_agent = _make_mock_agent()
    executor = Executor(mock_agent, max_parallel_steps=4, context_engine=_make_ce())
    narration = "I'll analyze veritas arch — let me first explore the workspace."
    report = "# Veritas Architecture Analysis\n\nFull report body."
    recap = "Analysis delivered above. In short: done."
    messages: list = [
        AIMessageChunk(content=narration),
        ToolMessage(content="files", tool_call_id="t1"),
        AIMessageChunk(content=report),
        AIMessage(content=report),
        ToolMessage(content="todo results", tool_call_id="t2"),
        AIMessageChunk(content=recap),
        AIMessage(content=recap),
    ]
    prior = executor._prior_assistant_text_from_step_messages(messages)
    assert narration in prior
    assert report in prior
    assert recap not in prior


def test_prior_assistant_text_empty_for_single_hop() -> None:
    """No ToolMessage boundaries -> single-hop step has no prior text."""
    mock_agent = _make_mock_agent()
    executor = Executor(mock_agent, max_parallel_steps=4, context_engine=_make_ce())
    prior = executor._prior_assistant_text_from_step_messages(
        [AIMessageChunk(content="direct answer"), AIMessage(content="direct answer")]
    )
    assert prior == ""


def test_resolve_ledger_ai_content_empty_last_hop_uses_accumulated_output() -> None:
    """Empty last hop -> ledger AI content falls back to accumulated output.

    Pins the executor half of the empty last-hop finalize path: the ledger
    message carries the accumulated stream text, which finalize must gate
    through report validation before passthrough."""
    mock_agent = _make_mock_agent()
    executor = Executor(mock_agent, max_parallel_steps=4, context_engine=_make_ce())
    messages: list = [
        AIMessageChunk(content="I'll analyze the goal — let me look around."),
        ToolMessage(content="results", tool_call_id="c1"),
    ]
    content = executor._resolve_execute_step_ledger_ai_content(
        step_messages=messages,
        delegate_final=None,
        output="# Report\n\nAccumulated stream body.",
    )
    assert content == "# Report\n\nAccumulated stream body."


def test_resolve_ledger_ai_content_empty_messages_and_output_is_empty() -> None:
    """No AI text and no accumulated output -> empty ledger AI content."""
    mock_agent = _make_mock_agent()
    executor = Executor(mock_agent, max_parallel_steps=4, context_engine=_make_ce())
    content = executor._resolve_execute_step_ledger_ai_content(
        step_messages=[ToolMessage(content="results", tool_call_id="c1")],
        delegate_final=None,
        output="   ",
    )
    assert content == ""


@pytest.mark.asyncio
async def test_parallel_waves_emit_step_wave_queued_for_overflow() -> None:
    mock_agent = _make_mock_agent()

    executor = Executor(mock_agent, max_parallel_steps=1, context_engine=_make_ce())
    decision = AgentDecision(
        type="execute_steps",
        steps=[
            StepAction(id="a", description="p1", expected_output="o"),
            StepAction(id="b", description="p2", expected_output="o"),
        ],
        execution_mode="parallel",
        reasoning="r",
    )
    state = LoopState(goal="g", thread_id="t-main")
    queued: list[StepWaveQueued] = []
    async for item in executor.execute(decision, state):
        if isinstance(item, StepWaveQueued):
            queued.append(item)

    assert len(queued) == 1
    assert [s.id for s in queued[0].steps] == ["b"]


@pytest.mark.asyncio
async def test_parallel_waves_emit_step_wave_start_per_batch() -> None:
    mock_agent = _make_mock_agent()

    executor = Executor(mock_agent, max_parallel_steps=1, context_engine=_make_ce())
    decision = AgentDecision(
        type="execute_steps",
        steps=[
            StepAction(id="a", description="p1", expected_output="o"),
            StepAction(id="b", description="p2", expected_output="o"),
        ],
        execution_mode="parallel",
        reasoning="r",
    )
    state = LoopState(goal="g", thread_id="t-main")
    wave_starts: list[StepWaveStart] = []
    async for item in executor.execute(decision, state):
        if isinstance(item, StepWaveStart):
            wave_starts.append(item)

    assert [tuple(w.steps) for w in wave_starts] == [
        (decision.steps[0],),
        (decision.steps[1],),
    ]


@pytest.mark.asyncio
async def test_parallel_waves_respect_max_parallel_steps() -> None:
    mock_agent = _make_mock_agent()

    executor = Executor(mock_agent, max_parallel_steps=1, context_engine=_make_ce())
    decision = AgentDecision(
        type="execute_steps",
        steps=[
            StepAction(id="a", description="p1", expected_output="o"),
            StepAction(id="b", description="p2", expected_output="o"),
        ],
        execution_mode="parallel",
        reasoning="r",
    )
    state = LoopState(goal="g", thread_id="t-main")
    async for _ in executor.execute(decision, state):
        pass

    assert mock_agent.execution_astream.call_count == 2
