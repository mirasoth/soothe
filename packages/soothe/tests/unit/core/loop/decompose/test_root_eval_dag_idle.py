"""Tests for dag_idle RailEvent emission in ROOT_EVAL."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from soothe.config.models import DecomposeLoopConfig, EvalLoopConfig
from soothe.context.engine import ContextEngine
from soothe.context.models import StepNode
from soothe.rails.interpreter import RailEvent
from soothe.sloop.orchestrator.runtime_context import (
    LoopPhaseScratch,
    LoopRuntimeContext,
)
from soothe.sloop.stations.decompose.root_eval import RootEvalNode


class _RecordingRailInterpreter:
    """Captures RailEvents handed to ``handle`` for assertions."""

    def __init__(self) -> None:
        self.events: list[RailEvent] = []

    async def handle(self, event: RailEvent) -> list:
        self.events.append(event)
        return []


def _ctx_with_ce(
    ce: ContextEngine,
    goal_id: str,
    *,
    rail_interpreter: object | None,
    interaction_mode: str | None = None,
) -> LoopRuntimeContext:
    loop_state = SimpleNamespace(
        goal="do work",
        goal_user_submission="do work",
        iteration=0,
        max_iterations=8,
        current_decision=None,
        plan_id=None,
        step_results=[],
        intent=None,
    )
    strange_loop = SimpleNamespace(
        config=SimpleNamespace(
            agent=SimpleNamespace(
                loop=SimpleNamespace(
                    decompose=DecomposeLoopConfig(),
                    eval=EvalLoopConfig(),
                    max_step_retries=2,
                )
            )
        ),
        _fast_llm=None,
    )
    checkpoint = SimpleNamespace(
        thread_health_metrics=SimpleNamespace(consecutive_rate_limit_errors=0),
    )
    ctx = LoopRuntimeContext(
        strange_loop=strange_loop,  # type: ignore[arg-type]
        state_manager=MagicMock(),
        plan_manager=MagicMock(),
        checkpoint=checkpoint,  # type: ignore[arg-type]
        goal_record=None,
        continue_loop_mode=False,
        recovery_valid_resume=False,
        loop_state=loop_state,  # type: ignore[arg-type]
        emit=AsyncMock(),
        ce=ce,
        ce_goal_id=goal_id,
        scratch=LoopPhaseScratch(),
        rail_interpreter=rail_interpreter,
    )
    ctx.interaction_mode = interaction_mode  # type: ignore[method-assign]
    return ctx


@pytest.mark.asyncio
async def test_dag_idle_emitted_when_action_tree_green() -> None:
    """A green action tree with no pending steps emits dag_idle before finalize."""
    ce = ContextEngine()
    goal = await ce.create_goal("do work", loop_id="L1")
    await ce.add_step(
        goal.id,
        StepNode(id="ROOT", description="root", status="completed"),
    )
    rail = _RecordingRailInterpreter()
    ctx = _ctx_with_ce(ce, goal.id, rail_interpreter=rail, interaction_mode=None)

    result = await RootEvalNode()(ctx, {})

    assert result["root_eval_route"] == "finalize"
    assert len(rail.events) == 1
    event = rail.events[0]
    assert event.name == "dag_idle"
    assert event.job_id == goal.id
    assert event.goal_id == goal.id
    assert event.payload["completed_step_ids"] == ["ROOT"]
    assert event.payload["node_count"] == 1


@pytest.mark.asyncio
async def test_dag_idle_not_emitted_when_tree_not_green() -> None:
    """Pending steps mean the tree is not green; no dag_idle, dispatch instead."""
    ce = ContextEngine()
    goal = await ce.create_goal("do work", loop_id="L1")
    await ce.add_step(
        goal.id,
        StepNode(id="ROOT", description="root", status="pending"),
    )
    rail = _RecordingRailInterpreter()
    ctx = _ctx_with_ce(ce, goal.id, rail_interpreter=rail, interaction_mode=None)

    result = await RootEvalNode()(ctx, {})

    assert result["root_eval_route"] == "dispatch"
    assert rail.events == []


@pytest.mark.asyncio
async def test_dag_idle_not_emitted_when_failed_steps_present() -> None:
    """Failed steps route to fatal; dag_idle is not emitted."""
    ce = ContextEngine()
    goal = await ce.create_goal("do work", loop_id="L1")
    await ce.add_step(
        goal.id,
        StepNode(id="ROOT", description="root", status="failed"),
    )
    rail = _RecordingRailInterpreter()
    ctx = _ctx_with_ce(ce, goal.id, rail_interpreter=rail, interaction_mode=None)

    result = await RootEvalNode()(ctx, {})

    assert result["root_eval_route"] == "fatal"
    assert rail.events == []


@pytest.mark.asyncio
async def test_dag_idle_not_emitted_in_readonly_mode() -> None:
    """Read-only modes finalize early and never reach the green-tree gate."""
    ce = ContextEngine()
    goal = await ce.create_goal("do work", loop_id="L1")
    await ce.add_step(
        goal.id,
        StepNode(id="ROOT", description="root", status="completed"),
    )
    rail = _RecordingRailInterpreter()
    ctx = _ctx_with_ce(ce, goal.id, rail_interpreter=rail, interaction_mode="plan")

    result = await RootEvalNode()(ctx, {})

    assert result["root_eval_route"] == "finalize"
    assert rail.events == []


@pytest.mark.asyncio
async def test_no_rail_interpreter_is_noop() -> None:
    """Without a bound interpreter, the green-tree path still finalizes."""
    ce = ContextEngine()
    goal = await ce.create_goal("do work", loop_id="L1")
    await ce.add_step(
        goal.id,
        StepNode(id="ROOT", description="root", status="completed"),
    )
    ctx = _ctx_with_ce(ce, goal.id, rail_interpreter=None, interaction_mode=None)

    result = await RootEvalNode()(ctx, {})

    assert result["root_eval_route"] == "finalize"
