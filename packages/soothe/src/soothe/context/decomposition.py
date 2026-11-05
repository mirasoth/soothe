"""Decomposition proposal types for recursive step decompose."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator
from soothe_nano.protocols.routing import TaskComplexity

from soothe.context.models import ExecutionHint
from soothe.sloop.clarification.question_schema import QuestionSpec

StepOutcome = Literal["complete", "decompose", "blocked"]


class ProposedSubtask(BaseModel):
    """One proposed child step inside a `DecompositionProposal`."""

    description: str
    full_description: str = ""
    expected_output: str = ""
    execution_hint: ExecutionHint | None = "auto"
    depends_on_local: list[int] | None = None
    in_scope: bool = Field(
        default=True,
        description="Eval assertion that this child is within the original user goal.",
    )
    necessary_for_user_goal: bool = Field(
        default=True,
        description="Eval assertion that this child is necessary to complete the user goal.",
    )
    task_complexity: TaskComplexity = Field(
        default=TaskComplexity.SIMPLE,
        description=(
            "Complexity of this child step: simple (single focused step) or "
            "complex (needs further decomposition)."
        ),
    )

    @field_validator("description")
    @classmethod
    def _description_nonempty(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            return "(untitled subtask)"
        return text


class DecompositionProposal(BaseModel):
    """Local parent-relative proposal queued for CE reconcile."""

    parent_step_id: str
    subtasks: list[ProposedSubtask] = Field(min_length=1)
    wave_seq: int = 0

    @field_validator("parent_step_id")
    @classmethod
    def _parent_nonempty(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("parent_step_id must be non-empty")
        return text

    @field_validator("subtasks")
    @classmethod
    def _validate_local_deps(cls, subtasks: list[ProposedSubtask]) -> list[ProposedSubtask]:
        n = len(subtasks)
        for idx, sub in enumerate(subtasks):
            deps = sub.depends_on_local or []
            if not deps:
                continue
            filtered = [d for d in deps if 0 <= d < n and d != idx]
            if len(filtered) != len(deps):
                sub.depends_on_local = filtered or None
        return subtasks


class CoverageClarification(BaseModel):
    """A user-decision gate surfaced by an Eval step via `coverage_verdict`.

    Carries structured `QuestionSpec` questions so the relay and TUI render
    coverage gates identically to live `ask_user` gates. Routed straight to
    `await_clarification`; never materialized as an execution step.
    """

    questions: list[QuestionSpec] = Field(min_length=1)
    reason: str = Field(default="", description="Why the goal is blocked on the user.")


class CoverageVerdict(BaseModel):
    """Structured verdict emitted by an Eval step via the coverage_verdict tool.

    `complete` is the only sanctioned signal that a goal is fully covered.
    When false, `remaining_subtasks` are queued as a `DecompositionProposal`
    so reconcile materializes continuation steps for the next plan-execute wave.

    `pending_clarification` (IG-LOOP-015) reports a user-decision gate that
    blocks the goal: the verdict routes it straight to `await_clarification`
    via the relay instead of materializing an execution step whose brief is
    "ask the user" (closes the search_tools spiral observed in loop b047).
    At most one of `remaining_subtasks` (continuation work) and
    `pending_clarification` (blocked on user) is meaningful per verdict.
    """

    complete: bool
    reasoning: str = ""
    remaining_subtasks: list[ProposedSubtask] = Field(default_factory=list)
    pending_clarification: CoverageClarification | None = None
