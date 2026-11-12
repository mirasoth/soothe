"""Unit tests for clarification origin constants."""

from __future__ import annotations

from soothe.sloop.clarification.origins import (
    CLARIFICATION_ORIGINS,
    ORIGIN_COVERAGE_GATE,
    ORIGIN_EXECUTE,
    ORIGIN_PLAN_MODE_REVIEW,
    ORIGIN_RAIL_PAUSE,
    ORIGIN_TOOL_APPROVAL,
)


def test_clarification_origins_are_live_only() -> None:
    assert CLARIFICATION_ORIGINS == {
        ORIGIN_EXECUTE,
        ORIGIN_PLAN_MODE_REVIEW,
        ORIGIN_RAIL_PAUSE,
        ORIGIN_TOOL_APPROVAL,
        ORIGIN_COVERAGE_GATE,
    }


def test_coverage_gate_origin_is_distinct() -> None:
    """coverage_verdict user-decision gates carry their own origin."""
    assert ORIGIN_COVERAGE_GATE == "coverage_gate"
    assert ORIGIN_COVERAGE_GATE != ORIGIN_EXECUTE
    assert ORIGIN_COVERAGE_GATE in CLARIFICATION_ORIGINS
