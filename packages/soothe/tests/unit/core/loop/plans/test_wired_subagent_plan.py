"""Tests for terminal 1-step plans used by the wired-subagent route."""

from __future__ import annotations

from soothe.sloop.plans.wired_subagent_plan import build_wired_subagent_plan
from soothe.sloop.state.schemas import resolve_wire_subagent
from soothe.sloop.utils.step_title import preview_step_title


def test_build_wired_subagent_plan_keeps_verbatim_goal_behind_a_short_title() -> None:
    goal = "## 长文\n\n" + ("分析选项 " * 100)
    step = build_wired_subagent_plan(goal).decision.steps[0]
    assert step.description == preview_step_title(goal)
    assert "\n" not in step.description
    assert step.full_description == goal


def test_resolve_wire_subagent_accepts_slash_hint() -> None:
    assert resolve_wire_subagent(wire_subagent="browser_use") == "browser_use"
    assert resolve_wire_subagent(wire_subagent="deep_research") == "deep_research"
    assert resolve_wire_subagent(wire_subagent="unknown") is None


def test_build_wired_subagent_plan_wires_browser_use() -> None:
    plan = build_wired_subagent_plan(
        "get weather at beijing",
        wire_subagent="browser_use",
    )
    step = plan.decision.steps[0]
    assert step.execution_hint == "subagent"
    assert step.subagent == "browser_use"


def test_build_wired_subagent_plan_propagates_requires_tool_use() -> None:
    plan = build_wired_subagent_plan("shanghai weather now", requires_tool_use=True)
    assert plan.decision.steps[0].requires_tool_use is True


def test_build_wired_subagent_plan_leaves_delegate_none_for_generic_goal() -> None:
    plan = build_wired_subagent_plan("list files in this directory")
    step = plan.decision.steps[0]
    assert step.execution_hint == "auto"
    assert step.subagent is None
