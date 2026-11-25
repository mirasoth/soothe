"""Clarification wire-content formatting for plan-review and tool-approval submits.

Pure utility — no mixin state dependency. Extracted from `_execution.py` to
keep the execution mixin focused on agent execution and message routing.
"""

from __future__ import annotations

_PLAN_REVIEW_ACTIONS = frozenset({"Approve", "Reject", "Refine"})
_TOOL_APPROVAL_ACTIONS = frozenset({"Approve", "Reject", "Edit"})


def clarification_wire_content(answers: list[str], *, origin_node: str = "") -> str:
    """Human-readable turn content for a clarification submit (not a new goal).

    Option-selector actions use a stable ``<prefix>: …`` header so a dropped
    ``clarification_answer`` flag cannot turn a bare action into Pass1 TASK.
    Plan review → ``Plan review:`` (Refine carries refinement text in
    ``answers[1]``). Tool approval → ``Tool approval:``. Multi-tool gates
    send one decision per tool, so a second slot that is itself an action
    token falls through to the per-answer summary instead of being read as
    refinement prose.
    """
    non_empty = [a for a in answers if str(a).strip()]
    if not non_empty:
        return ""
    first = str(answers[0]).strip() if answers else ""
    is_tool_approval = origin_node == "tool_approval"
    is_selector_action = first in _PLAN_REVIEW_ACTIONS or first in _TOOL_APPROVAL_ACTIONS
    if is_selector_action:
        # Origin-aware prefix; when origin is unknown (legacy caller), default
        # to "Plan review" to preserve the pre-RFC behavior where the action
        # label alone selected the plan-review prefix.
        prefix = "Tool approval" if is_tool_approval else "Plan review"
        second = str(answers[1]).strip() if len(answers) > 1 else ""
        # answers[1] is refinement text only when it is not itself an action
        # token — multi-tool gates emit one decision per tool and a trailing
        # comment rides after all decision slots. For multi-tool batches,
        # fall through to the per-answer summary so every decision shows.
        is_refinement_slot = bool(second) and second not in (
            _PLAN_REVIEW_ACTIONS | _TOOL_APPROVAL_ACTIONS
        )
        if is_refinement_slot:
            return f"{prefix}: {first} — {second}"
        if not second:
            return f"{prefix}: {first}"
    if len(non_empty) == 1:
        return non_empty[0]
    return " | ".join(f"A{i + 1}: {a}" for i, a in enumerate(answers) if str(a).strip())
