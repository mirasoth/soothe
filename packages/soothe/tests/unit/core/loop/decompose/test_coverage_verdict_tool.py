"""Unit tests for the structured coverage_verdict Eval tool."""

from __future__ import annotations

import asyncio

from soothe.sloop.decompose.runtime import bind_decompose_runtime, reset_decompose_runtime
from soothe.sloop.eval.verdict_tool import build_coverage_verdict_tool

_TOOL = build_coverage_verdict_tool()


def _bind(
    step_id: str,
    proposals: list,
    verdicts: list,
    clarifications: list | None = None,
):
    return bind_decompose_runtime(
        step_id=step_id,
        sink=proposals,
        verdict_sink=verdicts,
        clarification_sink=clarifications,
    )


def test_complete_verdict_records_no_proposal() -> None:
    """A complete verdict is recorded on the verdict sink and queues no proposal."""
    proposals: list = []
    verdicts: list = []
    tokens = _bind("EVAL-1", proposals, verdicts)
    try:
        result = asyncio.run(_TOOL.ainvoke({"complete": True, "reasoning": "done"}))
    finally:
        reset_decompose_runtime(tokens)

    assert "complete" in result.lower()
    assert len(verdicts) == 1
    assert verdicts[0]["complete"] is True
    assert proposals == []


def test_incomplete_verdict_with_subtasks_queues_proposal() -> None:
    """An incomplete verdict with remaining subtasks queues a continuation proposal."""
    proposals: list = []
    verdicts: list = []
    tokens = _bind("EVAL-2", proposals, verdicts)
    try:
        result = asyncio.run(
            _TOOL.ainvoke(
                {
                    "complete": False,
                    "reasoning": "gaps remain",
                    "remaining_subtasks": [
                        {"description": "Write phase3 source"},
                        {"description": "Run cargo test"},
                    ],
                }
            )
        )
    finally:
        reset_decompose_runtime(tokens)

    assert "continuation subtask" in result.lower()
    assert len(verdicts) == 1
    assert verdicts[0]["complete"] is False
    assert len(proposals) == 1
    assert proposals[0].parent_step_id == "EVAL-2"
    assert len(proposals[0].subtasks) == 2


def test_pending_clarification_routes_to_clarification_sink() -> None:
    """IG-LOOP-015: a user-decision gate routes to the clarification sink,
    never to the proposal sink — no execution step is materialized."""
    proposals: list = []
    verdicts: list = []
    clarifications: list = []
    tokens = _bind("EVAL-3", proposals, verdicts, clarifications=clarifications)
    try:
        result = asyncio.run(
            _TOOL.ainvoke(
                {
                    "complete": False,
                    "reasoning": "blocked on projector architecture decision",
                    "pending_clarification": {
                        "reason": "Decision #1 unresolved",
                        "questions": [
                            {
                                "question": "Which projector architecture?",
                                "header": "Arch",
                                "options": [
                                    {"label": "Unified", "description": "One path"},
                                    {"label": "Separate", "description": "Per-type"},
                                ],
                            }
                        ],
                    },
                }
            )
        )
    finally:
        reset_decompose_runtime(tokens)

    assert "blocked on user decision" in result.lower()
    assert len(verdicts) == 1
    assert verdicts[0]["complete"] is False
    assert len(clarifications) == 1
    assert clarifications[0]["reason"] == "Decision #1 unresolved"
    assert len(clarifications[0]["questions"]) == 1
    assert proposals == [], "no execution step must be materialized for a user-decision gate"


def test_pending_clarification_without_sink_returns_error() -> None:
    """No clarification sink bound → clear error, no silent drop."""
    proposals: list = []
    verdicts: list = []
    tokens = _bind("EVAL-4", proposals, verdicts, clarifications=None)
    try:
        result = asyncio.run(
            _TOOL.ainvoke(
                {
                    "complete": False,
                    "reasoning": "blocked",
                    "pending_clarification": {
                        "reason": "need approval",
                        "questions": [
                            {
                                "question": "Approve?",
                                "header": "Approve",
                                "options": [
                                    {"label": "Yes", "description": "go"},
                                    {"label": "No", "description": "stop"},
                                ],
                            }
                        ],
                    },
                }
            )
        )
    finally:
        reset_decompose_runtime(tokens)

    assert "error" in result.lower()
    assert "clarification sink" in result.lower()


def test_tool_without_runtime_binding_is_safe() -> None:
    """Calling the tool outside a bound step thread returns a clear error."""

    async def _call() -> str:
        return await _TOOL.ainvoke({"complete": True, "reasoning": "x"})

    result = asyncio.run(_call())
    assert "error" in result.lower()
