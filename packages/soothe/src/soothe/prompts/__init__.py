"""Host prompt construction."""

from soothe_nano.prompts.context_xml import ContextXml
from soothe_nano.prompts.fragments import (
    ASSISTANT_IDENTITY_FRAGMENT,
    DEFAULT_SYSTEM_PROMPT_BODY_FRAGMENT,
    SIMPLE_SYSTEM_PROMPT_FRAGMENT,
)
from soothe_nano.prompts.identity import PromptIdentity
from soothe_nano.prompts.project_instructions import ProjectInstructions
from soothe_nano.prompts.system_templates import SystemTemplates

from .fragments import (
    APPROVED_PLAN_EXECUTE_HINT,
    ASK_MODE_ADDENDUM,
    COVERAGE_VERDICT_TOOL_DESCRIPTION,
    DECOMPOSE_TASK_TOOL_DESCRIPTION,
    EVAL_DECISION_SYSTEM,
    EVAL_POLICY_SYSTEM_ADDENDUM,
    PARALLEL_NUDGE_ADDENDUM,
    PLAN_MODE_ADDENDUM,
    THREAD_POLICY_SYSTEM_ADDENDUM,
    THREAD_USER_HINT_CHILD_FRAGMENT,
    THREAD_USER_HINT_ROOT_FRAGMENT,
    WESTWORLD_FANOUT_ADDENDUM,
    WRITE_TODOS_TOOL_DESCRIPTION,
)


def user_finish_or_split_hint_lines(*, is_dag_root: bool) -> list[str]:
    """One-line user reminder (policy lives in system + tool schemas)."""
    if is_dag_root:
        return [THREAD_USER_HINT_ROOT_FRAGMENT]
    return [THREAD_USER_HINT_CHILD_FRAGMENT]


__all__ = [
    "APPROVED_PLAN_EXECUTE_HINT",
    "ASK_MODE_ADDENDUM",
    "ASSISTANT_IDENTITY_FRAGMENT",
    "COVERAGE_VERDICT_TOOL_DESCRIPTION",
    "ContextXml",
    "DECOMPOSE_TASK_TOOL_DESCRIPTION",
    "DEFAULT_SYSTEM_PROMPT_BODY_FRAGMENT",
    "EVAL_DECISION_SYSTEM",
    "EVAL_POLICY_SYSTEM_ADDENDUM",
    "PARALLEL_NUDGE_ADDENDUM",
    "PLAN_MODE_ADDENDUM",
    "ProjectInstructions",
    "PromptIdentity",
    "SIMPLE_SYSTEM_PROMPT_FRAGMENT",
    "SystemTemplates",
    "THREAD_POLICY_SYSTEM_ADDENDUM",
    "WESTWORLD_FANOUT_ADDENDUM",
    "WRITE_TODOS_TOOL_DESCRIPTION",
    "user_finish_or_split_hint_lines",
]
