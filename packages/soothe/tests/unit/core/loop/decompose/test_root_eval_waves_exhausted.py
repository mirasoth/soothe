"""Tests for ROOT_EVAL dispatch↔root_eval cycle break when max_waves exhausted.

When ``max_waves`` is reached and the action tree is not green (ready non-eval
steps remain), DISPATCH rejects the ready steps and bounces to ROOT_EVAL.
Before the fix, ROOT_EVAL saw the same ready steps and routed back to DISPATCH
— an unbounded cycle that exhausted the LangGraph recursion budget and aborted
the whole loop on a single goal.

The fix makes ROOT_EVAL fall through to coverage Eval insertion / finalize when
waves are exhausted, so the goal is assessed from work done so far instead of
spinning until the recursion limit trips.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from soothe.config.models import DecomposeLoopConfig, EvalLoopConfig
from soothe.context.engine import ContextEngine
from soothe.context.models import StepNode
from soothe.sloop.intention.models import IntakeLabel
from soothe.sloop.orchestrator.runtime_context import (
    LoopPhaseScratch,
    LoopRuntimeContext,
)
from soothe.sloop.stations.decompose.root_eval import RootEvalNode


class _RecordingRailInterpreter:
    """Captures RailEvents handed to ``handle`` for assertions."""

    def __init__(self) -> None:
        self.events: list = []

    async def handle(self, event: object) -> list:
        self.events.append(event)
        return []


def _ctx_with_ce(
    ce: ContextEngine,
    goal_id: str,
    *,
    iteration: int = 0,
    max_waves: int = 10,
    intake_label: IntakeLabel | None = IntakeLabel.COMPLEX,
    rail_interpreter: object | None = None,
) -> LoopRuntimeContext:
    intent = SimpleNamespace(intake_label=intake_label) if intake_label is not None else None
    loop_state = SimpleNamespace(
        goal="deploy agentspace stack",
        goal_user_submission="deploy agentspace stack",
        iteration=iteration,
        max_iterations=99,
        current_decision=None,
        plan_id=None,
        step_results=[],
        intent=intent,
    )
    strange_loop = SimpleNamespace(
        config=SimpleNamespace(
            agent=SimpleNamespace(
                loop=SimpleNamespace(
                    decompose=DecomposeLoopConfig(max_waves=max_waves),
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
    ctx.interaction_mode = None  # type: ignore[method-assign]
    return ctx


@pytest.mark.asyncio
async def test_waves_not_exhausted_routes_to_dispatch_no_eval() -> None:
    """When waves remain, a pending action step routes to DISPATCH and no Eval
    is inserted — the existing claim-and-execute flow."""
    ce = ContextEngine()
    goal = await ce.create_goal("deploy agentspace stack", loop_id="L1")
    await ce.add_step(
        goal.id,
        StepNode(id="ROOT", description="deploy", status="pending"),
    )
    ctx = _ctx_with_ce(ce, goal.id, iteration=0, max_waves=10)

    result = await RootEvalNode()(ctx, {})

    assert result["root_eval_route"] == "dispatch"
    refreshed = await ce.get_goal(goal.id)
    assert refreshed is not None
    assert not [n for n in refreshed.steps.nodes.values() if n.kind == "eval"]


@pytest.mark.asyncio
async def test_waves_exhausted_inserts_eval_instead_of_cycling() -> None:
    """When max_waves is reached with a pending action step, ROOT_EVAL must NOT
    route back to DISPATCH (which would reject the step and bounce back here,
    cycling until the recursion limit aborts the loop). Instead it falls through
    to coverage Eval insertion so the goal is assessed from work done so far.
    """
    ce = ContextEngine()
    goal = await ce.create_goal("deploy agentspace stack", loop_id="L1")
    await ce.add_step(
        goal.id,
        StepNode(id="ROOT", description="deploy", status="pending"),
    )
    rail = _RecordingRailInterpreter()
    ctx = _ctx_with_ce(
        ce,
        goal.id,
        iteration=10,
        max_waves=10,
        rail_interpreter=rail,
    )

    result = await RootEvalNode()(ctx, {})

    # Eval is inserted and route is dispatch so DISPATCH can run the eval-kind
    # step (accepted even with waves exhausted). This breaks the cycle: the
    # next DISPATCH claims the eval, not the stuck action step.
    assert result["root_eval_route"] == "dispatch"
    refreshed = await ce.get_goal(goal.id)
    assert refreshed is not None
    eval_nodes = [n for n in refreshed.steps.nodes.values() if n.kind == "eval"]
    assert len(eval_nodes) == 1
    assert eval_nodes[0].status == "pending"
    assert eval_nodes[0].parent_step_id == "ROOT"


@pytest.mark.asyncio
async def test_waves_exhausted_skips_dag_idle_when_tree_not_green() -> None:
    """dag_idle must not fire when the action tree isn't green — otherwise rail
    rules could trigger a false complete_job on incomplete work."""
    ce = ContextEngine()
    goal = await ce.create_goal("deploy agentspace stack", loop_id="L1")
    await ce.add_step(
        goal.id,
        StepNode(id="ROOT", description="deploy", status="pending"),
    )
    rail = _RecordingRailInterpreter()
    ctx = _ctx_with_ce(
        ce,
        goal.id,
        iteration=10,
        max_waves=10,
        rail_interpreter=rail,
    )

    await RootEvalNode()(ctx, {})

    assert rail.events == []


@pytest.mark.asyncio
async def test_waves_exhausted_completed_eval_finalizes() -> None:
    """After the inserted Eval runs and records a complete verdict, the next
    ROOT_EVAL finalizes — no infinite Eval loop even with waves exhausted."""
    from soothe.context.models import StepExecution

    ce = ContextEngine()
    goal = await ce.create_goal("deploy agentspace stack", loop_id="L1")
    await ce.add_step(
        goal.id,
        StepNode(id="ROOT", description="deploy", status="pending"),
    )
    await ce.add_step(
        goal.id,
        StepNode(
            id="ROOT-EVAL",
            description="Evaluate user-goal coverage",
            status="completed",
            parent_step_id="ROOT",
            kind="eval",
            plan_iteration=1,
            execution=StepExecution(
                outcome={"coverage_verdict": {"complete": True, "reasoning": "done"}},
            ),
        ),
    )
    ctx = _ctx_with_ce(ce, goal.id, iteration=10, max_waves=10)

    result = await RootEvalNode()(ctx, {})

    assert result["root_eval_route"] == "finalize"


@pytest.mark.asyncio
async def test_waves_exhausted_max_eval_rounds_fatal() -> None:
    """When eval rounds are also exhausted, ROOT_EVAL goes fatal (→ FINALIZE)
    rather than cycling."""
    ce = ContextEngine()
    goal = await ce.create_goal("deploy agentspace stack", loop_id="L1")
    await ce.add_step(
        goal.id,
        StepNode(id="ROOT", description="deploy", status="pending"),
    )
    # Add max_eval_rounds (default 10) completed evals without complete verdicts.
    for i in range(10):
        await ce.add_step(
            goal.id,
            StepNode(
                id=f"ROOT-EVAL{i}",
                description="Evaluate user-goal coverage",
                status="completed",
                parent_step_id="ROOT",
                kind="eval",
                plan_iteration=i + 1,
            ),
        )
    ctx = _ctx_with_ce(
        ce,
        goal.id,
        iteration=10,
        max_waves=10,
        # Use an unlabeled intent so the structural skip path is taken and the
        # max-rounds guard fires before any intake-gated eval decision.
        intake_label=None,
    )

    result = await RootEvalNode()(ctx, {})

    assert result["root_eval_route"] == "fatal"
