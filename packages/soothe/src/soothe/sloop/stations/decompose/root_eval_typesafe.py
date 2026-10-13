"""TypeSafe-backed root Eval re-audit decision.

`decide_reaudit_vs_finalize_typesafe` answers whether a completed Eval that
lacked a binding `complete` verdict should be re-audited or finalized.

The decision is asymmetric:

- `reaudit` (run another Eval round) matches the fail-safe default, so a
  trusted verdict is enough.
- `finalize` **suppresses** the re-audit, so that direction additionally
  requires `suppress_min_confidence`.

Anything unavailable, untrusted, or near-tie returns `None` and the caller
runs the existing unconditional re-audit unchanged.
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

_QUESTION_ID = "reaudit"

_REAUDIT = "reaudit"
_FINALIZE = "finalize"

_GOAL_MAX_CHARS = 1200
_HISTORY_MAX_CHARS = 4000

_INSTRUCTIONS = (
    "The latest coverage Eval completed without a binding complete verdict. "
    "Should we re-audit (run another Eval round) or finalize (accept the goal "
    "as complete based on the evidence shown)?"
)

_CRITERIA: dict[str, str] = {
    _REAUDIT: (
        "Coverage is uncertain or incomplete: the step history shows unverified "
        "work, missing deliverables, or evidence gaps that another Eval round "
        "should address."
    ),
    _FINALIZE: (
        "Despite the missing structured verdict, the step history and close "
        "reports show the goal is fully achieved — re-auditing would not add "
        "new information."
    ),
}


async def decide_reaudit_vs_finalize_typesafe(
    *,
    user_goal: str,
    step_history_table: str,
    soothe_config: SootheConfig | None = None,
) -> bool | None:
    """Decide whether to re-audit or finalize via TypeSafe.

    Args:
        user_goal: Original user goal text.
        step_history_table: Rendered step history (descriptions, expected
            outputs, close reports, outcomes).
        soothe_config: ``SootheConfig`` providing the classifier provider and
            decision parameters (nano ``classifier`` block).

    Returns:
        `True` to re-audit, `False` to finalize, or `None` when the
        classifier is disabled/unavailable, the verdict is untrusted, or
        the suppression bar is not met — the caller then runs the existing
        unconditional re-audit.
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
        logger.info("[root_eval] typesafe backend unavailable (%s); re-auditing", exc)
        return None
    if classifier is None:
        logger.info("[root_eval] typesafe backend unavailable; re-auditing")
        return None

    state: dict[str, Any] = {
        "goal": truncate_text(user_goal.strip(), limit=_GOAL_MAX_CHARS),
        "step_history": truncate_text(step_history_table, limit=_HISTORY_MAX_CHARS),
    }

    try:
        response = await classifier.ainvoke(ClassifierRequest(state=state, questions=questions))
    except Exception as exc:  # noqa: BLE001 — never block on the classifier
        logger.warning("[root_eval] typesafe unavailable (%s); re-auditing", exc)
        return None

    answers = getattr(response, "answers", {}) or {}
    answer = answers.get(_QUESTION_ID)
    if answer is None:
        logger.info("[root_eval] typesafe returned no answer; re-auditing")
        return None

    trusted, reason = verdict_trusted(cfg, answer)
    if not trusted:
        logger.info("[root_eval] verdict untrusted (%s); re-auditing", reason)
        return None

    choice = str(getattr(answer, "choice", "") or "")
    confidence = getattr(answer, "confidence", None)
    if choice == _REAUDIT:
        logger.debug("[root_eval] typesafe: re-audit (conf=%s)", confidence)
        return True
    if choice == _FINALIZE:
        suppress_bar = float(cfg.suppress_min_confidence)
        if confidence is not None and confidence < suppress_bar:
            logger.info(
                "[root_eval] finalize below suppression bar (conf=%s < %s); re-auditing",
                confidence,
                suppress_bar,
            )
            return None
        logger.debug("[root_eval] typesafe: finalize (conf=%s)", confidence)
        return False

    logger.info("[root_eval] typesafe returned unknown choice %r; re-auditing", choice)
    return None


__all__ = ["decide_reaudit_vs_finalize_typesafe"]
