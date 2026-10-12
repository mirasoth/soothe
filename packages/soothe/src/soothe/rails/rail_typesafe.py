"""TypeSafe-backed LoopRail auto-pick with LLM fallback.

`pick_rail_typesafe` selects one rail from the catalog (or abstains) using
a `Choice` question — a categorical judgment that the TypeSafe model
answers without an LLM round trip.

When the TypeSafe verdict is trusted and the rail id is in the allowed
candidate set, the caller skips the LLM `RailAutoPicker.pick()` call
entirely. An untrusted or unavailable verdict returns `None` so the
caller falls through to the LLM path unchanged.

The decision is asymmetric:

- `abstain` (no rail fits) **suppresses** rail selection, so it
  additionally requires `suppress_min_confidence`.
- Picking a specific rail is fail-safe-adjacent (the deterministic
  fallback ladder runs when no rail is picked), so a trusted verdict is
  enough.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from soothe.rails.catalog import RailDefinition
from soothe.sloop.clarification.typesafe_client import (
    build_typesafe_classifier,
    classifier_config,
    verdict_trusted,
)
from soothe.utils.text import truncate_text

if TYPE_CHECKING:
    from soothe.config import SootheConfig

logger = logging.getLogger(__name__)

_QUESTION_ID = "rail_pick"

_ABSTAIN = "abstain"

_DESCRIPTION_MAX_CHARS = 600
_SUMMARY_MAX_CHARS = 200

_INSTRUCTIONS = (
    "Which LoopRail best fits this job, or should no specialized rail apply? "
    "Match the job intent to each candidate's applies_when (summary is context). "
    "Prefer the most specific fit. Choose abstain if no rail fits better than "
    "opportunistic Autopilot."
)


def _build_criteria(candidates: Sequence[RailDefinition]) -> dict[str, str]:
    """Build Choice criteria from rail candidates plus an abstain option."""
    criteria: dict[str, str] = {_ABSTAIN: "No specialized rail fits better than Autopilot."}
    for rail in candidates:
        criteria[rail.id] = truncate_text(
            f"{rail.summary}. Applies when: {rail.applies_when}",
            limit=_SUMMARY_MAX_CHARS,
        )
    return criteria


async def pick_rail_typesafe(
    *,
    description: str,
    candidates: Sequence[RailDefinition],
    soothe_config: SootheConfig | None = None,
) -> str | None:
    """Pick a rail id via TypeSafe, or ``None`` to fall back to the LLM.

    Args:
        description: Job submit text for rail matching.
        candidates: Filtered, sorted rail definitions eligible for auto-pick.
        soothe_config: ``SootheConfig`` providing the classifier provider and
            decision parameters (nano ``classifier`` block).

    Returns:
        A rail id string when a trusted verdict selects a candidate;
        ``"abstain"`` when a trusted verdict at the suppression bar says
        no rail fits (caller treats this as a high-confidence null pick);
        ``None`` when the classifier is disabled/unavailable, the verdict
        is untrusted, or the chosen label is not in the candidate set —
        callers fall back to the LLM path on ``None``.
    """
    if not candidates:
        return None

    cfg = classifier_config(soothe_config)
    if cfg is None or not cfg.enabled:
        return None

    from langchain_typesafe import Choice
    from langchain_typesafe.types import ClassifierRequest

    criteria = _build_criteria(candidates)
    questions = {_QUESTION_ID: Choice(instructions=_INSTRUCTIONS, criteria=criteria)}
    try:
        classifier = build_typesafe_classifier(soothe_config)
    except Exception as exc:  # noqa: BLE001 — never block on the classifier
        logger.info("[rail_pick] typesafe backend unavailable (%s); using LLM", exc)
        return None
    if classifier is None:
        logger.info("[rail_pick] typesafe backend unavailable; using LLM")
        return None

    state: dict[str, Any] = {
        "job_description": truncate_text(description.strip(), limit=_DESCRIPTION_MAX_CHARS),
    }

    try:
        response = await classifier.ainvoke(ClassifierRequest(state=state, questions=questions))
    except Exception as exc:  # noqa: BLE001 — never block on the classifier
        logger.warning("[rail_pick] typesafe unavailable (%s); using LLM", exc)
        return None

    answers = getattr(response, "answers", {}) or {}
    answer = answers.get(_QUESTION_ID)
    if answer is None:
        logger.info("[rail_pick] typesafe returned no answer; using LLM")
        return None

    trusted, reason = verdict_trusted(cfg, answer)
    if not trusted:
        logger.info("[rail_pick] verdict untrusted (%s); using LLM", reason)
        return None

    choice = str(getattr(answer, "choice", "") or "")
    confidence = getattr(answer, "confidence", None)
    allowed = {c.id for c in candidates}

    if choice == _ABSTAIN:
        # Abstaining suppresses rail selection — apply the suppression bar.
        suppress_bar = float(cfg.suppress_min_confidence)
        if confidence is not None and confidence < suppress_bar:
            logger.info(
                "[rail_pick] abstain below suppression bar (conf=%s < %s); using LLM",
                confidence,
                suppress_bar,
            )
            return None
        logger.debug("[rail_pick] typesafe: abstain (conf=%s)", confidence)
        return _ABSTAIN

    if choice in allowed:
        logger.debug("[rail_pick] typesafe: rail=%s (conf=%s)", choice, confidence)
        return choice

    logger.info("[rail_pick] typesafe returned unknown rail %r; using LLM", choice)
    return None


__all__ = ["pick_rail_typesafe"]
