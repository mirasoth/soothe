"""Single-step plan builder for the wired-subagent delegate path."""

from __future__ import annotations

from soothe.sloop.state.schemas import (
    AgentDecision,
    PlanResult,
    StepAction,
    resolve_wire_subagent,
)
from soothe.sloop.utils.step_title import preview_step_title

# Soft expected-output contract for the wired specialist's direct answer.
_WIRED_SUBAGENT_EXPECTED_OUTPUT = (
    "Direct answer to the user's request. Use tool results when the goal needs "
    "live or external data; otherwise answer from reasoning."
)


def build_wired_subagent_plan(
    goal: str,
    *,
    wire_subagent: str | None = None,
    requires_tool_use: bool = False,
) -> PlanResult:
    """Build a minimal 1-step terminal plan for a wired subagent.

    Args:
        goal: The user's goal text (verbatim submission).
        wire_subagent: Allowlisted specialist for the wired-subagent route.
        requires_tool_use: Execute deliverable-gate signal for the step.

    Returns:
        A `PlanResult` with a single execute step whose action is the goal
        itself and a soft direct-answer `expected_output` contract. The step
        title is a one-line preview; `full_description` keeps the verbatim goal.
    """
    resolved_wire = resolve_wire_subagent(wire_subagent=wire_subagent)
    step = StepAction(
        description=preview_step_title(goal),
        full_description=goal,
        expected_output=_WIRED_SUBAGENT_EXPECTED_OUTPUT,
        requires_tool_use=requires_tool_use,
        is_dag_root=True,
        task_complexity="simple",
    )
    if resolved_wire:
        step = step.model_copy(
            update={
                "execution_hint": "subagent",
                "subagent": resolved_wire,
            }
        )

    return PlanResult(
        status="continue",
        goal_progress="none",
        assessment_reasoning="",
        plan_action="new",
        require_goal_completion=False,
        terminal_after_execute=True,
        decision=AgentDecision(
            type="execute_steps",
            execution_mode="parallel",
            reasoning="",
            steps=[step],
        ),
        next_action=goal[:300],
    )


__all__ = ["build_wired_subagent_plan"]
