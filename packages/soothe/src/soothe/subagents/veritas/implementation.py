"""Veritas auto-answerer: decision-model fast path + single structured-output LLM call."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from soothe_nano.llm import StructuredOutputError, ainvoke_structured_traced

from soothe.sloop.clarification.protocol import ClarificationRequest
from soothe.subagents.veritas.prompts import (
    build_veritas_system_prompt,
    build_veritas_user_prompt,
)
from soothe.subagents.veritas.schemas import (
    VeritasAnswerSchema,
    build_veritas_response_schema,
    coerce_veritas_response,
)
from soothe.utils.text import truncate_text

if TYPE_CHECKING:
    from soothe.config.models import SootheConfig

logger = logging.getLogger(__name__)

_FORCED_DEFER_PREFIX_STRUCTURED = "structured_output_failed"
_FORCED_DEFER_RATIONALE_QUESTION = "answer_was_question"
_FORCED_DEFER_PREFIX_TRANSIENT = "transient_failure"

_QUESTION_PREVIEW_CHARS = 120
_PROMPT_PREVIEW_CHARS = 240

_DEFAULT_MAX_RETRIES = 2
_DEFAULT_RETRY_BACKOFF_SECONDS = 2.0

VeritasAnswerer = Callable[..., Awaitable[VeritasAnswerSchema]]
"""Bound veritas answerer: ``await answerer(request, thread_id=..., loop_id=...)``."""

# Argument keys worth sending to the decision model (bounded, low secret risk).
_CLASSIFY_ARG_KEYS: tuple[str, ...] = ("command", "file_path", "path", "directory")
_CLASSIFY_PREVIEW_CHARS = 200
_SECRET_ARG_MARKERS = ("token", "password", "secret", "api_key", "authorization")


def build_veritas_answerer(
    soothe_config: SootheConfig,
    *,
    model_role: str | None = None,
) -> VeritasAnswerer:
    """Build a configured veritas answerer (single source of truth).

    Both inline consumers share this constructor so model selection, context
    depth, retry policy, and confidence coercion stay in one place:

    `AskUserGateMiddleware` (RFC-635) — built once per CoreAgent graph,
      called per `ask_user` tool call with per-run trace ids.
    `AutoClarificationPolicy` (station) — built once per goal run for
      rail_pause / planner-emitted questions and gate fail-safes.
    `AutoModeMiddleware` — built once per CoreAgent graph,
      called per L1-unresolved gated tool call.

    When `agent.veritas.decision_fast_path` is enabled, the answerer first
    consults the calibrated decision model (Jev) and only falls through to
    the LLM for open questions, untrusted verdicts, and trusted escalates.

    Args:
        soothe_config: Config providing `agent.veritas` and the chat-model
            factory.
        model_role: Override for `agent.veritas.model_role`.
    """

    veritas_cfg = soothe_config.agent.veritas
    model = soothe_config.create_chat_model(model_role or veritas_cfg.model_role)
    fast_path_cfg = getattr(veritas_cfg, "decision_fast_path", None)
    decision_model = None
    if fast_path_cfg is not None and getattr(fast_path_cfg, "enabled", False):
        decision_model = _LazyRiskClassifier(soothe_config)

    async def _answerer(
        request: ClarificationRequest,
        *,
        thread_id: str | None = None,
        loop_id: str | None = None,
    ) -> VeritasAnswerSchema:
        return await answer(
            request,
            model=model,
            max_context_steps=veritas_cfg.max_context_steps,
            soothe_config=soothe_config,
            thread_id=thread_id,
            loop_id=loop_id,
            max_retries=veritas_cfg.max_retries,
            retry_backoff_seconds=veritas_cfg.retry_backoff_seconds,
            coerced_confidence=veritas_cfg.coerced_confidence,
            decision_model=decision_model,
        )

    return _answerer


async def answer(
    request: ClarificationRequest,
    *,
    model: BaseChatModel,
    max_context_steps: int = 8,
    soothe_config: SootheConfig | None = None,
    thread_id: str | None = None,
    loop_id: str | None = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    retry_backoff_seconds: float = _DEFAULT_RETRY_BACKOFF_SECONDS,
    coerced_confidence: float = 0.7,
    decision_model: Any | None = None,
) -> VeritasAnswerSchema:
    """Produce a clarification answer grounded in goal intent and global context.

    Decision fast path: when `decision_model` is provided and the request
    carries an approval subject, the calibrated model is consulted first.
    Trusted `allow` / `reject` verdicts resolve inline
    (`source="decision_model"`); trusted `escalate` and everything unusable
    (untrusted / unavailable / no subject) fall through to the LLM leg —
    `escalate` with the risk assessment embedded in the prompt.
    """
    view = request.loop_state
    n = len(request.questions)

    logger.debug(
        "[veritas] answer() goal_id=%s origin=%s questions=%d iter=%d "
        "context_steps=%d active_skills=%d thread_id=%s",
        view.goal_id,
        request.origin_node,
        n,
        view.iteration,
        len(view.recent_step_outputs),
        len(view.active_skills),
        thread_id or "<none>",
    )
    logger.debug("[veritas] questions: %s", _preview_questions(request.questions))

    json_schema = build_veritas_response_schema(n)
    system_prompt = build_veritas_system_prompt()
    user_prompt = build_veritas_user_prompt(request, max_context_steps=max_context_steps)

    fast = await _decision_fast_path(request, decision_model)
    if fast is not None:
        kind, payload = fast
        if kind == "resolve":
            return payload
        # Trusted escalate: deep-judge with the LLM, embedding the risk hint.
        user_prompt += (
            "\n\n## Calibrated risk assessment (decision model)\n"
            "The calibrated decision model classified this action as "
            f"**escalate** (confidence={payload.confidence}, "
            f"probabilities={payload.probabilities}). Weigh this assessment "
            "against the goal context when answering."
        )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]

    logger.debug(
        "[veritas] prompt sizes: system=%d chars user=%d chars",
        len(system_prompt),
        len(user_prompt),
    )
    logger.debug(
        "[veritas] user prompt preview: %s", truncate_text(user_prompt, limit=_PROMPT_PREVIEW_CHARS)
    )

    trace_name = (
        (soothe_config.observability.langfuse.trace_name or "").strip()
        if soothe_config is not None
        else ""
    )
    model_name = getattr(model, "model", None) or model.__class__.__name__
    data: dict[str, Any] | None = None
    last_exc: Exception | None = None
    attempts_used = 0
    t0 = time.monotonic()

    for attempt in range(max_retries + 1):
        attempts_used = attempt + 1
        try:
            data = await ainvoke_structured_traced(
                model,
                messages,
                json_schema=json_schema,
                schema_name="VeritasAnswer",
                strict=True,
                soothe_config=soothe_config,
                purpose="clarification_answer",
                component="subagent.veritas",
                phase="pre-stream",
                session_id=thread_id,
                loop_id=loop_id,
                run_name=f"{trace_name}:veritas" if trace_name else "veritas",
                normalize=lambda value: coerce_veritas_response(
                    value,
                    n,
                    coerced_confidence=coerced_confidence,
                ),
            )
            break
        except StructuredOutputError as exc:
            logger.warning("[veritas] structured output failed: %s", exc)
            _log_call_stat(
                model_name=model_name,
                goal_id=view.goal_id,
                origin=request.origin_node,
                elapsed=time.monotonic() - t0,
                attempts=attempts_used,
                outcome="structured_output_error",
                defer=True,
                confidence=0.0,
                answers=0,
                thread_id=thread_id,
            )
            return VeritasAnswerSchema(
                defer=True,
                confidence=0.0,
                rationale=f"{_FORCED_DEFER_PREFIX_STRUCTURED}: {exc}",
                answers=[],
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < max_retries:
                backoff = retry_backoff_seconds * (2**attempt)
                logger.warning(
                    "[veritas] transient failure (attempt %d/%d): %s; retrying in %.1fs",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
            else:
                logger.warning(
                    "[veritas] transient failure exhausted retries (%d/%d): %s",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                )

    if data is None:
        _log_call_stat(
            model_name=model_name,
            goal_id=view.goal_id,
            origin=request.origin_node,
            elapsed=time.monotonic() - t0,
            attempts=attempts_used,
            outcome="transient_failure",
            defer=True,
            confidence=0.0,
            answers=0,
            thread_id=thread_id,
        )
        return VeritasAnswerSchema(
            defer=True,
            confidence=0.0,
            rationale=f"{_FORCED_DEFER_PREFIX_TRANSIENT}: {last_exc}",
            answers=[],
        )

    result = VeritasAnswerSchema.model_validate(data)
    logger.debug(
        "[veritas] result defer=%s confidence=%.2f answers=%d rationale=%s",
        result.defer,
        result.confidence,
        len(result.answers),
        truncate_text(result.rationale, limit=_QUESTION_PREVIEW_CHARS),
    )
    _log_call_stat(
        model_name=model_name,
        goal_id=view.goal_id,
        origin=request.origin_node,
        elapsed=time.monotonic() - t0,
        attempts=attempts_used,
        outcome="ok",
        defer=result.defer,
        confidence=result.confidence,
        answers=len(result.answers),
        thread_id=thread_id,
    )
    if result.reasoning:
        logger.debug(
            "[veritas] reasoning: %s",
            truncate_text(result.reasoning, limit=_QUESTION_PREVIEW_CHARS),
        )

    if not result.defer and _any_answer_is_a_question(result.answers, result.answer_is_question):
        logger.info("[veritas] answer classified as question; coercing to defer")
        return result.model_copy(
            update={
                "defer": True,
                "confidence": 0.0,
                "rationale": _FORCED_DEFER_RATIONALE_QUESTION,
            }
        )

    return result


def _any_answer_is_a_question(
    answers: list[str],
    answer_is_question: list[bool] | None = None,
) -> bool:
    """Detect whether any answer is itself a question.

    Uses the model's structured `answer_is_question` field. When the model
    omits it (malformed response), returns False.
    """
    if answer_is_question:
        return any(answer_is_question)
    return False


class _LazyRiskClassifier:
    """Defers decision-model provider resolution until the first
    decision-shaped request.

    Keeps `build_veritas_answerer` cheap (no classifier client construction
    at graph build) and keeps provider-resolution failures confined to the
    fast path, where they degrade to the LLM leg.
    """

    def __init__(self, soothe_config: Any) -> None:
        self._soothe_config = soothe_config
        self._built: Any | None = None

    async def classify(self, items: list[Any]) -> list[Any]:
        if self._built is None:
            from soothe.subagents.veritas.decision_model import build_risk_classifier

            self._built = build_risk_classifier(self._soothe_config)
        return await self._built.classify(items)


async def _decision_fast_path(
    request: ClarificationRequest,
    classifier: Any | None,
) -> tuple[str, Any] | None:
    """RFC-LOOP-030 fast path: Jev first, LLM second.

    Applicable only to requests carrying an approval subject — the
    `tool_approval` origin's `action_requests` metadata. Open question
    shapes (plain `ask_user`, rail pauses) have no subject and skip the
    classifier entirely.

    Returns:
        `("resolve", VeritasAnswerSchema)` — trusted allow/reject resolved
        inline with `source="decision_model"`.
        `("escalate", RiskVerdict)` — trusted escalate; the LLM leg runs
        with the risk assessment embedded in the prompt.
        `None` — no subject / no classifier / untrusted / unavailable:
            plain LLM path.
    """
    if classifier is None:
        return None
    action_requests = (request.metadata or {}).get("action_requests")
    if not isinstance(action_requests, list) or not action_requests:
        return None
    subject = action_requests[0]
    if not isinstance(subject, Mapping):
        return None
    tool = str(subject.get("name") or subject.get("id") or "")
    if not tool:
        return None

    from soothe.subagents.veritas.decision_model import RiskQuery

    raw_args = subject.get("args")
    query = RiskQuery(
        tool=tool,
        args_preview=_bounded_args_preview(raw_args if isinstance(raw_args, dict) else {}),
        goal_summary=getattr(request.loop_state, "goal_description", None) or None,
    )
    try:
        verdicts = await classifier.classify([query])
    except Exception:  # noqa: BLE001 — never block on the decision model
        logger.warning("[veritas] decision fast path failed; using the LLM path", exc_info=True)
        return None
    if not verdicts:
        return None
    verdict = verdicts[0]

    if verdict.band in ("allow", "reject"):
        n = max(1, len(request.questions))
        answer = "approve" if verdict.band == "allow" else "deny"
        schema = VeritasAnswerSchema(
            defer=False,
            answers=[answer] * n,
            confidence=float(verdict.confidence) if verdict.confidence is not None else 0.0,
            rationale=f"decision model {verdict.band}: {verdict.reason}",
            answer_is_question=[False] * n,
            source="decision_model",
        )
        _log_call_stat(
            model_name="decision-model",
            goal_id=request.loop_state.goal_id,
            origin=request.origin_node,
            elapsed=0.0,
            attempts=1,
            outcome=f"decision_fast_path_{verdict.band}",
            defer=False,
            confidence=schema.confidence,
            answers=n,
            thread_id=None,
        )
        return ("resolve", schema)

    if verdict.band == "escalate":
        logger.info(
            "[veritas] decision fast path escalate tool=%s; deep-judging with the LLM",
            tool,
        )
        return ("escalate", verdict)

    # untrusted / unavailable — the LLM leg decides.
    return None


def _bounded_args_preview(args: dict[str, Any]) -> dict[str, Any]:
    """Bounded, redacted argument preview for the decision model."""
    preview: dict[str, Any] = {}
    for key in _CLASSIFY_ARG_KEYS:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if any(marker in key.lower() for marker in _SECRET_ARG_MARKERS):
            continue
        preview[key] = truncate_text(value.strip(), limit=_CLASSIFY_PREVIEW_CHARS)
        break
    return preview


def _preview_questions(questions: tuple) -> str:
    # RFC-622 §9c: questions may be QuestionSpec.model_dump() dicts, not just strings.
    return " | ".join(
        truncate_text(_question_text(q), limit=_QUESTION_PREVIEW_CHARS) for q in questions
    )


def _question_text(question: Any) -> str:
    """Render a structured question or plain string as text."""
    if isinstance(question, dict):
        return str(question.get("question") or question.get("header") or "")
    return str(question)


def _log_call_stat(
    *,
    model_name: str,
    goal_id: str | None,
    origin: str,
    elapsed: float,
    attempts: int,
    outcome: str,
    defer: bool,
    confidence: float,
    answers: int,
    thread_id: str | None,
) -> None:
    """Emit an info-level log line for veritas LLM call-stat analysis.

    Fields: model, goal_id, origin, elapsed_ms, attempts, outcome,
    defer, confidence, answers, thread_id. Designed for grep/awk
    aggregation over `[veritas] call_stat` lines.
    """
    logger.info(
        "[veritas] call_stat model=%s goal_id=%s origin=%s elapsed_ms=%.0f "
        "attempts=%d outcome=%s defer=%s confidence=%.2f answers=%d thread_id=%s",
        model_name,
        goal_id or "<none>",
        origin,
        elapsed * 1000,
        attempts,
        outcome,
        defer,
        confidence,
        answers,
        thread_id or "<none>",
    )


__all__ = ["VeritasAnswerer", "answer", "build_veritas_answerer"]
