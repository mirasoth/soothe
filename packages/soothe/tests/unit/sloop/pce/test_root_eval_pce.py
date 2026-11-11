"""ROOT_EVAL PCE signal consumption tests (RFC-SLP-005).

The fourth eval-required condition: unconsumed audit-requiring signals
force a coverage Eval even for single-leaf goals.
"""

from __future__ import annotations

from typing import Any

from soothe.context.models import (
    GOAL_LIKELY_COMPLETE_AUDIT_BAR,
    CoverageSignal,
    GoalNode,
    StepNode,
)
from soothe.sloop.stations.decompose import root_eval as re_mod


def _goal_with_signal(kind: str, confidence: float, **signal_kwargs: Any) -> GoalNode:
    goal = GoalNode(description="finish the work", status="active")
    step = StepNode(id="S1", description="single big leaf", status="completed")
    goal.steps.nodes[step.id] = step
    goal.coverage_signals.append(
        CoverageSignal(goal_id=goal.id, kind=kind, confidence=confidence, **signal_kwargs)
    )
    return goal


def test_audit_requiring_kinds_force_audit():
    for kind in ("coverage_gap", "runaway_step", "stalled", "scope_drift"):
        goal = _goal_with_signal(kind, 0.9)
        signals = re_mod._pce_audit_signals(goal)
        assert len(signals) == 1, kind


def test_goal_likely_complete_below_bar_forces_audit():
    goal = _goal_with_signal("goal_likely_complete", GOAL_LIKELY_COMPLETE_AUDIT_BAR - 0.05)
    assert len(re_mod._pce_audit_signals(goal)) == 1


def test_goal_likely_complete_at_or_above_bar_does_not_force():
    goal = _goal_with_signal("goal_likely_complete", 0.9)
    assert re_mod._pce_audit_signals(goal) == []


def test_on_track_never_forces_audit():
    goal = _goal_with_signal("on_track", 0.3)
    assert re_mod._pce_audit_signals(goal) == []


def test_consumed_signals_are_ignored():
    goal = _goal_with_signal("runaway_step", 0.9, consumed_by="root_eval")
    assert re_mod._pce_audit_signals(goal) == []


def test_consume_marks_signals():
    goal = _goal_with_signal("runaway_step", 0.9)
    signals = re_mod._pce_audit_signals(goal)
    re_mod._consume_pce_signals(goal, "root_eval", signals)
    assert goal.coverage_signals[0].consumed_by == "root_eval"
    assert re_mod._pce_audit_signals(goal) == []


def test_envelope_appends_unconsumed_signals():
    goal = _goal_with_signal("runaway_step", 0.75)
    nodes = list(goal.steps.nodes.values())
    envelope = re_mod._eval_envelope_with_signals("the goal", nodes, goal)
    assert "PCE COVERAGE SIGNALS" in envelope
    assert "kind=runaway_step conf=0.75" in envelope


def test_envelope_without_signals_is_plain():
    goal = GoalNode(description="x", status="active")
    step = StepNode(id="S1", description="leaf", status="completed")
    goal.steps.nodes[step.id] = step
    nodes = list(goal.steps.nodes.values())
    envelope = re_mod._eval_envelope_with_signals("the goal", nodes, goal)
    assert "PCE COVERAGE SIGNALS" not in envelope
