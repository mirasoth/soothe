"""Project StrangeLoop state into user-safe synthesis context."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from langchain_core.messages import BaseMessage

from soothe.prompts.user_message import (
    _goal_text,
    flatten_user_message_content,
)
from soothe.sloop.engine.completion.scenario_classifier import (
    ScenarioClassification,
    format_hint_for_scenario,
)

if TYPE_CHECKING:
    from soothe.config.models import PlanPromptLedgerConfig
    from soothe.sloop.state.schemas import LoopState

_ANALYSIS_BLOCK_RE = re.compile(r"<analysis>.*?</analysis>", re.DOTALL | re.IGNORECASE)


def strip_analysis_scratchpad(text: str) -> str:
    """Remove `<analysis>...</analysis>` scratchpad blocks from model output.

    The scratchpad is internal reasoning emitted before the report body when
    scratchpad mode is active. This strips it so downstream consumers only
    see the finished report.

    Args:
        text: Raw model output that may contain one or more analysis blocks.

    Returns:
        Text with analysis blocks removed and surrounding whitespace collapsed.
    """
    if not text:
        return text
    stripped = _ANALYSIS_BLOCK_RE.sub("", text)
    return stripped.strip()


def normalize_user_query(goal: str | None) -> str:
    """Normalize stored goal text for the user-facing synthesis request."""
    text = _goal_text(goal)
    if text == "No goal specified":
        return "No request specified"
    return text


def flatten_execute_human_content(content: str) -> str:
    """Extract task-focused text from an execute-step human envelope.

    Delegates to `flatten_user_message_content` for scenario-based
    `EXECUTION TASK:` / `GOAL:` sections.
    """
    return flatten_user_message_content(content)


def render_synthesis_system_prompt(
    classification: ScenarioClassification,
    *,
    user_goal: str,
    workspace: str | None = None,
    agent_instructions_max_chars: int = 8000,
    response_language: object | None = None,
    scratchpad_mode: bool = False,
) -> str:
    """Render system instructions from the synthesis template (no orchestration terms).

    When `scratchpad_mode` is True, the template instructs the model to
    self-classify inside an `<analysis>` block before writing the report
    body, and scenario sections/focus areas/evidence emphasis become
    optional reference rather than fixed directives. The caller is
    responsible for stripping the analysis block from the final output
    via `strip_analysis_scratchpad`.

    Args:
        classification: Scenario classification with style, sections, and focus.
        user_goal: Normalized goal text for the user-facing report.
        workspace: Optional workspace path for agent instructions inlining.
        agent_instructions_max_chars: Budget for inlined AGENTS.md content.
        response_language: Optional language hint object for output language.
        scratchpad_mode: When True, enable `<analysis>` self-classification.

    Returns:
        Rendered system prompt string.
    """
    from soothe.prompts import (
        build_response_language_hint,
        build_timestamp_xml_footer,
        load_agent_instructions,
    )
    from soothe.prompts.loader import load_prompt_fragment

    template = load_prompt_fragment("instructions/synthesis_report_system.xml")
    focus_items = "\n".join(f"- {item}" for item in classification.contextual_focus)
    parts = [
        template.render(
            scenario=classification.scenario,
            sections=classification.sections,
            contextual_focus=focus_items,
            evidence_emphasis=classification.evidence_emphasis,
            format_hint=format_hint_for_scenario(classification.scenario),
            user_goal=user_goal,
            scratchpad_mode=scratchpad_mode,
        ),
        build_response_language_hint(response_language),
    ]
    if workspace:
        block = load_agent_instructions(
            workspace,
            headline_max_chars=agent_instructions_max_chars,
        )
        if block:
            parts.append(block)
    parts.append(build_timestamp_xml_footer())
    return "\n\n".join(parts)


def build_synthesis_messages(
    state: LoopState,
    classification: ScenarioClassification,
    *,
    user_query: str | None = None,
    max_chars: int,
    ledger_cfg: PlanPromptLedgerConfig | None = None,
    agent_instructions_max_chars: int = 8000,
) -> list[BaseMessage]:
    """Assemble system + execute ledger + TASK human for goal-completion synthesis.

    Delegates to :class:`GraphPromptWrapper` for centralized projection and
    system-prompt assembly so synthesis shares the same pipeline as planner
    calls.
    """
    from soothe.prompts.graph_wrapper import GraphPromptWrapper

    wrapper = GraphPromptWrapper()
    return wrapper.build_synthesis_messages(
        state=state,
        classification=classification,
        user_query=user_query,
        max_chars=max_chars,
        ledger_cfg=ledger_cfg,
        agent_instructions_max_chars=agent_instructions_max_chars,
    )


__all__ = [
    "build_synthesis_messages",
    "flatten_execute_human_content",
    "normalize_user_query",
    "render_synthesis_system_prompt",
    "strip_analysis_scratchpad",
]
