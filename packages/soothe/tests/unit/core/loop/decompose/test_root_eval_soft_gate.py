"""IG-SLP-015: soft coverage-eval gate for the medium single-leaf branch.

Audit PCE signals no longer hard-force an Eval on the medium/unknown
single-leaf path — they become evidence for the decision model
(`decide_eval_required`). L1 guarantees (stalled confidence bar, recency
override) skip the model entirely; COMPLEX keeps the hard force.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from soothe.config.models import DecomposeLoopConfig, EvalLoopConfig
from soothe.context.engine import ContextEngine
from soothe.context.models import CoverageSignal, StepNode
from soothe.sloop.eval.eval_decision import EvalDecision
from soothe.sloop.orchestrator.runtime_context import (
    LoopPhaseScratch,
    LoopRuntimeContext,
)
from soothe.sloop.stations.decompose.root_eval import RootEvalNode

_DECIDER = "soothe.sloop.eval.eval_decision.decide_eval_required"


def _ctx_with_ce(
    ce: ContextEngine,
    goal_id: str,
    *,
    task_complexity: str | None = "medium",
) -> LoopRuntimeContext:
    routing = (
        SimpleNamespace(task_complexity=task_complexity) if task_complexity is not None else None
    )
    loop_state = SimpleNamespace(
        goal="analyze the image",
        goal_user_submission="analyze the image",
        iteration=0,
        max_iterations=99,
        current_decision=None,
        plan_id=None,
        step_results=[],
        routing_classification=routing,
    )
    strange_loop = SimpleNamespace(
        config=SimpleNamespace(
            agent=SimpleNamespace(
                loop=SimpleNamespace(
                    decompose=DecomposeLoopConfig(max_waves=10),
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
    )
    ctx.interaction_mode = None  # type: ignore[method-assign]
    return ctx


async def _goal_with_completed_leaf(ce: ContextEngine, **signal: object):
    goal = await ce.create_goal("analyze the image", loop_id="L1")
    await ce.add_step(
        goal.id,
        StepNode(id="S1", description="analyze image", status="completed"),
    )
    if signal:
        fresh = await ce.get_goal(goal.id)
        assert fresh is not None
        fresh.coverage_signals.append(CoverageSignal(goal_id=fresh.id, **signal))  # type: ignore[arg-type]
    return goal


@pytest.mark.asyncio
async def test_soft_gate_skip_finalizes_and_consumes_signals() -> None:
    """Decision model says skip → finalize; audit signals consumed, no eval node."""
    ce = ContextEngine()
    goal = await _goal_with_completed_leaf(ce, kind="stalled", confidence=0.9)
    ctx = _ctx_with_ce(ce, goal.id)
    decision = EvalDecision(should_run_eval=False, reasoning="Steps already cover the goal.")

    with patch(_DECIDER, AsyncMock(return_value=decision)) as model:
        result = await RootEvalNode()(ctx, {})

    assert result["root_eval_route"] == "finalize"
    model.assert_awaited_once()
    refreshed = await ce.get_goal(goal.id)
    assert refreshed is not None
    assert all(s.consumed_by == "root_eval" for s in refreshed.coverage_signals)
    assert not [n for n in refreshed.steps.nodes.values() if n.kind == "eval"]


@pytest.mark.asyncio
async def test_soft_gate_run_inserts_eval() -> None:
    """Decision model says run → fall through to Eval insertion (dispatch)."""
    ce = ContextEngine()
    goal = await _goal_with_completed_leaf(ce, kind="stalled", confidence=0.9)
    ctx = _ctx_with_ce(ce, goal.id)
    decision = EvalDecision(should_run_eval=True, reasoning="Coverage is uncertain.")

    with patch(_DECIDER, AsyncMock(return_value=decision)) as model:
        result = await RootEvalNode()(ctx, {})

    assert result["root_eval_route"] == "dispatch"
    model.assert_awaited_once()
    refreshed = await ce.get_goal(goal.id)
    assert refreshed is not None
    eval_nodes = [n for n in refreshed.steps.nodes.values() if n.kind == "eval"]
    assert len(eval_nodes) == 1
    assert eval_nodes[0].status == "pending"


@pytest.mark.asyncio
async def test_complex_bypasses_the_decision_model() -> None:
    """COMPLEX single-leaf goals keep the hard force — the model is not called."""
    ce = ContextEngine()
    goal = await _goal_with_completed_leaf(ce, kind="stalled", confidence=0.9)
    ctx = _ctx_with_ce(ce, goal.id, task_complexity="complex")

    with patch(_DECIDER, AsyncMock()) as model:
        result = await RootEvalNode()(ctx, {})

    assert result["root_eval_route"] == "dispatch"
    model.assert_not_awaited()
    refreshed = await ce.get_goal(goal.id)
    assert refreshed is not None
    assert [n for n in refreshed.steps.nodes.values() if n.kind == "eval"]


@pytest.mark.asyncio
async def test_stalled_below_bar_skips_without_model_call() -> None:
    """L1 guarantee: stalled below the confidence bar skips with no LLM."""
    ce = ContextEngine()
    goal = await _goal_with_completed_leaf(ce, kind="stalled", confidence=0.68)
    ctx = _ctx_with_ce(ce, goal.id)

    with patch(_DECIDER, AsyncMock()) as model:
        result = await RootEvalNode()(ctx, {})

    assert result["root_eval_route"] == "finalize"
    model.assert_not_awaited()


@pytest.mark.asyncio
async def test_recency_override_skips_without_model_call() -> None:
    """L1 guarantee: newer high-confidence completion supersedes older stalled."""
    ce = ContextEngine()
    goal = await ce.create_goal("analyze the image", loop_id="L1")
    await ce.add_step(
        goal.id,
        StepNode(id="S1", description="analyze image", status="completed"),
    )
    fresh = await ce.get_goal(goal.id)
    assert fresh is not None
    fresh.coverage_signals.append(CoverageSignal(goal_id=fresh.id, kind="stalled", confidence=0.9))
    fresh.coverage_signals.append(
        CoverageSignal(goal_id=fresh.id, kind="goal_likely_complete", confidence=0.9)
    )
    ctx = _ctx_with_ce(ce, goal.id)

    with patch(_DECIDER, AsyncMock()) as model:
        result = await RootEvalNode()(ctx, {})

    assert result["root_eval_route"] == "finalize"
    model.assert_not_awaited()


@pytest.mark.asyncio
async def test_soft_gate_receives_pce_evidence() -> None:
    """Audit signals are passed as evidence lines, not silently dropped."""
    ce = ContextEngine()
    goal = await _goal_with_completed_leaf(ce, kind="stalled", confidence=0.9)
    ctx = _ctx_with_ce(ce, goal.id)
    decision = EvalDecision(should_run_eval=False, reasoning="Signals are stale noise.")

    with patch(_DECIDER, AsyncMock(return_value=decision)) as model:
        await RootEvalNode()(ctx, {})

    kwargs = model.await_args.kwargs
    assert kwargs["pce_evidence"]
    assert any("kind=stalled" in line for line in kwargs["pce_evidence"])
