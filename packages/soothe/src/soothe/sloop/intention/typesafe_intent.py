"""TypeSafe-backed intent classification with LLM fallback.

Intent routing is a categorical decision — 4-way `intake_label` plus a
4-way `response_language` — which is exactly what the TypeSafe `Choice`
primitive answers (label + full distribution + confidence, no generation).
Both questions travel in one request, so a task query can be routed without
any LLM round trip.

`classify_intent_typesafe` returns `None` whenever the verdict cannot be
trusted or the backend is unavailable; the caller then runs the existing LLM
path unchanged. It also returns `None` for a `chitchat` label: a social reply
requires generated text, which a decision model cannot produce.
"""

from __future__ import annotations

import logging
from typing import Any

from soothe.sloop.clarification.typesafe_client import (
    build_typesafe_classifier,
    classifier_config,
    verdict_trusted,
)
from soothe.sloop.intention.models import (
    IntakeLabel,
    IntentClassification,
    ResponseLanguage,
    derive_task_complexity_from_intake,
)
from soothe.utils.text import truncate_text

logger = logging.getLogger(__name__)

_LABEL_QUESTION_ID = "intent"
_LANGUAGE_QUESTION_ID = "language"

_QUERY_MAX_CHARS = 1000

_LABEL_INSTRUCTIONS = "Classify this user request by how much work it needs from the assistant."

_LABEL_CRITERIA: dict[str, str] = {
    IntakeLabel.CHITCHAT: (
        "Social exchange, greeting, or small talk — no tool work or task execution is expected."
    ),
    IntakeLabel.MINIMAL: (
        "A direct single-step action: one tool call or lookup answers it, no "
        "planning or decomposition."
    ),
    IntakeLabel.SIMPLE: (
        "A single deliverable needing a few steps — light planning, one concrete artifact."
    ),
    IntakeLabel.COMPLEX: (
        "Multi-phase work: needs planning, decomposition, exploration across "
        "files, or a full agent loop."
    ),
}

_LANGUAGE_INSTRUCTIONS = "Which language is the user writing in?"

_LANGUAGE_CRITERIA: dict[str, str] = {
    ResponseLanguage.EN: "English",
    ResponseLanguage.ZH: "Chinese",
    ResponseLanguage.JA: "Japanese",
    ResponseLanguage.KO: "Korean",
}


async def classify_intent_typesafe(
    query: str,
    *,
    soothe_config: Any | None = None,
) -> IntentClassification | None:
    """Classify a query's intent via TypeSafe, or `None` to fall back.

    Args:
        query: The user's request text.
        soothe_config: `SootheConfig` providing the classifier provider and
            decision parameters (nano `classifier` block).

    Returns:
        An `IntentClassification` when a trusted verdict is available and no
        generation is required; `None` when the classifier is disabled or
        unavailable, the verdict is untrusted (distribution shift / near-tie),
        or the label is `chitchat` (needs a generated reply). Callers fall
        back to the existing LLM path on `None`.
    """
    cfg = classifier_config(soothe_config)
    if cfg is None or not cfg.enabled:
        return None

    from langchain_typesafe import Choice

    classifier = build_typesafe_classifier(
        soothe_config,
        {
            _LABEL_QUESTION_ID: Choice(instructions=_LABEL_INSTRUCTIONS, criteria=_LABEL_CRITERIA),
            _LANGUAGE_QUESTION_ID: Choice(
                instructions=_LANGUAGE_INSTRUCTIONS, criteria=_LANGUAGE_CRITERIA
            ),
        },
    )
    if classifier is None:
        return None

    state = {"query": truncate_text(query.strip(), limit=_QUERY_MAX_CHARS)}
    try:
        response = await classifier.ainvoke(state)
    except Exception as exc:  # noqa: BLE001 — never block on the classifier
        logger.warning("[intent] typesafe classification unavailable (%s); falling back", exc)
        return None

    choices = getattr(response, "choices", {}) or {}
    label_answer = choices.get(_LABEL_QUESTION_ID)
    if label_answer is None:
        logger.warning("[intent] typesafe returned no label answer; falling back")
        return None

    trusted, reason = verdict_trusted(cfg, label_answer)
    if not trusted:
        logger.info("[intent] typesafe verdict untrusted (%s); falling back", reason)
        return None

    raw_label = str(getattr(label_answer, "choice", "") or "")
    try:
        label = IntakeLabel(raw_label)
    except ValueError:
        logger.info("[intent] typesafe returned unknown label %r; falling back", raw_label)
        return None

    if cfg.shadow:
        logger.info(
            "[intent] typesafe(shadow) label=%s conf=%s; using LLM path",
            label.value,
            getattr(label_answer, "confidence", None),
        )
        return None

    # Language rides along in the same request but is gated independently:
    # an untrusted language verdict leaves the field unset rather than
    # misrouting the reply language.
    language = choices.get(_LANGUAGE_QUESTION_ID)
    if language is not None:
        lang_trusted, lang_reason = verdict_trusted(cfg, language)
        if not lang_trusted:
            logger.debug("[intent] language verdict untrusted (%s); leaving unset", lang_reason)
            language = None

    if label is IntakeLabel.CHITCHAT:
        # A social reply needs generated text — a decision model cannot
        # produce it, so hand this back to the LLM path.
        logger.debug("[intent] chitchat needs a generated reply; falling back")
        return None

    return IntentClassification(
        intake_label=label,
        reasoning=None,
        chitchat_response=None,
        response_language=_language_from(language),
        task_complexity=derive_task_complexity_from_intake(label),
    )


def _language_from(answer: Any) -> ResponseLanguage | None:
    """Map the language answer onto `ResponseLanguage`, or `None`."""
    if answer is None:
        return None
    try:
        return ResponseLanguage(str(getattr(answer, "choice", "") or ""))
    except ValueError:
        return None


__all__ = ["classify_intent_typesafe"]
