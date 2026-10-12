"""TypeSafe-backed finish-vs-split decision for decompose_task injection.

`decide_finish_vs_split_typesafe` answers whether the current step should
finish in-thread (decompose_task stripped from the tool surface) or keep
the decompose option (fail-safe). A `Choice` question can answer this
without an LLM round trip.

The decision is asymmetric:

- `split` (keep the tool) matches the fail-safe default — a trusted verdict
  is enough.
- `finish` (strip the tool) **suppresses** the LLM's ability to decompose.
  Acting on a wrong suppression forces work into one thread that should
  have been parallelized, so that direction additionally requires
  `suppress_min_confidence`.

Anything unavailable, untrusted, or near-tie returns `None` and the caller
keeps the decompose tool unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from soothe.sloop.clarification.typesafe_client import (
    build_typesafe_classifier,
    classifier_config,
    verdict_trusted,
)
from soothe.utils.text import truncate_text

if TYPE_CHECKING:
    from soothe.config import SootheConfig

logger = logging.getLogger(__name__)

_QUESTION_ID = "finish_split"

_FINISH = "finish"
_SPLIT = "split"

_STEP_DESC_MAX_CHARS = 800
_GOAL_MAX_CHARS = 600

_INSTRUCTIONS = (
    "Should this step finish in the current thread, or is it complex enough "
    "to warrant splitting into parallel subtasks? A step that is a single "
    "focused deliverable should finish; one spanning multiple independent "
    "areas or phases should split."
)

_CRITERIA: dict[str, str] = {
    _FINISH: (
        "The step is a single cohesive deliverable that can be completed "
        "in one thread — one focused area, no independent parallel workstreams."
    ),
    _SPLIT: (
        "The step spans multiple independent areas or phases that would "
        "benefit from parallel execution or independent retry per subtask."
    ),
}


async def decide_finish_vs_split_typesafe(
    *,
    step_description: str,
    user_goal: str,
    intake_label: str | None = None,
    soothe_config: SootheConfig | None = None,
) -> str | None:
    """Decide whether to strip decompose_task via TypeSafe.

    Args:
        step_description: The step's full description text.
        user_goal: Original user goal text.
        intake_label: Intake complexity label, when known.
        soothe_config: ``SootheConfig`` providing the classifier provider and
            decision parameters (nano ``classifier`` block).

    Returns:
        ``"finish"`` when JEV says finish-in-thread with trusted confidence
        and suppression bar met; ``"split"`` when JEV says split with trusted
        confidence; ``None`` when the classifier is disabled/unavailable, the
        verdict is untrusted, or the suppression bar is not met — callers
        keep the decompose tool unchanged on ``None``.
    """
    cfg = classifier_config(soothe_config)
    if cfg is None or not cfg.enabled:
        return None

    from langchain_typesafe import Choice
    from langchain_typesafe.types import ClassifierRequest

    questions = {_QUESTION_ID: Choice(instructions=_INSTRUCTIONS, criteria=_CRITERIA)}
    try:
        classifier = build_typesafe_classifier(soothe_config)
    except Exception as exc:  # noqa: BLE001 — never block on the classifier
        logger.info("[decompose] typesafe backend unavailable (%s); keeping tool", exc)
        return None
    if classifier is None:
        logger.info("[decompose] typesafe backend unavailable; keeping tool")
        return None

    state: dict[str, Any] = {
        "step_description": truncate_text(step_description.strip(), limit=_STEP_DESC_MAX_CHARS),
        "goal": truncate_text(user_goal.strip(), limit=_GOAL_MAX_CHARS),
    }
    if intake_label:
        state["intake_label"] = str(intake_label)

    try:
        response = await classifier.ainvoke(ClassifierRequest(state=state, questions=questions))
    except Exception as exc:  # noqa: BLE001 — never block on the classifier
        logger.warning("[decompose] typesafe unavailable (%s); keeping tool", exc)
        return None

    answers = getattr(response, "answers", {}) or {}
    answer = answers.get(_QUESTION_ID)
    if answer is None:
        logger.info("[decompose] typesafe returned no answer; keeping tool")
        return None

    trusted, reason = verdict_trusted(cfg, answer)
    if not trusted:
        logger.info("[decompose] verdict untrusted (%s); keeping tool", reason)
        return None

    choice = str(getattr(answer, "choice", "") or "")
    confidence = getattr(answer, "confidence", None)

    if choice == _SPLIT:
        # Same direction as the fail-safe — no extra bar needed.
        logger.debug("[decompose] typesafe: split (conf=%s)", confidence)
        return "split"

    if choice == _FINISH:
        # Suppressing decompose is a work-suppression direction — apply
        # the asymmetric suppression bar so a marginal verdict does not
        # strip the tool.
        suppress_bar = float(cfg.suppress_min_confidence)
        if confidence is not None and confidence < suppress_bar:
            logger.info(
                "[decompose] finish verdict below suppression bar (conf=%s < %s); keeping tool",
                confidence,
                suppress_bar,
            )
            return None
        logger.debug("[decompose] typesafe: finish (conf=%s)", confidence)
        return "finish"

    logger.info("[decompose] typesafe returned unknown choice %r; keeping tool", choice)
    return None


__all__ = ["decide_finish_vs_split_typesafe"]
