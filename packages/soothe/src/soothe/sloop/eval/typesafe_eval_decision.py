"""TypeSafe-backed goal-coverage decision with LLM fallback.

`decide_eval_required` asks whether a coverage Eval step should audit the
goal. That is a categorical judgment over the step history and close
reports, so a `Choice` question can answer it without an LLM round trip.

The decision is asymmetric, and the gate reflects it:

- `eval` (run the audit) matches the fail-safe default, so a trusted verdict
  is enough.
- `no_eval` **suppresses** work. Acting on a wrong suppression ships an
  unaudited goal, so that direction additionally requires
  `suppress_min_confidence`.

Anything unavailable, untrusted, or near-tie returns `None` and the caller
runs the existing LLM decision unchanged (whose own fail-safe is
`should_run_eval=True`).
"""

from __future__ import annotations

import logging
from typing import Any

from soothe.sloop.clarification.typesafe_client import (
    build_typesafe_classifier,
    classifier_config,
    verdict_trusted,
)
from soothe.utils.text import truncate_text

logger = logging.getLogger(__name__)

_QUESTION_ID = "coverage"

_EVAL = "eval"
_NO_EVAL = "no_eval"

_GOAL_MAX_CHARS = 1200
_HISTORY_MAX_CHARS = 4000

_INSTRUCTIONS = (
    "Decide whether a coverage audit of the goal is warranted, given the "
    "steps already executed and their reported outcomes."
)

_CRITERIA: dict[str, str] = {
    _NO_EVAL: (
        "The executed steps fully cover the goal: the close reports and "
        "outcomes show the deliverable is produced and nothing material is "
        "left unaddressed."
    ),
    _EVAL: (
        "Coverage is uncertain or incomplete: something in the goal is "
        "unverified, partially done, failed, or the evidence is missing."
    ),
}


async def decide_eval_coverage_typesafe(
    *,
    user_goal: str,
    step_history_table: str,
    task_complexity: str | None = None,
    soothe_config: Any | None = None,
) -> bool | None:
    """Decide whether a coverage Eval is required via TypeSafe.

    Args:
        user_goal: Original user goal text.
        step_history_table: Rendered step history (descriptions, expected
            outputs, close reports, outcomes).
        task_complexity: Intake label value, when known.
        soothe_config: `SootheConfig` providing the classifier provider and
            decision parameters (nano `classifier` block).

    Returns:
        `True` to run the audit, `False` to skip it, or `None` when the
        classifier is disabled/unavailable, the verdict is untrusted, or the
        suppression bar is not met — the caller then uses the LLM path.
    """
    cfg = classifier_config(soothe_config)
    if cfg is None or not cfg.enabled:
        return None

    from langchain_typesafe import Choice

    classifier = build_typesafe_classifier(
        soothe_config,
        {_QUESTION_ID: Choice(instructions=_INSTRUCTIONS, criteria=_CRITERIA)},
    )
    if classifier is None:
        return None

    state: dict[str, Any] = {
        "goal": truncate_text(user_goal.strip(), limit=_GOAL_MAX_CHARS),
        "step_history": truncate_text(step_history_table, limit=_HISTORY_MAX_CHARS),
    }
    if task_complexity:
        state["task_complexity"] = task_complexity

    try:
        response = await classifier.ainvoke(state)
    except Exception as exc:  # noqa: BLE001 — never block on the classifier
        logger.warning("[eval_decision] typesafe unavailable (%s); using LLM", exc)
        return None

    choices = getattr(response, "choices", {}) or {}
    answer = choices.get(_QUESTION_ID)
    if answer is None:
        logger.warning("[eval_decision] typesafe returned no answer; using LLM")
        return None

    trusted, reason = verdict_trusted(cfg, answer)
    if not trusted:
        logger.info("[eval_decision] verdict untrusted (%s); using LLM", reason)
        return None

    choice = str(getattr(answer, "choice", "") or "")
    confidence = getattr(answer, "confidence", None)
    if choice == _EVAL:
        # Same direction as the fail-safe — no extra bar needed.
        logger.debug("[eval_decision] typesafe: run audit (conf=%s)", confidence)
        return True
    if choice == _NO_EVAL:
        suppress_bar = float(cfg.suppress_min_confidence)
        if confidence is not None and confidence < suppress_bar:
            logger.info(
                "[eval_decision] suppression below bar (conf=%s < %s); using LLM",
                confidence,
                suppress_bar,
            )
            return None
        logger.debug("[eval_decision] typesafe: skip audit (conf=%s)", confidence)
        return False

    logger.info("[eval_decision] typesafe returned unknown choice %r; using LLM", choice)
    return None


__all__ = ["decide_eval_coverage_typesafe"]
