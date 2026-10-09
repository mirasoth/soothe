"""Executor-bound `coverage_verdict` tool for Eval steps."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from soothe.context.decomposition import CoverageVerdict, DecompositionProposal, ProposedSubtask
from soothe.prompts import COVERAGE_VERDICT_TOOL_DESCRIPTION
from soothe.sloop.decompose.runtime import (
    current_proposal_sink,
    current_step_id,
    current_verdict_sink,
    current_wave_seq,
)

logger = logging.getLogger(__name__)


class _CoverageVerdictArgs(BaseModel):
    complete: bool = Field(description="True when the original user goal is fully covered.")
    reasoning: str = Field(default="", description="One-line verdict citing the decisive evidence.")
    remaining_subtasks: list[ProposedSubtask] = Field(
        default_factory=list,
        description=(
            "Continuation steps for the next plan-execute wave when complete is false. "
            "Each must be independently executable and grounded in evidence already gathered."
        ),
    )


def _parse_subtask(item: Any) -> ProposedSubtask:
    if isinstance(item, ProposedSubtask):
        return item
    return ProposedSubtask.model_validate(item)


def _record_verdict(verdict: CoverageVerdict, *, step_id: str) -> str:
    sink = current_verdict_sink()
    if sink is None:
        return (
            "Error: coverage_verdict is only available inside a StrangeLoop Eval step "
            "thread with a verdict sink bound."
        )
    sink.append(verdict.model_dump(mode="python"))
    if not verdict.complete and verdict.remaining_subtasks:
        proposal_sink = current_proposal_sink()
        if proposal_sink is None:
            return "Error: no proposal sink bound; continuation subtasks could not be queued."
        proposal = DecompositionProposal(
            parent_step_id=step_id,
            subtasks=verdict.remaining_subtasks,
            wave_seq=current_wave_seq(),
        )
        proposal_sink.append(proposal)
        logger.info(
            "[coverage_verdict] queued continuation proposal parent=%s subtasks=%d wave=%d",
            step_id,
            len(proposal.subtasks),
            proposal.wave_seq,
        )
        return (
            f"Coverage verdict recorded (complete=false, {len(proposal.subtasks)} "
            "continuation subtask(s) queued). This thread should end; do not continue working."
        )
    state = "complete" if verdict.complete else "incomplete (no subtasks queued)"
    logger.info(
        "[coverage_verdict] recorded verdict step=%s complete=%s", step_id, verdict.complete
    )
    return f"Coverage verdict recorded ({state}). This thread should end; do not continue working."


async def _arun_coverage_verdict(
    complete: bool,
    reasoning: str = "",
    remaining_subtasks: list[Any] | None = None,
) -> str:
    step_id = current_step_id()
    if not step_id:
        return (
            "Error: coverage_verdict is only available inside a StrangeLoop Eval step "
            "thread with a verdict sink bound."
        )
    parsed = [_parse_subtask(s) for s in (remaining_subtasks or [])]
    verdict = CoverageVerdict(
        complete=complete,
        reasoning=reasoning,
        remaining_subtasks=parsed,
    )
    return _record_verdict(verdict, step_id=step_id)


def _run_coverage_verdict(
    complete: bool,
    reasoning: str = "",
    remaining_subtasks: list[Any] | None = None,
) -> str:
    step_id = current_step_id()
    if not step_id:
        return (
            "Error: coverage_verdict is only available inside a StrangeLoop Eval step "
            "thread with a verdict sink bound."
        )
    parsed = [_parse_subtask(s) for s in (remaining_subtasks or [])]
    verdict = CoverageVerdict(
        complete=complete,
        reasoning=reasoning,
        remaining_subtasks=parsed,
    )
    return _record_verdict(verdict, step_id=step_id)


def build_coverage_verdict_tool() -> StructuredTool:
    """Build the loop-scoped `coverage_verdict` tool for Eval steps."""
    return StructuredTool.from_function(
        name="coverage_verdict",
        description=COVERAGE_VERDICT_TOOL_DESCRIPTION,
        func=_run_coverage_verdict,
        coroutine=_arun_coverage_verdict,
        args_schema=_CoverageVerdictArgs,
        infer_schema=False,
    )


__all__ = ["build_coverage_verdict_tool"]
