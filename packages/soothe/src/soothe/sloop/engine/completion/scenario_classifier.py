"""Scenario classifier for synthesis generation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, model_validator

from soothe.prompts.fragments import (
    SCENARIO_CLASSIFIER_SYSTEM_FRAGMENT,
    SCENARIO_CLASSIFIER_USER_FRAGMENT,
)

if TYPE_CHECKING:
    from soothe.sloop.state.schemas import LoopState

from soothe.config.models import ScenarioRulesConfig

logger = logging.getLogger(__name__)

_DEFAULT_SCENARIO_RULES = ScenarioRulesConfig()

# Built-in scenario style names → short descriptions.
# Used by the prompt fragments for the scenario list reference.
_SCENARIO_DESCRIPTIONS: dict[str, str] = {
    "code_architecture_design": "System/module structure analysis",
    "code_implementation_design": "Concrete implementation patterns and examples",
    "research_synthesis": "Multi-source information gathering and findings",
    "travel_activity_plan": "Structured planning for trips, events, activities",
    "tutorial_guide": "Step-by-step instructional content",
    "analysis_report": "Data/metrics/trends analysis with recommendations",
    "investigation_summary": "Problem/troubleshooting investigation process",
    "decision_analysis": "Options comparison with trade-offs",
    "content_draft": "Blog, documentation, proposal, email drafts",
    "general_summary": "Simple summarization fallback",
}

# Per-scenario CLI layout hints for goal-completion synthesis.
# Layout examples only — not outline authority. Markdown tables, bullets, and
# mermaid fences render in the Rich TUI (terminal diagram expand).
SCENARIO_FORMAT_HINTS: dict[str, str] = {
    "code_architecture_design": (
        "Bullets/tables first. Component inventory: GFM table (Name | Role | Location). "
        "Key findings and recommendations: bullet lists. "
        "Main request/data/control flow: ```mermaid flowchart when evidence supports it."
    ),
    "code_implementation_design": (
        "Bullets/tables first. APIs, signatures, or config keys: GFM table. "
        "Patterns, usage, and caveats: bullet lists. "
        "Call or deployment sequence: ```mermaid sequenceDiagram when helpful."
    ),
    "research_synthesis": (
        "Bullets/tables first. Source comparison: GFM table (Source | Finding | Confidence). "
        "Discoveries and conclusions: bullet lists."
    ),
    "travel_activity_plan": (
        "Bullets/tables first. Itinerary: GFM table (Day/Time | Activity | Location | Notes). "
        "Tips and recommendations: bullet lists."
    ),
    "tutorial_guide": (
        "Bullets/tables first. Prerequisites or checklist items: GFM table or bullets. "
        "Procedure: numbered or bullet steps. "
        "Optional overview flow: ```mermaid flowchart."
    ),
    "analysis_report": (
        "Bullets/tables first. Metrics and measurements: GFM table (Metric | Value | Notes). "
        "Trends and recommendations: bullet lists."
    ),
    "investigation_summary": (
        "Bullets/tables first. Symptom/cause matrix: GFM table (Symptom | Cause | Status). "
        "Investigation steps and resolution: bullet lists. "
        "Repro or request path: ```mermaid sequenceDiagram when evidence supports it."
    ),
    "decision_analysis": (
        "Bullets/tables first. Options comparison: GFM table (Option | Pros | Cons | Fit). "
        "Recommendation rationale: bullet lists."
    ),
    "content_draft": (
        "Short paragraphs OK for narrative body; still use ## outline and bullets for lists."
    ),
    "general_summary": (
        "Bullets first: one-line outcome under Summary, then 3–5 Key Points as bullets "
        "(use a small GFM table if comparing a few metrics)."
    ),
    "custom": (
        "Bullets/tables first. GFM tables for comparisons or inventories; "
        "bullets for lists of 3+ items; ```mermaid when a diagram clarifies structure."
    ),
}


def format_hint_for_scenario(scenario: str) -> str:
    """Return CLI layout hint for a synthesis scenario name."""
    return SCENARIO_FORMAT_HINTS.get(scenario, SCENARIO_FORMAT_HINTS["custom"])


class ScenarioClassification(BaseModel):
    """Scenario classification result for synthesis generation.

    Produced by ScenarioClassifier from goal + intent + execution pattern.
    Guides Phase 2 synthesis with style + optional outline suggestions + focus.

    Empty `sections` means Phase 2 invents the outline (heuristic fast-path).
    Non-empty `sections` are soft suggestions, not required headings.
    """

    scenario: str = Field(description="Built-in scenario name or 'custom' for novel cases")
    sections: list[str] = Field(
        default_factory=list,
        description=(
            "Suggested ## section titles for the report outline (3–7 when set; "
            "empty = Phase 2 designs the outline)"
        ),
    )
    contextual_focus: list[str] = Field(
        description="2-3 specific focus areas for this goal (not generic)"
    )
    evidence_emphasis: str = Field(description="How to use execution evidence in synthesis")

    @model_validator(mode="after")
    def validate_sections(self) -> ScenarioClassification:
        """Normalize section titles; empty list is allowed (Phase 2 invents outline)."""
        self.sections = [s.strip() for s in self.sections if isinstance(s, str) and s.strip()]
        return self


def _extract_execution_summary(state: LoopState) -> dict:
    """Extract execution metadata from state step results.

    Args:
        state: Loop state with step_results.

    Returns:
        Execution summary dict with total_steps, successful_steps,
        step_types, tools_used, evidence_volume.
    """
    total_steps = len(state.step_results)
    successful_steps = sum(1 for r in state.step_results if r.success)

    step_types = []
    tools_used = []
    for result in state.step_results:
        outcome_type = result.outcome.get("type", "unknown")
        step_types.append(outcome_type)

        # Extract tools from outcome metadata
        tool_name = result.outcome.get("tool_name")
        if tool_name:
            tools_used.append(tool_name)

    # Rough character scale for routing (truncate=True avoids duplicating ledger-scale blobs).
    evidence_volume = 0
    for result in state.step_results:
        if result.success:
            evidence_volume += len(result.to_evidence_string(truncate=True))

    return {
        "total_steps": total_steps,
        "successful_steps": successful_steps,
        "step_types": step_types,
        "tools_used": tools_used,
        "evidence_volume": evidence_volume,
    }


def _build_classifier_system_prompt() -> str:
    """Build system prompt with task instructions, scenario list, and output schema."""
    scenarios_list = "\n".join(
        f"{i + 1}. {name} - {desc}" for i, (name, desc) in enumerate(_SCENARIO_DESCRIPTIONS.items())
    )
    return SCENARIO_CLASSIFIER_SYSTEM_FRAGMENT.format(scenarios_list=scenarios_list)


def _build_classifier_user_prompt(
    goal: str,
    intent_type: str,
    task_complexity: str,
    execution_summary: dict,
) -> str:
    """Build per-request user prompt with goal, intent, and execution summary."""
    return SCENARIO_CLASSIFIER_USER_FRAGMENT.format(
        goal=goal,
        intent_type=intent_type,
        task_complexity=task_complexity,
        total_steps=execution_summary["total_steps"],
        successful_steps=execution_summary["successful_steps"],
        step_types=execution_summary["step_types"],
        tools_used=execution_summary["tools_used"],
        evidence_volume=execution_summary["evidence_volume"],
    )


def _heuristic_classify(
    goal: str,
    intent_type: str,
    execution_summary: dict,
    *,
    scenario_rules: ScenarioRulesConfig | None = None,
) -> ScenarioClassification | None:
    """Config-driven fast-path for obvious scenario classification.

    Sets scenario style + focus/emphasis only. Leaves `sections` empty so
    the synthesis model invents the report outline (builtins are not
    outline authority). Returns None when no rule matches — the caller
    then activates scratchpad mode for the model to self-classify.
    """
    rules = scenario_rules or _DEFAULT_SCENARIO_RULES
    total_steps = execution_summary["total_steps"]
    successful_steps = execution_summary["successful_steps"]
    step_types = execution_summary["step_types"]
    evidence_volume = execution_summary["evidence_volume"]

    if rules.skip_llm_when_single_step and total_steps <= 1:
        return ScenarioClassification(
            scenario="general_summary",
            sections=[],
            contextual_focus=[f"Summarize result for: {goal[:120]}"],
            evidence_emphasis=(
                "Present the single-step outcome as bullets (and a small table if comparing "
                "a few values); do not narrate the turn"
            ),
        )

    if rules.skip_llm_when_all_failed and successful_steps == 0 and total_steps > 0:
        return ScenarioClassification(
            scenario="investigation_summary",
            sections=[],
            contextual_focus=["Identify root cause of failures", "Summarize troubleshooting steps"],
            evidence_emphasis=(
                "Group error patterns in a Symptom|Cause|Status table where possible; "
                "resolution as bullets"
            ),
        )

    has_tool = any(t not in ("unknown", "llm_call") for t in step_types)
    if total_steps >= rules.high_step_count_threshold and has_tool:
        return ScenarioClassification(
            scenario="analysis_report",
            sections=[],
            contextual_focus=[
                f"Aggregate findings across {total_steps} steps",
                "Highlight key metrics and outcomes",
            ],
            evidence_emphasis=(
                "Summarize tool outputs by concern in tables/bullets, not chronologically"
            ),
        )

    if evidence_volume < rules.low_evidence_volume_threshold and intent_type == "agentic":
        return ScenarioClassification(
            scenario="general_summary",
            sections=[],
            contextual_focus=[f"Summarize key findings for: {goal[:120]}"],
            evidence_emphasis="Present key outcomes as a short bullet list",
        )

    # No heuristic matched — caller activates scratchpad self-classification.
    return None
