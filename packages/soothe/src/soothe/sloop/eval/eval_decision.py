"""LLM-structured decision on whether a coverage Eval step is needed."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from soothe_nano.llm import ainvoke_structured_traced
from soothe_nano.protocols.routing import TaskComplexity

from soothe.context.models import StepNode

if TYPE_CHECKING:
    from soothe.config import SootheConfig
    from soothe.utils.observability.langfuse import GoalLoopTrace

logger = logging.getLogger(__name__)

_ARTIFACT_PATH_RE = re.compile(
    r"\.?platonic/(?:drafts|specs|impl)/\S+"
    r"|[\w/]+\.(?:py|md|ts|js|rs|go|java|rb|sh|yaml|yml|toml|json)\b",
    re.IGNORECASE,
)

_ASK_USER_TOOL_RE = re.compile(r"\bask_user\b|\bAskUserQuestion\b", re.IGNORECASE)


class EvalDecision(BaseModel):
    """LLM structured decision on whether a coverage Eval step is needed."""

    should_run_eval: bool = Field(
        description=(
            "True if a coverage Eval step should run to audit whether the "
            "original user goal was achieved completely."
        )
    )
    reasoning: str = Field(
        description="Brief first-person reasoning (I'll … / Let me …).",
    )


def _fail_safe_decision(reason: str = "eval decision unavailable") -> EvalDecision:
    """Fail-safe: when the LLM call fails, require Eval (never skip silently)."""
    return EvalDecision(should_run_eval=True, reasoning=reason)


def _step_history_table(nodes: list[StepNode]) -> str:
    """Render a compact step history table for the decision prompt."""
    rows: list[str] = []
    for node in nodes:
        close = node.close_report.model_dump() if node.close_report is not None else None
        outcome = node.execution.outcome if node.execution is not None else None
        rows.append(
            f"- {node.id} kind={node.kind} status={node.status}: {node.description}\n"
            f"  expected={node.expected_output or '(unspecified)'}\n"
            f"  close_report={close or '(none)'}\n"
            f"  outcome={outcome or '(none)'}"
        )
    return "\n".join(rows)


def _extract_structured_signals(step_history: list[StepNode]) -> dict[str, Any]:
    """Extract structured completion signals from the step history.

    These signals give the TypeSafe classifier context beyond raw text,
    enabling it to recognize BRAINSTORM-mode completion (ask_user gate +
    artifact produced) without an LLM round trip.

    Args:
        step_history: All StepNodes in the goal DAG.

    Returns:
        Dict with keys: ``close_report_requires_eval``, ``step_kind``,
        ``ask_user_interrupt``, ``artifact_produced``.
    """
    completed = [n for n in step_history if n.status == "completed" and n.execution is not None]
    if not completed:
        return {
            "close_report_requires_eval": True,
            "step_kind": None,
            "ask_user_interrupt": False,
            "artifact_produced": False,
        }

    last = completed[-1]

    close_requires = any(
        n.close_report is not None and n.close_report.requires_eval for n in completed
    )
    if not any(n.close_report is not None for n in completed):
        close_requires = True

    ask_user = False
    artifact = False
    if last.execution is not None:
        output_msgs = last.execution.output_messages or []
        combined = " ".join(str(m) for m in output_msgs)
        outcome_text = str(last.execution.outcome or "")
        full_text = f"{combined}\n{outcome_text}"
        ask_user = bool(_ASK_USER_TOOL_RE.search(full_text))
        artifact = bool(_ARTIFACT_PATH_RE.search(full_text))

    return {
        "close_report_requires_eval": close_requires,
        "step_kind": str(last.kind) if last.kind else None,
        "ask_user_interrupt": ask_user,
        "artifact_produced": artifact,
    }


async def _classify_eval_coverage(
    *,
    user_goal: str,
    step_history_table: str,
    task_complexity: str | None,
    structured_signals: dict[str, Any] | None = None,
    soothe_config: SootheConfig | None,
) -> bool | None:
    """Try the TypeSafe coverage verdict; never raises, never blocks."""
    try:
        from soothe.sloop.eval.typesafe_eval_decision import decide_eval_coverage_typesafe
    except Exception:  # noqa: BLE001 — optional dependency
        return None
    try:
        return await decide_eval_coverage_typesafe(
            user_goal=user_goal,
            step_history_table=step_history_table,
            task_complexity=task_complexity,
            structured_signals=structured_signals,
            soothe_config=soothe_config,
        )
    except Exception:  # noqa: BLE001 — fall back on any classifier failure
        logger.debug("TypeSafe coverage decision failed; using LLM path", exc_info=True)
        return None


async def decide_eval_required(
    *,
    fast_model: Any,
    user_goal: str,
    step_history: list[StepNode],
    task_complexity: TaskComplexity | None,
    soothe_config: SootheConfig | None = None,
    goal_trace: GoalLoopTrace | None = None,
) -> EvalDecision:
    """Decide whether a coverage Eval step should run.

    MINIMAL short-circuits to skip; any failure fails safe to run Eval.
    """
    complexity_value = (
        getattr(task_complexity, "value", task_complexity)
        if task_complexity is not None
        else "unknown"
    )

    # MINIMAL never needs Eval — skip the LLM call entirely.
    if task_complexity == TaskComplexity.MINIMAL:
        return EvalDecision(
            should_run_eval=False,
            reasoning="Minimal task; no coverage audit needed.",
        )

    if fast_model is None:
        return _fail_safe_decision("No fast model available; requiring Eval")

    # TypeSafe fast path: a trusted categorical verdict answers the coverage
    # question with no LLM round trip. `None` (unavailable, untrusted, or
    # below the suppression bar) falls through to the LLM path unchanged,
    # whose own fail-safe is `should_run_eval=True`.
    structured_signals = _extract_structured_signals(step_history)
    classified = await _classify_eval_coverage(
        user_goal=user_goal,
        step_history_table=_step_history_table(step_history),
        task_complexity=complexity_value,
        structured_signals=structured_signals,
        soothe_config=soothe_config,
    )
    if classified is not None:
        if classified:
            return _fail_safe_decision("Classifier: coverage audit warranted")
        return EvalDecision(
            should_run_eval=False,
            reasoning="Classifier: steps already cover the goal.",
        )

    from soothe.prompts import EVAL_DECISION_SYSTEM

    system = EVAL_DECISION_SYSTEM
    human = (
        f"ORIGINAL USER GOAL:\n{user_goal or '(unspecified)'}\n\n"
        f"TASK COMPLEXITY: {complexity_value}\n\n"
        f"INTRA-GOAL STEP HISTORY:\n{_step_history_table(step_history)}"
    )
    try:
        data = await ainvoke_structured_traced(
            fast_model,
            [SystemMessage(content=system), HumanMessage(content=human)],
            json_schema=EvalDecision.model_json_schema(),
            schema_name="EvalDecision",
            strict=True,
            soothe_config=soothe_config,
            purpose="decide_eval_required",
            component="sloop.eval.eval_decision",
            phase="root_eval",
            goal_trace=goal_trace,
        )
        return EvalDecision.model_validate(data)
    except Exception:
        logger.warning(
            "Eval decision LLM call failed; requiring Eval (fail-safe)",
            exc_info=True,
        )
        return _fail_safe_decision("LLM call failed; requiring Eval")


__all__ = ["EvalDecision", "decide_eval_required"]
