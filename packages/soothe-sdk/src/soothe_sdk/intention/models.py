"""Shared routing classification models for CoreAgent execution paths."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class TaskComplexity(StrEnum):
    """Unified task complexity levels for routing decisions.

    - minimal: direct factual reply; no tools needed
    - simple: one cohesive deliverable in one area/module (single leaf)
    - complex: 2+ independent areas/modules that could run in parallel, or a
      multi-phase sequence needing separate context per phase
    """

    MINIMAL = "minimal"
    SIMPLE = "simple"
    COMPLEX = "complex"


class RoutingClassification(BaseModel):
    """Routing complexity classification for execution path selection.

    Args:
        task_complexity: Routing complexity level.
        preferred_subagent: Wire or host hint for which subagent to prefer.
        routing_hint: Routing strategy hint.
    """

    task_complexity: TaskComplexity = Field(
        description="Routing complexity: minimal (no tools), simple, or complex"
    )
    preferred_subagent: str | None = Field(
        default=None,
        description="Preferred subagent name when the host requests a specialist",
    )
    routing_hint: str | None = Field(
        default=None,
        description="Routing strategy hint: 'subagent', 'tool', 'llm_only', etc.",
    )


__all__ = ["RoutingClassification", "TaskComplexity"]
