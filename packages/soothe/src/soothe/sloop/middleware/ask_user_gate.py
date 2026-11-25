"""Inline veritas fast path for `ask_user` tool calls.

`AskUserGateMiddleware` runs veritas in ``aafter_model`` and answers
confident questions with a synthetic `ToolMessage` (zero interrupts).
Defer / failure outcomes are re-emitted as a ``gate_deferred`` interrupt
so the station skips its own veritas call; without an attached human they
hard-defer (goal parks in awaiting_clarification) unless the operator
opted into `autopilot_retry_on_fail`.

Channel closure invariants (IG-LOOP-018): every ask_user question ends in
exactly one of — a veritas answer, a human answer, an explicit unanswered
sentinel, or a park. Malformed ask_user args are stripped with an
instructive error in ALL modes (their native interrupt is uncapturable
and would terminate the step stream). In bypass mode the gate activates
regardless of clarification mode: veritas still answers confident
questions, and defers render an honest bypass sentinel (never a fake
answer). Non-answer outcomes emit `CLARIFICATION_UNANSWERED`.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import interrupt

from soothe.events.catalog import CLARIFICATION_AUTO_ANSWERED, CLARIFICATION_UNANSWERED
from soothe.sloop.clarification.interrupt_kinds import INTERRUPT_TYPE_ASK_USER
from soothe.sloop.clarification.origins import ORIGIN_EXECUTE
from soothe.sloop.clarification.protocol import (
    ClarificationRequest,
    LoopStateView,
)
from soothe.sloop.middleware.auto_mode_context import AutoModeContext
from soothe.subagents.veritas.implementation import (
    _FORCED_DEFER_PREFIX_STRUCTURED,
    _FORCED_DEFER_RATIONALE_QUESTION,
)
from soothe.subagents.veritas.schemas import VeritasAnswerSchema
from soothe.utils.text import truncate_text

logger = logging.getLogger(__name__)

_ASK_USER_TOOL = "ask_user"

# Configurable key the executor writes the per-step LoopStateView under.
_LOOP_VIEW_KEY = "soothe_veritas_loop_view"
_LOOP_ID_KEY = "soothe_loop_id"

# Honest sentinels — they declare themselves NOT answers, so the agent never
# mistakes them for one (IG-LOOP-018 closure invariant).
_BYPASS_SENTINEL = (
    "(bypass mode: unanswered — no human surface is attached; "
    "decide autonomously or switch composer mode)"
)
_RETRY_SENTINEL = "(no answer available — veritas deferred; try a different action)"
_NO_ANSWER_SENTINEL = "(no answer provided for this question)"


class _GateOutcome:
    """One ask_user call's gate outcome."""

    __slots__ = ("questions", "result", "kind", "failed", "inline_retry", "malformed")

    def __init__(
        self,
        questions: list[dict[str, Any]] | None,
        result: VeritasAnswerSchema | None,
        kind: str | None,
        *,
        failed: bool = False,
        inline_retry: bool = False,
        malformed: bool = False,
    ) -> None:
        self.questions = questions or []
        self.result = result
        self.kind = kind
        self.failed = failed
        self.inline_retry = inline_retry
        self.malformed = malformed

    @property
    def confident(self) -> bool:
        return self.result is not None and self.kind is None and not self.failed


class AskUserGateMiddleware(AgentMiddleware):
    """Inline veritas fast path for `ask_user` calls. See module docstring."""

    def __init__(
        self,
        veritas_answer: Any,
        *,
        min_confidence: float = 0.4,
        default_clarification_mode: str = "auto",
        autopilot_retry_on_fail: bool = True,
    ) -> None:
        """Wire the veritas callable and gate posture.

        Args:
            veritas_answer: Async callable taking a `ClarificationRequest`
                and returning a `VeritasAnswerSchema` (the subagent's
                `answer`, model-bound at build time).
            min_confidence: Confidence threshold (mirrors the station policy).
            default_clarification_mode: Fallback when the per-run mode is
                absent from `configurable`.
            autopilot_retry_on_fail: Render the honest retry sentinel inline
                when no human is attached and veritas fails/defers (explicit
                operator opt-in — default is park: the interrupt hard-defers
                and the goal waits for a human).
        """
        super().__init__()
        self._veritas_answer = veritas_answer
        self._min_confidence = min_confidence
        self._default_mode = default_clarification_mode
        self._autopilot_retry_on_fail = autopilot_retry_on_fail

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Answer confident ask_user calls inline; defer the rest."""
        try:
            messages = state["messages"] if state is not None else None
        except (KeyError, TypeError):
            return None
        if not messages:
            return None
        last_ai_msg = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage)), None)
        if not last_ai_msg or not last_ai_msg.tool_calls:
            return None

        gated = [
            (idx, tc)
            for idx, tc in enumerate(last_ai_msg.tool_calls)
            if tc.get("name") == _ASK_USER_TOOL
        ]
        if not gated:
            return None

        ctx = AutoModeContext.from_runtime(runtime, default_mode=self._default_mode)
        view = self._loop_view(ctx)
        if view is None:
            return None
        # Veritas leg activates in auto mode, or in bypass mode (defers
        # render the honest bypass sentinel). The validation leg below runs
        # in EVERY mode: malformed ask_user args must never reach the real
        # tool — its interrupt is uncapturable and terminates the step
        # stream.
        veritas_leg = ctx.bypass or ctx.clarification_mode == "auto"

        outcomes: list[tuple[int, Any, _GateOutcome]] = []
        for idx, tc in gated:
            questions = self._extract_questions(tc)
            if not questions:
                outcomes.append((idx, tc, _GateOutcome(None, None, None, malformed=True)))
                continue
            if veritas_leg:
                outcomes.append((idx, tc, await self._evaluate(tc, questions, ctx, view)))
            else:
                # Non-auto mode: a valid ask_user passes through to the
                # native human interrupt (manual relay / plan review).
                outcomes.append((idx, tc, _GateOutcome(None, None, None)))

        # Confident outcomes resolve inline; inline-retry outcomes never reach
        # the interrupt. Remaining deferred outcomes bundle into one gate
        # interrupt — unless bypass mode routes them to the retry sentinel.
        deferred = [
            (idx, tc, o)
            for idx, tc, o in outcomes
            if not o.confident and not o.inline_retry and o.questions
        ]
        answers_by_idx: dict[int, str] = {}
        if deferred and not ctx.bypass:
            payload = {
                "type": INTERRUPT_TYPE_ASK_USER,
                "questions": [q for _, _, o in deferred for q in o.questions],
                "gate_deferred": True,
                "gate_deferred_kind": next((o.kind for _, _, o in deferred if o.kind), "explicit"),
            }
            resumed = interrupt(payload)
            self._distribute_answers(deferred, resumed, answers_by_idx)

        revised: list[Any] = []
        artificial: list[ToolMessage] = []
        changed = False
        outcome_by_idx = {idx: (tc, o) for idx, tc, o in outcomes}
        for idx, tc in enumerate(last_ai_msg.tool_calls):
            entry = outcome_by_idx.get(idx)
            if entry is None:
                revised.append(tc)
                continue
            _, outcome = entry
            if outcome.malformed:
                artificial.append(
                    ToolMessage(
                        content=(
                            "ask_user was called with malformed or empty "
                            "questions; the call was not executed. Re-emit "
                            "the tool call with at least one valid question."
                        ),
                        name=_ASK_USER_TOOL,
                        tool_call_id=tc["id"],
                        status="error",
                    )
                )
                self._notify_unanswered(outcome, "malformed", ctx)
                changed = True
                continue
            if outcome.confident:
                content = self._render_answers(outcome.questions, list(outcome.result.answers))
                artificial.append(
                    ToolMessage(
                        content=content,
                        name=_ASK_USER_TOOL,
                        tool_call_id=tc["id"],
                    )
                )
                self._notify_answered(outcome)
                changed = True
                continue
            if outcome.inline_retry:
                # B-a: bypass renders the honest bypass sentinel; the
                # autopilot opt-in retry renders the deferred sentinel.
                # Neither pretends to be an answer.
                sentinel = _BYPASS_SENTINEL if ctx.bypass else _RETRY_SENTINEL
                content = self._render_answers(
                    outcome.questions, [sentinel] * len(outcome.questions)
                )
                artificial.append(
                    ToolMessage(
                        content=content,
                        name=_ASK_USER_TOOL,
                        tool_call_id=tc["id"],
                    )
                )
                self._notify_unanswered(outcome, "bypass" if ctx.bypass else "autopilot_retry", ctx)
                changed = True
                continue
            if idx in answers_by_idx:
                artificial.append(
                    ToolMessage(
                        content=answers_by_idx[idx],
                        name=_ASK_USER_TOOL,
                        tool_call_id=tc["id"],
                    )
                )
                changed = True
                continue
            # Interrupted but no answer slice — treat as dismissal.
            if outcome.questions:
                artificial.append(
                    ToolMessage(
                        content="Clarification dismissed without an answer. Decide how to proceed.",
                        name=_ASK_USER_TOOL,
                        tool_call_id=tc["id"],
                    )
                )
                self._notify_unanswered(outcome, "dismissed", ctx)
                changed = True
                continue
            revised.append(tc)

        if not changed and not artificial:
            return None
        last_ai_msg.tool_calls = revised
        return {"messages": [last_ai_msg, *artificial]}

    async def _evaluate(
        self,
        tool_call: Any,
        questions: list[dict[str, Any]],
        ctx: AutoModeContext,
        view: LoopStateView,
    ) -> _GateOutcome:
        """Run veritas inline for one ask_user call (questions pre-parsed)."""
        request = ClarificationRequest(
            questions=tuple(questions),
            origin_node=ORIGIN_EXECUTE,
            origin_interrupt_id=f"gate:{tool_call.get('id') or 'ask_user'}",
            loop_state=view,
        )
        try:
            result = await self._veritas_answer(
                request,
                thread_id=str(ctx.configurable.get("thread_id") or "") or None,
                loop_id=str(ctx.configurable.get(_LOOP_ID_KEY) or "") or None,
            )
        except Exception:
            logger.warning(
                "[ask_user_gate] veritas call failed; falling back to station path",
                exc_info=True,
            )
            # Bypass or autopilot opt-in: honest inline sentinel; else
            # interrupt so the outcome parks for the human.
            if ctx.bypass or (not ctx.human_attached and self._autopilot_retry_on_fail):
                return _GateOutcome(
                    questions,
                    None,
                    "structured_output_failed",
                    failed=True,
                    inline_retry=True,
                )
            return _GateOutcome(questions, None, "structured_output_failed", failed=True)

        kind = self._classify(result)
        if kind is None:
            answers = [str(a).strip() for a in result.answers]
            if not answers or any(not a for a in answers):
                return _GateOutcome(questions, None, "explicit")
            return _GateOutcome(questions, result, None)

        # Defer / failure: bypass or autopilot opt-in renders the honest
        # inline sentinel; anything else defers (park-capable) for the human.
        if ctx.bypass or (not ctx.human_attached and self._autopilot_retry_on_fail):
            return _GateOutcome(
                questions,
                None,
                kind,
                failed=kind == "structured_output_failed",
                inline_retry=True,
            )
        return _GateOutcome(questions, None, kind)

    def _classify(self, result: VeritasAnswerSchema) -> str | None:
        """Resolve a veritas result to a defer kind, or `None` to accept."""
        if result.defer:
            if result.rationale.startswith(_FORCED_DEFER_PREFIX_STRUCTURED):
                return "structured_output_failed"
            if result.rationale == _FORCED_DEFER_RATIONALE_QUESTION:
                return "answer_was_question"
            return "explicit"
        if result.confidence < self._min_confidence:
            return "low_confidence"
        return None

    @staticmethod
    def _extract_questions(tool_call: Any) -> list[dict[str, Any]] | None:
        """Parse the ask_user tool call's questions via the tool's coercion."""
        from soothe.coreagent.tools.ask_user import _AskUserArgs

        args = tool_call.get("args") or {}
        if not isinstance(args, dict):
            return None
        try:
            parsed = _AskUserArgs.model_validate(dict(args))
        except Exception:
            logger.warning("[ask_user_gate] unparseable ask_user args; passing through")
            return None
        return [q.model_dump() for q in parsed.questions]

    @staticmethod
    def _render_answers(questions: list[dict[str, Any]], answers: list[str]) -> str:
        """Render the synthetic ToolMessage exactly like the tool does."""
        from soothe.coreagent.tools.ask_user import _format_answers

        return _format_answers(questions, {"answers": answers})

    @staticmethod
    def _loop_view(ctx: AutoModeContext) -> LoopStateView | None:
        """Read the per-step LoopStateView from configurable."""
        view = ctx.configurable.get(_LOOP_VIEW_KEY)
        return view if isinstance(view, LoopStateView) else None

    def _distribute_answers(
        self,
        deferred: list[tuple[int, Any, _GateOutcome]],
        resumed: Any,
        answers_by_idx: dict[int, str],
    ) -> None:
        """Map the resume payload's answers back onto the deferred calls."""
        raw = resumed.get("answers", resumed) if isinstance(resumed, dict) else resumed
        if isinstance(raw, str):
            flat: list[str] = [raw]
        elif isinstance(raw, list):
            flat = [str(a) for a in raw]
        else:
            flat = []
        if not flat:
            return
        pos = 0
        for idx, _, outcome in deferred:
            n = len(outcome.questions)
            if not n:
                continue
            # Broadcast a single answer across all questions (station parity).
            if len(flat) == 1:
                slice_ = flat * n
            else:
                slice_ = flat[pos : pos + n]
                # Missing slots render an explicit no-answer marker — never
                # a fabricated duplicate of the last answer (IG-LOOP-018).
                while len(slice_) < n:
                    slice_.append(_NO_ANSWER_SENTINEL)
            pos += n
            answers_by_idx[idx] = self._render_answers(outcome.questions, slice_)

    def _notify_answered(self, outcome: _GateOutcome) -> None:
        """Log and stream-emit an inline answer for history accounting."""
        preview = truncate_text(
            " | ".join(q.get("question", "") for q in outcome.questions),
            limit=160,
        )
        logger.info(
            "[ask_user_gate] answered inline (%d question(s), confidence=%.2f): %s",
            len(outcome.questions),
            outcome.result.confidence,
            preview,
        )
        try:
            from langgraph.config import get_stream_writer

            writer = get_stream_writer()
            if callable(writer):
                writer(
                    {
                        "type": CLARIFICATION_AUTO_ANSWERED,
                        "questions": outcome.questions,
                        "answers": list(outcome.result.answers),
                        "confidence": outcome.result.confidence,
                        "source": "veritas",
                    }
                )
        except Exception:  # noqa: BLE001 — observability only, never fatal
            pass

    def _notify_unanswered(self, outcome: _GateOutcome, kind: str, ctx: AutoModeContext) -> None:
        """Log and stream-emit a non-answer termination for observability."""
        preview = truncate_text(
            " | ".join(q.get("question", "") for q in outcome.questions),
            limit=160,
        )
        logger.info(
            "[ask_user_gate] ask_user terminated without an answer (kind=%s, "
            "mode=%s, bypass=%s): %s",
            kind,
            ctx.clarification_mode,
            ctx.bypass,
            preview,
        )
        try:
            from langgraph.config import get_stream_writer

            writer = get_stream_writer()
            if callable(writer):
                writer(
                    {
                        "type": CLARIFICATION_UNANSWERED,
                        "kind": kind,
                        "mode": ctx.clarification_mode,
                        "bypass": ctx.bypass,
                        "questions": outcome.questions,
                    }
                )
        except Exception:  # noqa: BLE001 — observability only, never fatal
            pass


__all__ = ["AskUserGateMiddleware"]
