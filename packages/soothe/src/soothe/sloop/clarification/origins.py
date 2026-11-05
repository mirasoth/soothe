"""Clarification origin constants."""

from __future__ import annotations

from typing import Final, Literal

from soothe.sloop.orchestrator.stations import EXECUTE

# --- Live StrangeLoop / host origins ----------------------------------------

ORIGIN_EXECUTE: Final = EXECUTE
"""CoreAgent execute-step `ask_user` clarification."""

ORIGIN_PLAN_MODE_REVIEW: Final = "plan_mode_review"
"""Human review gate after plan-mode draft (approve / reject / refine)."""

ORIGIN_RAIL_PAUSE: Final = "rail_pause"
"""LoopRail `pause_for_user` human gate; host-side Veritas only."""

ORIGIN_TOOL_APPROVAL: Final = "tool_approval"
"""Deepagents `HumanInTheLoopMiddleware` tool-action approval gate.

The executor captures `action_requests` interrupts here instead of
auto-approving them. The request resumes at `EXECUTE` (the step
containing the tool call)."""

ORIGIN_COVERAGE_GATE: Final = "coverage_gate"
"""Eval `coverage_verdict` user-decision gate.

When an Eval step reports the goal blocked on a user decision, the verdict
routes straight to `await_clarification` via the relay — no execution step
is materialized. Veritas arbitrates it like `execute` (IG-LOOP-015)."""

ClarificationOrigin = Literal[
    "execute",
    "plan_mode_review",
    "rail_pause",
    "tool_approval",
    "coverage_gate",
]

CLARIFICATION_ORIGINS: frozenset[str] = frozenset(
    {
        ORIGIN_EXECUTE,
        ORIGIN_PLAN_MODE_REVIEW,
        ORIGIN_RAIL_PAUSE,
        ORIGIN_TOOL_APPROVAL,
        ORIGIN_COVERAGE_GATE,
    }
)

PLAN_MODE_REVIEW_INTERRUPT_PREFIX: Final = "plan-mode-review:"
"""Interrupt prefix for plan-mode review clarifications."""


__all__ = [
    "CLARIFICATION_ORIGINS",
    "ClarificationOrigin",
    "ORIGIN_COVERAGE_GATE",
    "ORIGIN_EXECUTE",
    "ORIGIN_PLAN_MODE_REVIEW",
    "ORIGIN_RAIL_PAUSE",
    "ORIGIN_TOOL_APPROVAL",
    "PLAN_MODE_REVIEW_INTERRUPT_PREFIX",
]
