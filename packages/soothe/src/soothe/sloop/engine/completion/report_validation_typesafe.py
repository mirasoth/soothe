"""TypeSafe-backed report validation gate before LEDGER_DIRECT passthrough.

`validate_report_typesafe` decides whether the final step's output is a
valid goal report (substantive, addresses the user goal) and can be passed
through directly without a synthesis LLM call.

The decision is asymmetric:

- `invalid` (synthesize) matches the fail-safe default, so a trusted verdict
  is enough.
- `valid` **suppresses** the synthesis LLM call, so that direction
  additionally requires `suppress_min_confidence`.

Anything unavailable, untrusted, or near-tie returns `None` and the caller
runs the synthesis path unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from soothe.sloop.clarification.typesafe_client import (
    build_classifier,
    classifier_config,
    verdict_trusted,
)
from soothe.utils.text import truncate_text

if TYPE_CHECKING:
    from soothe.config import SootheConfig

logger = logging.getLogger(__name__)

_QUESTION_ID = "report_valid"

_VALID = "valid"
_INVALID = "invalid"

_GOAL_MAX_CHARS = 600
_OUTPUT_MAX_CHARS = 4000
_PRIOR_OUTPUT_MAX_CHARS = 4000

_INSTRUCTIONS = (
    "Is the final step output a valid goal report? A valid report is "
    "substantive (not a tool log or one-liner), addresses the user goal "
    "directly, is self-contained (it carries the full deliverable rather "
    "than recapping or referencing content delivered earlier), and is "
    "coherent enough to serve as the final goal report without further synthesis."
)

_CRITERIA: dict[str, str] = {
    _VALID: (
        "The output is substantive and addresses the user goal directly: "
        "it contains the deliverable itself (design draft, analysis, code, report), "
        "is self-contained — a reader seeing only this output gets the complete "
        "answer even when prior step output exists — is coherent and structured, "
        "and does not require further synthesis to be a useful final response."
    ),
    _INVALID: (
        "The output is not a valid goal report: it is a tool call log, a "
        "one-line status, a clarifying question without substantive content, "
        "a raw data dump, does not address the user goal, or it is a closing "
        "recap/summary that points at a deliverable delivered earlier in the "
        "step instead of containing the deliverable itself."
    ),
}


async def validate_report_typesafe(
    *,
    user_goal: str,
    step_output: str,
    step_kind: str | None = None,
    prior_output: str | None = None,
    pce_signal: str | None = None,
    soothe_config: SootheConfig | None = None,
) -> bool | None:
    """Decide whether the final step output is a valid goal report.

    Args:
        user_goal: Original user goal text.
        step_output: The final completed step's AI output text (the
            candidate goal report).
        step_kind: Step kind of the final completed step, when known
            (e.g. ``"brainstorm"``, ``"action"``, ``"eval"``).
        prior_output: Tail of the step's pre-final-hop AI text, when the
            step ran multiple assistant turns. Grounds the
            self-containedness judgment: a final hop that merely recaps
            content delivered in earlier hops is not a valid report.
        soothe_config: ``SootheConfig`` providing the classifier provider and
            decision parameters (nano ``classifier`` block).
        pce_signal: Latest unconsumed PCE coverage signal summary
            (kind + confidence), when one exists (RFC-SLP-005).

    Returns:
        `True` when the output is a valid goal report (passthrough),
        `False` when it is invalid (synthesize), or `None` when the
        classifier is disabled/unavailable, the verdict is untrusted, or
        the suppression bar is not met — the caller then runs the synthesis
        path.
    """
    cfg = classifier_config(soothe_config)
    if cfg is None or not cfg.enabled:
        return None

    from langchain_typesafe import Choice
    from langchain_typesafe.types import ClassifierRequest

    questions = {_QUESTION_ID: Choice(instructions=_INSTRUCTIONS, criteria=_CRITERIA)}
    try:
        classifier = build_classifier(soothe_config)
    except Exception as exc:  # noqa: BLE001 — never block on the classifier
        logger.info("[report_validation] classifier unavailable (%s); synthesizing", exc)
        return None
    if classifier is None:
        logger.info("[report_validation] classifier unavailable; synthesizing")
        return None

    state: dict[str, Any] = {
        "goal": truncate_text(user_goal.strip(), limit=_GOAL_MAX_CHARS),
        "step_output": truncate_text(step_output, limit=_OUTPUT_MAX_CHARS),
    }
    if step_kind:
        state["step_kind"] = step_kind
    if pce_signal:
        state["pce_signal"] = pce_signal
    prior = (prior_output or "").strip()
    if prior:
        # Tail: the hops closest to the final one carry what a recap would point at.
        state["prior_step_output"] = prior[-_PRIOR_OUTPUT_MAX_CHARS:]

    try:
        response = await classifier.ainvoke(ClassifierRequest(state=state, questions=questions))
    except Exception as exc:  # noqa: BLE001 — never block on the classifier
        logger.warning("[report_validation] classifier unavailable (%s); synthesizing", exc)
        return None

    answers = getattr(response, "answers", {}) or {}
    answer = answers.get(_QUESTION_ID)
    if answer is None:
        logger.info("[report_validation] classifier returned no answer; synthesizing")
        return None

    trusted, reason = verdict_trusted(cfg, answer)
    if not trusted:
        logger.info("[report_validation] verdict untrusted (%s); synthesizing", reason)
        return None

    choice = str(getattr(answer, "choice", "") or "")
    confidence = getattr(answer, "confidence", None)
    if choice == _INVALID:
        logger.debug("[report_validation] classifier: invalid (conf=%s)", confidence)
        return False
    if choice == _VALID:
        suppress_bar = float(cfg.suppress_min_confidence)
        if confidence is not None and confidence < suppress_bar:
            logger.info(
                "[report_validation] valid below suppression bar (conf=%s < %s); synthesizing",
                confidence,
                suppress_bar,
            )
            return None
        logger.debug("[report_validation] classifier: valid (conf=%s)", confidence)
        return True

    logger.info("[report_validation] classifier returned unknown choice %r; synthesizing", choice)
    return None


__all__ = ["validate_report_typesafe"]
