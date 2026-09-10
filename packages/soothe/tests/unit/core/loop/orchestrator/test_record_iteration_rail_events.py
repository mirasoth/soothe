"""Tests for step_completed/step_failed RailEvent emission in record_progress."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from soothe.context.engine import ContextEngine
from soothe.context.models import GoalNode
from soothe.context.store_sqlite import SqliteContextPersistence
from soothe.rails.interpreter import RailEvent
from soothe.sloop.orchestrator.runtime_context import (
    LoopPhaseScratch,
    LoopRuntimeContext,
)
from soothe.sloop.state.schemas import AgentDecision, LoopState, PlanResult, StepAction


def _make_ctx(
    ce: ContextEngine,
    goal_id: str,
    *,
    step_results: list,
    rail_interpreter: object | None,
) -> LoopRuntimeContext:
    plan_result = PlanResult(
        status="continue",
        goal_progress="low",
        decision=AgentDecision(
            type="execute_steps",
            steps=[StepAction(id="S1", description="step", dependencies=None)],
            execution_mode="parallel",
        ),
    )
    decision = plan_result.decision
    assert decision is not None
    loop_state = LoopState(goal="goal", thread_id="t1")
    loop_state.bind_ce(ce, goal_id)
    ctx = LoopRuntimeContext(
        strange_loop=MagicMock(),
        state_manager=MagicMock(record_iteration=AsyncMock()),
        plan_manager=MagicMock(record_step_outcomes=MagicMock()),
        checkpoint=MagicMock(),
        goal_record=MagicMock(goal_id="g1"),
        continue_loop_mode=False,
        recovery_valid_resume=False,
        loop_state=loop_state,
        emit=AsyncMock(),
        scratch=LoopPhaseScratch(
            plan_result=plan_result,
            decision=decision,
            step_results=step_results,
            iteration_perf_start=None,
        ),
        ce=ce,
        ce_goal_id=goal_id,
        rail_interpreter=rail_interpreter,
    )
    return ctx


class _RecordingRailInterpreter:
    """Captures RailEvents handed to ``handle`` for assertions."""

    def __init__(self) -> None:
        self.events: list[RailEvent] = []

    async def handle(self, event: RailEvent) -> list:
        self.events.append(event)
        return []


@pytest.mark.asyncio
async def test_step_completed_and_failed_events_emitted() -> None:
    """A wave with one success and one failure emits both rail events."""
    from soothe.sloop.stations.execute.record_progress import node_record_iteration

    ce = ContextEngine(
        persistence=SqliteContextPersistence(loop_id="test", db_path=Path(":memory:"))
    )
    goal = GoalNode(description="goal")
    ce._dag.add_goal(goal)
    await ce.add_step(goal.id, _step_node("S1"))
    await ce.add_step(goal.id, _step_node("S2"))

    ok = SimpleNamespace(
        step_id="S1",
        success=True,
        outcome={"type": "file_write"},
        error=None,
        error_type=None,
        duration_ms=10,
        thread_id="t1",
        tool_call_count=1,
        subagent_task_completions=0,
        hit_subagent_cap=False,
        hit_tool_budget=False,
    )
    bad = SimpleNamespace(
        step_id="S2",
        success=False,
        outcome={},
        error="boom",
        error_type="execution",
        duration_ms=20,
        thread_id="t2",
        tool_call_count=0,
        subagent_task_completions=0,
        hit_subagent_cap=False,
        hit_tool_budget=False,
    )
    rail = _RecordingRailInterpreter()
    ctx = _make_ctx(ce, goal.id, step_results=[ok, bad], rail_interpreter=rail)

    await node_record_iteration(ctx, {})

    names = [e.name for e in rail.events]
    assert names == ["step_completed", "step_failed"]
    completed = rail.events[0]
    assert completed.job_id == goal.id
    assert completed.goal_id == goal.id
    assert completed.payload["step_id"] == "S1"
    assert completed.payload["success"] is True
    failed = rail.events[1]
    assert failed.payload["step_id"] == "S2"
    assert failed.payload["success"] is False
    assert failed.payload["error"] == "boom"
    assert failed.payload["error_type"] == "execution"


@pytest.mark.asyncio
async def test_no_rail_interpreter_is_noop() -> None:
    """Without a bound rail interpreter, no events are emitted (no error)."""
    from soothe.sloop.stations.execute.record_progress import node_record_iteration

    ce = ContextEngine(
        persistence=SqliteContextPersistence(loop_id="test", db_path=Path(":memory:"))
    )
    goal = GoalNode(description="goal")
    ce._dag.add_goal(goal)
    ok = SimpleNamespace(
        step_id="S1",
        success=True,
        outcome={},
        error=None,
        error_type=None,
        duration_ms=1,
        thread_id="t1",
        tool_call_count=0,
        subagent_task_completions=0,
        hit_subagent_cap=False,
        hit_tool_budget=False,
    )
    ctx = _make_ctx(ce, goal.id, step_results=[ok], rail_interpreter=None)
    # Must not raise.
    await node_record_iteration(ctx, {})


@pytest.mark.asyncio
async def test_decompose_parent_steps_skipped() -> None:
    """Steps that queued decomposition proposals are not emitted."""
    from soothe.sloop.stations.execute.record_progress import node_record_iteration

    ce = ContextEngine(
        persistence=SqliteContextPersistence(loop_id="test", db_path=Path(":memory:"))
    )
    goal = GoalNode(description="goal")
    ce._dag.add_goal(goal)
    parent = SimpleNamespace(
        step_id="P1",
        success=True,
        outcome={},
        error=None,
        error_type=None,
        duration_ms=1,
        thread_id="t1",
        tool_call_count=0,
        subagent_task_completions=0,
        hit_subagent_cap=False,
        hit_tool_budget=False,
    )
    rail = _RecordingRailInterpreter()
    ctx = _make_ctx(ce, goal.id, step_results=[parent], rail_interpreter=rail)
    # Mark P1 as a decompose parent so its event is skipped.
    ctx.scratch.decompose_proposals = [
        SimpleNamespace(parent_step_id="P1"),
    ]

    await node_record_iteration(ctx, {})

    assert rail.events == []


def _step_node(step_id: str):
    from soothe.context.models import StepNode

    return StepNode(id=step_id, description=step_id, status="pending")
