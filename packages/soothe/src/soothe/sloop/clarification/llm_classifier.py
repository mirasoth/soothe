"""LLM-backed `Choice` classifier — stand-in for TypeSafe when unavailable.

When the TypeSafe classifier backend cannot be built (missing
`TYPESAFE_API_KEY`, transport error, missing dependency), every shared-helper
consumer would otherwise bail to its fail-safe default and skip the decision
entirely. Loop ``01a1bde2-…-83a1`` made this visible: with the key unset,
``build_typesafe_classifier`` returned ``None`` on every call, every station
hit ``if classifier is None: return None``, and no clarification verdicts were
produced for the whole loop.

This module provides a structural drop-in: the same
``ainvoke(ClassifierRequest) -> ClassifierResponse`` contract and the same
``ChoiceAnswer`` shape (``choice``, ``confidence``, ``probabilities``),
backed by the configured ``fast`` model role. ``verdict_trusted`` therefore
works unchanged — low-confidence and near-tie LLM verdicts fall back exactly
as untrusted TypeSafe verdicts do.

It is a best-effort stand-in, not a calibrated replacement. The label set
comes from each consumer's ``Choice.criteria`` dict (no keyword heuristics —
the LLM does the classification), and the structured-output schema locks the
``choice`` enum to those labels so the model cannot invent answers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_typesafe import ChoiceAnswer, ClassifierResponse
from langchain_typesafe.types import ClassifierRequest, Usage
from soothe_nano.llm.invoke_policy import (
    await_with_llm_call_policy,
    llm_rate_limit_config_from,
)
from soothe_nano.llm.structured import invoke_structured_chat

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.runnables import RunnableConfig

logger = logging.getLogger(__name__)

_FALLBACK_MODEL_TAG = "llm-fallback"

_SYSTEM_PROMPT = (
    "You are a classification engine. For each question, pick exactly one "
    "label from the provided criteria and report a confidence score in "
    "[0.0, 1.0] together with a probability distribution over all labels "
    "(summing to 1.0). Select the label that best fits the decision context. "
    "When the context is ambiguous or out of scope, spread probability "
    "evenly across the labels and report a low confidence."
)


class LLMChoiceClassifier:
    """TypeSafe-compatible `Choice` classifier backed by the fast LLM model.

    Stand-in for ``TypeSafeClassifier`` when the TypeSafe backend is
    unavailable. Answers ``ClassifierRequest``\\s with structured output,
    producing ``ChoiceAnswer``\\s with ``choice``, ``confidence``, and
    ``probabilities`` so ``verdict_trusted`` gates them uniformly.

    On any LLM or parse failure, returns a ``ClassifierResponse`` with empty
    ``answers`` so consumers degrade to their fail-safe default rather than
    raising — the same posture as a TypeSafe backend that returns no answer.
    """

    def __init__(self, model: BaseChatModel, *, soothe_config: Any = None) -> None:
        """Bind a fast chat model to the classifier contract.

        Args:
            model: A resolved ``BaseChatModel`` (typically the ``fast`` role).
            soothe_config: Optional ``SootheConfig`` for rate-limit policy.
        """
        self._model = model
        self._soothe_config = soothe_config

    async def ainvoke(
        self,
        input: ClassifierRequest,
        config: RunnableConfig | None = None,
        **_: Any,
    ) -> ClassifierResponse:
        """Classify the request's questions via the fast LLM model.

        Args:
            input: A ``ClassifierRequest`` with ``state`` (decision context)
                and ``questions`` (id -> ``Choice`` with instructions and
                criteria).
            config: Optional LangChain runnable config forwarded to the
                structured invoke.
            **_: Ignored keyword args, for signature compatibility with
                ``TypeSafeClassifier.ainvoke``.

        Returns:
            A ``ClassifierResponse`` whose ``.answers`` maps each question id
            to a ``ChoiceAnswer``. Empty ``answers`` on any failure.
        """
        questions = input.get("questions", {}) if isinstance(input, dict) else {}
        if not questions:
            return ClassifierResponse(model=_FALLBACK_MODEL_TAG, answers={}, usage=Usage())

        labels_by_qid: dict[str, list[str]] = {
            qid: list((getattr(q, "criteria", {}) or {}).keys()) for qid, q in questions.items()
        }

        schema = _build_schema(labels_by_qid)
        state = input.get("state", {}) if isinstance(input, dict) else {}
        messages = _build_messages(state, questions, labels_by_qid)

        async def _invoke() -> dict[str, Any]:
            return await invoke_structured_chat(
                self._model,
                messages,
                json_schema=schema,
                schema_name="LLMChoiceVerdict",
                strict=True,
                config=config,
            )

        try:
            rate_cfg = llm_rate_limit_config_from(self._soothe_config)
            result = await await_with_llm_call_policy(_invoke, config=rate_cfg)
        except Exception as exc:  # noqa: BLE001 — never block on the classifier
            logger.warning("[classifier:llm] structured invoke failed: %s", exc)
            return ClassifierResponse(model=_FALLBACK_MODEL_TAG, answers={}, usage=Usage())

        if not isinstance(result, dict) or not result:
            return ClassifierResponse(model=_FALLBACK_MODEL_TAG, answers={}, usage=Usage())

        answers: dict[str, ChoiceAnswer] = {}
        raw_questions = result.get("questions") or {}
        for qid, labels in labels_by_qid.items():
            entry = raw_questions.get(qid) or {}
            choice = entry.get("choice")
            if choice not in labels:
                logger.debug("[classifier:llm] question %s returned no valid choice", qid)
                continue
            confidence = _clamp_float(entry.get("confidence"))
            probabilities = _normalize_probs(entry.get("probabilities"), labels)
            answers[qid] = ChoiceAnswer(
                type="choice",
                choice=str(choice),
                confidence=confidence,
                probabilities=probabilities,
            )

        return ClassifierResponse(model=_FALLBACK_MODEL_TAG, answers=answers, usage=Usage())


def _build_schema(labels_by_qid: dict[str, list[str]]) -> dict[str, Any]:
    """Build a JSON Schema locking each question's choice to its label set.

    Each question id maps to an object whose ``choice`` is an enum of the
    ``criteria`` keys and whose ``probabilities`` is an object keyed by those
    same labels — so the model cannot emit a label not in the question.
    """
    question_props: dict[str, Any] = {}
    for qid, labels in labels_by_qid.items():
        question_props[qid] = {
            "type": "object",
            "properties": {
                "choice": {"type": "string", "enum": labels},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "probabilities": {
                    "type": "object",
                    "properties": {label: {"type": "number"} for label in labels},
                    "required": list(labels),
                },
            },
            "required": ["choice", "confidence", "probabilities"],
        }
    return {
        "type": "object",
        "properties": {
            "questions": {
                "type": "object",
                "properties": question_props,
                "required": list(labels_by_qid.keys()),
            }
        },
        "required": ["questions"],
    }


def _build_messages(
    state: dict[str, Any],
    questions: dict[str, Any],
    labels_by_qid: dict[str, list[str]],
) -> list[Any]:
    """Assemble system + human messages from the request state and questions."""
    blocks: list[str] = []
    for qid, labels in labels_by_qid.items():
        q = questions[qid]
        instructions = getattr(q, "instructions", "") or ""
        criteria = getattr(q, "criteria", {}) or {}
        lines = [f"Question {qid}: {instructions}".rstrip(), "Labels:"]
        for label in labels:
            lines.append(f"- {label}: {criteria.get(label, '')}")
        blocks.append("\n".join(lines))

    state_lines = [f"{k}: {v}" for k, v in state.items()] if state else ["(no context provided)"]
    human = (
        "Decision context:\n"
        + "\n".join(state_lines)
        + "\n\n---\nQuestions:\n"
        + "\n\n".join(blocks)
    )
    return [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=human)]


def _clamp_float(value: Any) -> float | None:
    """Coerce a confidence value to a float in [0.0, 1.0], or ``None``."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def _normalize_probs(raw: Any, labels: list[str]) -> dict[str, float]:
    """Normalize a raw probability map over ``labels`` to a sum-to-1 distribution.

    Missing labels default to 0.0; if all entries are missing or non-positive,
    a uniform distribution is returned so ``decisive()`` sees a real shape
    rather than an empty map.
    """
    if not isinstance(raw, dict):
        raw = {}
    probs: dict[str, float] = {}
    for label in labels:
        try:
            probs[label] = float(raw.get(label, 0.0) or 0.0)
        except (TypeError, ValueError):
            probs[label] = 0.0
    total = sum(probs.values())
    if total <= 0.0:
        even = 1.0 / len(labels)
        return {label: even for label in labels}
    return {label: p / total for label, p in probs.items()}


__all__ = ["LLMChoiceClassifier"]
