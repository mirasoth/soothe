"""Identical tool-call circuit breaker for the Act stream."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import ToolMessage
from soothe_nano.middleware.tool_call_args_registry import (
    _registry,
    init_tool_call_args_registry,
)

from soothe.config.constants import DEFAULT_IDENTICAL_TOOL_CALL_THRESHOLD
from soothe.context.engine import ContextEngine
from soothe.context.models import GoalNode
from soothe.context.store_sqlite import SqliteContextPersistence
from soothe.sloop.engine.execute.executor import Executor, _ActStreamBudget
from soothe.sloop.state.schemas import (
    AgentDecision,
    LoopState,
    StepAction,
    StepExecutionRecord,
)


def _make_ce() -> ContextEngine:
    return ContextEngine(
        persistence=SqliteContextPersistence(loop_id="test", db_path=Path(":memory:"))
    )


def _make_step() -> StepAction:
    return StepAction(
        id="s0",
        description="Investigate root cause",
        expected_output="findings",
        dependencies=[],
    )


def _tool_msg(name: str, call_id: str, content: str = "data") -> tuple:
    return (
        (),
        "messages",
        (ToolMessage(content=content, tool_call_id=call_id, name=name), {}),
    )


def _setup_args(args_by_id: dict[str, dict[str, Any]]) -> None:
    """Populate the tool-call args registry for the current context."""
    init_tool_call_args_registry()
    store = _registry.get()
    assert store is not None
    store.update(args_by_id)


def _identical_args(call_ids: list[str], **kwargs: Any) -> dict[str, dict[str, Any]]:
    """Build identical args entries for the given tool-call ids."""
    return {cid: dict(kwargs) for cid in call_ids}


@pytest.mark.asyncio
async def test_three_identical_read_file_calls_trip_breaker() -> None:
    """The third consecutive identical tool call stops the Act stream."""
    _setup_args(_identical_args(["1", "2", "3", "4"], file_path="/a.ts"))
    budget = _ActStreamBudget(max_tool_calls_per_step=99)
    chunks: list = [
        _tool_msg("read_file", "1", "file content A"),
        _tool_msg("read_file", "2", "file content A"),
        _tool_msg("read_file", "3", "file content A"),
        _tool_msg("read_file", "4", "should not appear"),
    ]

    async def fake_stream():
        for c in chunks:
            yield c

    ex = Executor(MagicMock(), max_parallel_steps=1, context_engine=_make_ce())
    rows = [
        r
        async for r in ex._stream_and_collect(fake_stream(), budget=budget, step_id="s0")
        if r.output is not None
    ]

    assert len(rows) == 1
    final = rows[0]
    assert budget.hit_identical_repeat is True
    assert final.main_tool_count == DEFAULT_IDENTICAL_TOOL_CALL_THRESHOLD
    assert "should not appear" not in (final.output or "")


@pytest.mark.asyncio
async def test_different_tool_calls_do_not_trip() -> None:
    """Non-repeating tool calls complete without tripping the breaker."""
    _setup_args(
        {
            "1": {"file_path": "/a"},
            "2": {"pattern": "x"},
            "3": {"file_path": "/b"},
            "4": {"path": "/"},
        }
    )
    budget = _ActStreamBudget(max_tool_calls_per_step=99)
    chunks: list = [
        _tool_msg("read_file", "1", "alpha"),
        _tool_msg("grep", "2", "beta"),
        _tool_msg("read_file", "3", "gamma"),
        _tool_msg("ls", "4", "delta"),
    ]

    async def fake_stream():
        for c in chunks:
            yield c

    ex = Executor(MagicMock(), max_parallel_steps=1, context_engine=_make_ce())
    rows = [
        r
        async for r in ex._stream_and_collect(fake_stream(), budget=budget, step_id="s0")
        if r.output is not None
    ]

    assert len(rows) == 1
    assert budget.hit_identical_repeat is False
    assert rows[0].main_tool_count == 4


@pytest.mark.asyncio
async def test_breaker_resets_after_different_call() -> None:
    """An intervening different tool call resets the identical-repeat counter."""
    _setup_args(
        {
            "1": {"file_path": "/a"},
            "2": {"file_path": "/a"},
            "3": {"pattern": "x"},
            "4": {"file_path": "/a"},
            "5": {"file_path": "/a"},
            "6": {"file_path": "/a"},
            "7": {"file_path": "/a"},
        }
    )
    budget = _ActStreamBudget(max_tool_calls_per_step=99)
    chunks: list = [
        _tool_msg("read_file", "1", "content A"),
        _tool_msg("read_file", "2", "content A"),
        _tool_msg("grep", "3", "intervening"),
        _tool_msg("read_file", "4", "content A"),
        _tool_msg("read_file", "5", "content A"),
        _tool_msg("read_file", "6", "trips here"),
        _tool_msg("read_file", "7", "should not appear"),
    ]

    async def fake_stream():
        for c in chunks:
            yield c

    ex = Executor(MagicMock(), max_parallel_steps=1, context_engine=_make_ce())
    rows = [
        r
        async for r in ex._stream_and_collect(fake_stream(), budget=budget, step_id="s0")
        if r.output is not None
    ]

    # After grep resets, 3 more identical read_file → trips on #6
    assert budget.hit_identical_repeat is True
    assert "should not appear" not in (rows[0].output or "")


@pytest.mark.asyncio
async def test_write_todos_is_exempt() -> None:
    """write_todos may repeat without tripping the breaker."""
    _setup_args(_identical_args(["1", "2", "3"], todos=[]))
    budget = _ActStreamBudget(max_tool_calls_per_step=99)
    chunks: list = [
        _tool_msg("write_todos", "1", "todos v1"),
        _tool_msg("write_todos", "2", "todos v1"),
        _tool_msg("write_todos", "3", "todos v1"),
        _tool_msg("read_file", "4", "file content"),
    ]

    async def fake_stream():
        for c in chunks:
            yield c

    ex = Executor(MagicMock(), max_parallel_steps=1, context_engine=_make_ce())
    rows = [
        r
        async for r in ex._stream_and_collect(fake_stream(), budget=budget, step_id="s0")
        if r.output is not None
    ]

    assert budget.hit_identical_repeat is False
    assert rows[0].main_tool_count == 4


@pytest.mark.asyncio
async def test_breaker_propagates_to_step_execution_record() -> None:
    """hit_identical_repeat propagates to StepExecutionRecord and LoopState."""
    tool_msgs = [_tool_msg("read_file", str(i), "same") for i in range(1, 5)]
    args_by_id = _identical_args(["1", "2", "3", "4"], file_path="/a.ts")

    async def fake_execution_astream(*_a: object, **_k: object):
        store = _registry.get()
        if store is not None:
            store.update(args_by_id)
        for c in tool_msgs:
            yield c

    agent = MagicMock()
    agent.execution_astream = MagicMock(side_effect=fake_execution_astream)
    agent.execution_aget_state = AsyncMock(return_value=MagicMock())
    agent.aget_state = AsyncMock(return_value=MagicMock())

    ce = _make_ce()
    ex = Executor(agent, max_parallel_steps=1, config=None, context_engine=ce)
    state = LoopState(goal="g", thread_id="t", max_iterations=3)
    goal = GoalNode(description="test")
    ce._dag.add_goal(goal)
    state.bind_ce(ce, goal.id)
    step = _make_step()
    decision = AgentDecision(
        type="execute_steps",
        steps=[step],
        execution_mode="parallel",
        reasoning="",
    )
    out = [item async for item in ex.execute(decision, state)]

    results = [x for x in out if isinstance(x, StepExecutionRecord)]
    assert len(results) == 1
    sr = results[0]
    assert sr.hit_identical_repeat is True
    assert sr.outcome.get("identical_repeat_breaker") is True
    assert state.last_wave_hit_identical_repeat is True


@pytest.mark.asyncio
async def test_two_identical_calls_do_not_trip() -> None:
    """Two identical calls are allowed; the threshold is 3."""
    _setup_args(_identical_args(["1", "2"], file_path="/a.ts"))
    budget = _ActStreamBudget(max_tool_calls_per_step=99)
    chunks: list = [
        _tool_msg("read_file", "1", "same"),
        _tool_msg("read_file", "2", "same"),
    ]

    async def fake_stream():
        for c in chunks:
            yield c

    ex = Executor(MagicMock(), max_parallel_steps=1, context_engine=_make_ce())
    rows = [
        r
        async for r in ex._stream_and_collect(fake_stream(), budget=budget, step_id="s0")
        if r.output is not None
    ]

    assert budget.hit_identical_repeat is False
    assert rows[0].main_tool_count == 2
