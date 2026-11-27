"""Safety fallbacks for structured action close assessment."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from soothe.context.models import StepCloseReport
from soothe.sloop.eval.step_close_report import _ASSESS_DEADLINE_S, assess_step_close


@pytest.mark.asyncio
async def test_missing_fast_model_requires_eval() -> None:
    report = await assess_step_close(
        fast_model=None,
        user_goal="finish the migration",
        step_description="migrate the database",
        final_output="partial result",
        outcome_summary={},
    )

    assert report.goal_portion_complete is False
    assert report.early_exit is True
    assert report.requires_eval is True


def test_step_close_report_schema_requires_semantic_fields() -> None:
    """Required fields let post-validation reject hallucinated payloads."""
    schema = StepCloseReport.model_json_schema()
    assert "goal_portion_complete" in schema["required"]
    assert "early_exit" in schema["required"]


@pytest.mark.asyncio
async def test_assess_forwards_structured_deadline() -> None:
    """The assessment call carries a wall-clock deadline (loop-stall guard)."""
    with (
        patch(
            "soothe.sloop.eval.step_close_report.ainvoke_structured_traced",
            new=AsyncMock(return_value={"goal_portion_complete": True, "early_exit": False}),
        ) as traced,
    ):
        report = await assess_step_close(
            fast_model=object(),
            user_goal="g",
            step_description="s",
            final_output="o",
            outcome_summary={},
        )

    assert report.goal_portion_complete is True
    assert traced.await_args.kwargs["deadline_s"] == _ASSESS_DEADLINE_S


@pytest.mark.asyncio
async def test_assess_fails_closed_on_structured_error() -> None:
    with (
        patch(
            "soothe.sloop.eval.step_close_report.ainvoke_structured_traced",
            new=AsyncMock(side_effect=RuntimeError("deadline exceeded")),
        ),
    ):
        report = await assess_step_close(
            fast_model=object(),
            user_goal="g",
            step_description="s",
            final_output="o",
            outcome_summary={},
        )

    assert report.goal_portion_complete is False
    assert report.early_exit is True
    assert report.requires_eval is True
