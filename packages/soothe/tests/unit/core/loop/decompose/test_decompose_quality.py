"""Tests for DecomposeQuality recording at action-tree green (RFC-SLP-006 §3)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from soothe.config.models import DecomposeLoopConfig, EvalLoopConfig
from soothe.context.engine import ContextEngine
from soothe.context.models import StepExecution, StepNode
from soothe.sloop.orchestrator.runtime_context import LoopPhaseScratch, LoopRuntimeContext
from soothe.sloop.stations.decompose.root_eval import _record_decompose_quality


def _ctx_with_ce(ce: ContextEngine, goal_id: str, *, ring_size: int = 20) -> LoopRuntimeContext:
    strange_loop = SimpleNamespace(
        config=SimpleNamespace(
            agent=SimpleNamespace(
                loop=SimpleNamespace(
                    decompose=DecomposeLoopConfig(quality_ring_size=ring_size),
                    eval=EvalLoopConfig(),
                )
            )
        )
    )
    return LoopRuntimeContext(
        strange_loop=strange_loop,  # type: ignore[arg-type]
        state_manager=MagicMock(),
        plan_manager=MagicMock(),
        checkpoint=SimpleNamespace(
            thread_health_metrics=SimpleNamespace(consecutive_rate_limit_errors=0),
        ),  # type: ignore[arg-type]
        goal_record=None,
        continue_loop_mode=False,
        recovery_valid_resume=False,
        loop_state=SimpleNamespace(),  # type: ignore[arg-type]
        emit=AsyncMock(),
        ce=ce,
        ce_goal_id=goal_id,
        scratch=LoopPhaseScratch(),
    )


def _child(node_id: str, parent: str, *, deps: list[str] | None = None) -> StepNode:
    return StepNode(
        id=node_id,
        description=node_id,
        status="completed",
        parent_step_id=parent,
        dependencies=deps or [],
        execution=StepExecution(tool_call_count=3, duration_ms=1000),
    )


async def _goal_with_decomposed_parents(ce: ContextEngine) -> str:
    goal = await ce.create_goal("do work", loop_id="L1")
    # ROOT decomposed into a pure chain A <- B.
    await ce.add_step(goal.id, StepNode(id="ROOT", description="root", status="decomposed"))
    await ce.add_step(goal.id, _child("A", "ROOT"))
    await ce.add_step(goal.id, _child("B", "ROOT", deps=["A"]))
    # P2 decomposed into two independent children.
    await ce.add_step(goal.id, StepNode(id="P2", description="p2", status="decomposed"))
    await ce.add_step(goal.id, _child("C", "P2"))
    await ce.add_step(goal.id, _child("D", "P2"))
    # P3 decomposed into a single child.
    await ce.add_step(goal.id, StepNode(id="P3", description="p3", status="decomposed"))
    await ce.add_step(goal.id, _child("E", "P3"))
    return goal.id


@pytest.mark.asyncio
async def test_quality_records_computed_at_tree_green() -> None:
    ce = ContextEngine()
    goal_id = await _goal_with_decomposed_parents(ce)
    goal = await ce.get_goal(goal_id)
    ctx = _ctx_with_ce(ce, goal_id)

    await _record_decompose_quality(ctx, goal)

    by_parent = {q.parent_step_id: q for q in goal.decompose_quality}
    assert set(by_parent) == {"ROOT", "P2", "P3"}

    chain = by_parent["ROOT"]
    assert chain.child_count == 2
    assert chain.single_child is False
    assert chain.sequential_only is True
    assert chain.independent_children == 1
    assert chain.deepest_chain_len == 2
    assert chain.child_tool_calls_sum == 6
    assert chain.child_duration_ms_sum == 2000

    fork = by_parent["P2"]
    assert fork.sequential_only is False
    assert fork.independent_children == 2
    assert fork.deepest_chain_len == 1

    single = by_parent["P3"]
    assert single.single_child is True
    assert single.sequential_only is False

    emitted = [call.args[0] for call in ctx.emit.await_args_list]
    assert emitted.count("decompose_quality") == 3


@pytest.mark.asyncio
async def test_quality_idempotent_per_parent() -> None:
    ce = ContextEngine()
    goal_id = await _goal_with_decomposed_parents(ce)
    goal = await ce.get_goal(goal_id)
    ctx = _ctx_with_ce(ce, goal_id)

    await _record_decompose_quality(ctx, goal)
    await _record_decompose_quality(ctx, goal)

    assert len(goal.decompose_quality) == 3


@pytest.mark.asyncio
async def test_quality_ring_bounded() -> None:
    ce = ContextEngine()
    goal_id = await _goal_with_decomposed_parents(ce)
    goal = await ce.get_goal(goal_id)
    ctx = _ctx_with_ce(ce, goal_id, ring_size=2)

    await _record_decompose_quality(ctx, goal)

    assert len(goal.decompose_quality) == 2


@pytest.mark.asyncio
async def test_quality_skips_parents_with_pending_children() -> None:
    ce = ContextEngine()
    goal = await ce.create_goal("do work", loop_id="L1")
    await ce.add_step(goal.id, StepNode(id="ROOT", description="root", status="decomposed"))
    await ce.add_step(
        goal.id,
        StepNode(id="A", description="a", status="pending", parent_step_id="ROOT"),
    )
    ctx = _ctx_with_ce(ce, goal.id)

    await _record_decompose_quality(ctx, goal)

    assert goal.decompose_quality == []
