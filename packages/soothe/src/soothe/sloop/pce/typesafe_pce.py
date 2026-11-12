"""Typesafe-first PCE verdict with LLM escalation.

A Choice question maps the goal-DAG snapshot onto one of the six signal
kinds. The drift guard reuses nano's verdict_trusted: untrusted
(below min_confidence or near-tie) verdicts escalate to a fast-LLM
structured pass instead of being acted on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from soothe_nano.llm import ainvoke_structured_traced

from soothe.sloop.clarification.typesafe_client import (
    build_classifier,
    classifier_config,
    verdict_trusted,
)
from soothe.utils.text import truncate_text

if TYPE_CHECKING:
    from soothe.config import SootheConfig

logger = logging.getLogger(__name__)

_QUESTION_ID = "pce_verdict"

_SNAPSHOT_MAX_CHARS = 6000

PCE_SYSTEM = (
    "You are a coverage monitor for an autonomous agent loop. Given the "
    "current goal DAG snapshot, judge the state of the ACTIVE goal. "
    "Respond with one kind, a confidence between 0 and 1, and a one-line "
    "reasoning. Kinds: on_track (progress aligns with the goal), "
    "coverage_gap (visible work does not cover the goal), "
    "goal_likely_complete (goal appears achieved), "
    "runaway_step (active step is far beyond a reasonable budget for its "
    "description), stalled (no meaningful progress), scope_drift (work "
    "has drifted away from the goal)."
)

_INSTRUCTIONS = (
    "Judge the execution state of the active goal in this DAG snapshot. "
    "on_track when steps align with the goal; coverage_gap when the "
    "goal needs work the DAG does not show; goal_likely_complete when "
    "the goal appears achieved; runaway_step when an active step is far "
    "beyond a reasonable budget; stalled when nothing progresses; "
    "scope_drift when the work no longer matches the goal."
)

_CRITERIA: dict[str, str] = {
    "on_track": "Completed and active steps map onto the goal; no missing area.",
    "coverage_gap": "The goal implies work that no step covers or schedules.",
    "goal_likely_complete": "Steps finished and evidence suggests the goal is met.",
    "runaway_step": "An active step's tool/token/elapsed budget vastly exceeds its scope.",
    "stalled": "No step is progressing and none can proceed.",
    "scope_drift": "Step descriptions or ledger content diverge from the goal.",
}


@dataclass
class PceVerdict:
    kind: str
    confidence: float
    source: str  # "typesafe" | "llm"
    reasoning: str = ""


class PceLlmVerdict(BaseModel):
    """Structured fast-LLM escalation verdict."""

    kind: str = Field(description="One of the six PCE signal kinds")
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = ""


async def decide_pce_verdict_typesafe(
    snapshot_input: str,
    *,
    soothe_config: SootheConfig | None,
) -> PceVerdict | None:
    """Typesafe verdict; None when unavailable or untrusted."""
    try:
        from langchain_typesafe import Choice
        from langchain_typesafe.types import ClassifierRequest

        classifier = build_classifier(soothe_config)
    except Exception as exc:  # noqa: BLE001 — never block on the classifier
        logger.info("[pce] classifier unavailable (%s)", exc)
        return None
    if classifier is None:
        return None

    state: dict[str, Any] = {
        "snapshot": truncate_text(snapshot_input, limit=_SNAPSHOT_MAX_CHARS),
    }
    questions = {_QUESTION_ID: Choice(instructions=_INSTRUCTIONS, criteria=_CRITERIA)}
    try:
        response = await classifier.ainvoke(ClassifierRequest(state=state, questions=questions))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[pce] classifier invoke failed (%s)", exc)
        return None

    answer = (getattr(response, "answers", {}) or {}).get(_QUESTION_ID)
    if answer is None:
        return None
    cfg = classifier_config(soothe_config)
    trusted, reason = verdict_trusted(cfg, answer)
    if not trusted:
        logger.info("[pce] typesafe verdict untrusted (%s)", reason)
        return None

    kind = str(getattr(answer, "choice", "") or "")
    if kind not in _CRITERIA:
        return None
    confidence = getattr(answer, "confidence", None)
    return PceVerdict(
        kind=kind,
        confidence=float(confidence) if confidence is not None else 0.0,
        source="typesafe",
    )


async def pce_llm_verdict(
    fast_model: Any,
    snapshot_input: str,
    *,
    soothe_config: SootheConfig | None,
) -> PceLlmVerdict | None:
    """Fast-LLM escalation pass; None on any failure."""
    if fast_model is None:
        return None
    try:
        data = await ainvoke_structured_traced(
            fast_model,
            [
                SystemMessage(content=PCE_SYSTEM),
                HumanMessage(content=truncate_text(snapshot_input, limit=_SNAPSHOT_MAX_CHARS)),
            ],
            json_schema=PceLlmVerdict.model_json_schema(),
            schema_name="PceLlmVerdict",
            strict=True,
            soothe_config=soothe_config,
            purpose="pce_llm_verdict",
            component="sloop.pce.typesafe_pce",
            phase="execute",
        )
        verdict = PceLlmVerdict.model_validate(data)
    except Exception:  # noqa: BLE001
        logger.warning("[pce] LLM escalation failed", exc_info=True)
        return None
    if verdict.kind not in _CRITERIA:
        return None
    return verdict
