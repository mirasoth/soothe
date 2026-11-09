"""Tests for ``route_after_enter`` branch dispatch and wired-subagent routing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from soothe.sloop.orchestrator.routing import route_after_enter
from soothe.sloop.plans.wired_subagent_plan import build_wired_subagent_plan
from soothe.sloop.stations.preprocess.enter_loop import node_init_or_resume
from soothe.sloop.utils.routing_scope import build_routing_classification

# -- route_after_enter truth table -----------------------------------------------


def test_route_after_enter_wired_subagent_routes_to_delegate() -> None:
    state = {"intent_route": "wired_subagent"}
    assert route_after_enter(state) == "delegate"


def test_route_after_enter_continue_loop_routes_to_dispatch() -> None:
    state = {"intent_route": "continue_loop"}
    assert route_after_enter(state) == "dispatch"


def test_route_after_enter_missing_route_falls_back_to_dispatch() -> None:
    """Fail-safe: a missing route on fresh routes to DISPATCH."""
    assert route_after_enter({}) == "dispatch"


# -- node_init_or_resume ----------------------------------------------------------


async def _noop_emit(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
    """No-op event sink for init_or_resume tests."""
    return None


@pytest.mark.asyncio
async def test_init_or_resume_wired_hint_routes_to_delegate() -> None:
    """An explicit preferred_subagent routes to the wired branch."""
    ctx = SimpleNamespace(
        loop_state=SimpleNamespace(
            routing_classification=build_routing_classification(None, "deep_research"),
            goal="Begin research on the seed paper",
            goal_user_submission="Begin research on the seed paper",
        ),
        preferred_subagent="deep_research",
        interaction_mode="agent",
        emit=_noop_emit,
    )

    result = await node_init_or_resume(ctx, {})  # type: ignore[arg-type]

    assert result["intent_route"] == "wired_subagent"
    assert route_after_enter(result) == "delegate"


@pytest.mark.asyncio
async def test_init_or_resume_without_hint_routes_to_dispatch() -> None:
    """With no explicit hint, enter_loop routes to DISPATCH."""
    ctx = SimpleNamespace(
        loop_state=SimpleNamespace(
            routing_classification=build_routing_classification(None, None),
            goal="research the seed paper",
            goal_user_submission="research the seed paper",
        ),
        preferred_subagent=None,
        interaction_mode="agent",
        emit=_noop_emit,
    )

    result = await node_init_or_resume(ctx, {})  # type: ignore[arg-type]

    assert result["intent_route"] == "continue_loop"
    assert route_after_enter(result) == "dispatch"


# -- Wired-subagent plan shape ---------------------------------------------------


def test_wired_subagent_plan_has_no_synthetic_reasoning_prefix() -> None:
    """The wired-subagent plan emits the goal as the step, no verbose prefix."""
    plan = build_wired_subagent_plan("list files in this directory")
    assert plan.next_action == "list files in this directory"
    assert plan.decision is not None
    assert plan.decision.reasoning == ""
    assert plan.terminal_after_execute is True
    assert plan.require_goal_completion is False
    assert "Direct answer" in plan.decision.steps[0].expected_output
    assert plan.decision.steps[0].requires_tool_use is False
