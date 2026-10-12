"""TypeSafe-backed scenario classification with LLM fallback.

`classify_scenario_typesafe` picks one of the built-in scenario styles for
synthesis generation using a `Choice` question — a categorical judgment that
the TypeSafe model answers without an LLM round trip.

When the heuristic fast-path (in `scenario_classifier._heuristic_classify`)
is inconclusive, this module is consulted before falling back to the
scratchpad self-classification LLM path. A trusted verdict returns a
`ScenarioClassification` with the chosen scenario and empty `sections` (the
synthesis model still invents the outline). An untrusted or unavailable
verdict returns `None` so the caller activates scratchpad mode unchanged.

This is a fail-safe enhancement: the scratchpad path is the existing
default, so suppressing it (returning a non-`None` result) requires
`min_confidence` only — the same direction as the fail-safe.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from soothe.sloop.clarification.typesafe_client import (
    build_typesafe_classifier,
    classifier_config,
    verdict_trusted,
)
from soothe.sloop.engine.completion.scenario_classifier import (
    _SCENARIO_DESCRIPTIONS,
    ScenarioClassification,
)
from soothe.utils.text import truncate_text

if TYPE_CHECKING:
    from soothe.config import SootheConfig

logger = logging.getLogger(__name__)

_QUESTION_ID = "scenario"

_GOAL_MAX_CHARS = 600
_EXECUTION_SUMMARY_MAX_CHARS = 800

_INSTRUCTIONS = (
    "Which report scenario best matches this goal and its execution pattern? "
    "Pick the scenario whose description most closely fits the work done."
)


async def classify_scenario_typesafe(
    *,
    goal: str,
    intent_type: str,
    task_complexity: str | None,
    execution_summary: dict[str, Any],
    soothe_config: SootheConfig | None = None,
) -> ScenarioClassification | None:
    """Classify a synthesis scenario via TypeSafe, or ``None`` to fall back.

    Args:
        goal: Original user goal text.
        intent_type: Intent type label (e.g. ``"agentic"``).
        task_complexity: Intake complexity label, when known.
        execution_summary: Execution metadata from ``_extract_execution_summary``.
        soothe_config: ``SootheConfig`` providing the classifier provider and
            decision parameters (nano ``classifier`` block).

    Returns:
        A ``ScenarioClassification`` when a trusted verdict is available;
        ``None`` when the classifier is disabled or unavailable, the verdict
        is untrusted (distribution shift / near-tie), or the chosen label is
        not a recognised scenario. Callers fall back to scratchpad mode on
        ``None``.
    """
    cfg = classifier_config(soothe_config)
    if cfg is None or not cfg.enabled:
        return None

    from langchain_typesafe import Choice
    from langchain_typesafe.types import ClassifierRequest

    questions = {_QUESTION_ID: Choice(instructions=_INSTRUCTIONS, criteria=_SCENARIO_DESCRIPTIONS)}
    try:
        classifier = build_typesafe_classifier(soothe_config)
    except Exception as exc:  # noqa: BLE001 — never block on the classifier
        logger.info("[scenario] typesafe backend unavailable (%s); using scratchpad", exc)
        return None
    if classifier is None:
        logger.info("[scenario] typesafe backend unavailable; using scratchpad")
        return None

    state: dict[str, Any] = {
        "goal": truncate_text(goal.strip(), limit=_GOAL_MAX_CHARS),
        "intent_type": intent_type,
        "execution_summary": truncate_text(
            str(execution_summary), limit=_EXECUTION_SUMMARY_MAX_CHARS
        ),
    }
    if task_complexity:
        state["task_complexity"] = task_complexity

    try:
        response = await classifier.ainvoke(ClassifierRequest(state=state, questions=questions))
    except Exception as exc:  # noqa: BLE001 — never block on the classifier
        logger.warning("[scenario] typesafe unavailable (%s); using scratchpad", exc)
        return None

    answers = getattr(response, "answers", {}) or {}
    answer = answers.get(_QUESTION_ID)
    if answer is None:
        logger.info("[scenario] typesafe returned no answer; using scratchpad")
        return None

    trusted, reason = verdict_trusted(cfg, answer)
    if not trusted:
        logger.info("[scenario] verdict untrusted (%s); using scratchpad", reason)
        return None

    choice = str(getattr(answer, "choice", "") or "")
    confidence = getattr(answer, "confidence", None)
    if choice not in _SCENARIO_DESCRIPTIONS:
        logger.info("[scenario] typesafe returned unknown scenario %r; using scratchpad", choice)
        return None

    # Suppressing the scratchpad self-classification is a work-suppression
    # direction — apply the asymmetric suppression bar so a marginal verdict
    # does not skip the LLM's own judgment.
    suppress_bar = float(cfg.suppress_min_confidence)
    if confidence is not None and confidence < suppress_bar:
        logger.info(
            "[scenario] verdict below suppression bar (conf=%s < %s); using scratchpad",
            confidence,
            suppress_bar,
        )
        return None

    logger.info(
        "[scenario] typesafe: scenario=%s conf=%s",
        choice,
        confidence,
    )
    return ScenarioClassification(
        scenario=choice,
        sections=[],
        contextual_focus=[f"Summarize key findings for: {goal[:120]}"],
        evidence_emphasis=(
            "Group evidence by concern or outcome in bullets/tables; "
            "do not replay turns chronologically"
        ),
    )


__all__ = ["classify_scenario_typesafe"]
