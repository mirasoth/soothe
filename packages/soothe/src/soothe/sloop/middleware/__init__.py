"""Unified middleware package for host agent behavior overrides."""

from soothe.sloop.decompose.middleware import DecomposeTaskMiddleware
from soothe.sloop.eval.middleware import EvalStepMiddleware
from soothe.sloop.middleware.ask_user_gate import AskUserGateMiddleware
from soothe.sloop.middleware.ask_user_prompt import AskUserPromptMiddleware
from soothe.sloop.middleware.auto_mode import AutoModeMiddleware
from soothe.sloop.middleware.goal_step_guard import GoalStepGuardMiddleware
from soothe.sloop.middleware.gp_variant_guard import (
    GeneralPurposeVariantGuardMiddleware,
)
from soothe.sloop.middleware.intake_task_guard import IntakeOnlyTaskGuardMiddleware
from soothe.sloop.middleware.westworld import WestWorldMiddleware

__all__ = [
    "AskUserGateMiddleware",
    "AskUserPromptMiddleware",
    "AutoModeMiddleware",
    "DecomposeTaskMiddleware",
    "EvalStepMiddleware",
    "GeneralPurposeVariantGuardMiddleware",
    "GoalStepGuardMiddleware",
    "IntakeOnlyTaskGuardMiddleware",
    "WestWorldMiddleware",
]
