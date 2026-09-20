"""Interactive (TUI relay) clarification policy."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.types import interrupt

from soothe.sloop.clarification.interrupt_kinds import INTERRUPT_TYPE_CLARIFICATION
from soothe.sloop.clarification.origins import (
    ORIGIN_PLAN_MODE_REVIEW,
    ORIGIN_TOOL_APPROVAL,
)
from soothe.sloop.clarification.protocol import (
    ClarificationAnswer,
    ClarificationDeferredError,
    ClarificationRequest,
    merge_answer_audit,
)

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

logger = logging.getLogger(__name__)


class InteractiveClarificationPolicy:
    """Relay clarifications to a human via the TUI with a durable pause.

    RFC-634: tool-approval requests arrive from `AutoModeMiddleware` with
    the escalated safety `rule_id` in `request.metadata["escalated_rule_id"]`
    (deterministic verdicts never reach the station). The rule id is stamped
    onto the human answer's audit so `node_execute` records a rule-level
    allowlist override.
    """

    def __init__(
        self,
        emit: EmitFn | None = None,
    ) -> None:
        """Wire the emit callback."""
        self._emit = emit
        self._escalated_rule_id: str | None = None

    def bind_emit(self, emit: EmitFn) -> None:
        """Attach the runtime emit callback."""
        self._emit = emit

    async def answer(self, request: ClarificationRequest) -> ClarificationAnswer:
        """Pause for a human answer.  `await_clarification` already emitted."""
        self._escalated_rule_id = self._metadata_rule_id(request)
        answer = await self._answer(request, announce=False)
        return self._merge_escalated_rule_id(answer)

    async def answer_as_manual_fallback(
        self, request: ClarificationRequest, *, announce: bool = True
    ) -> ClarificationAnswer:
        """Re-announce as `mode=manual` then pause (auto→manual upgrade).

        The TUI only mounts the interactive card for `mode=manual` emits;
        resume replays pass `announce=False` since the card already exists.
        """
        self._escalated_rule_id = self._metadata_rule_id(request)
        answer = await self._answer(request, announce=announce)
        return self._merge_escalated_rule_id(answer)

    @staticmethod
    def _metadata_rule_id(request: ClarificationRequest) -> str | None:
        """Safety rule id attached by the middleware gate, if any."""
        raw = request.metadata.get("escalated_rule_id") if request.metadata else None
        return str(raw) if raw else None

    def _merge_escalated_rule_id(self, answer: ClarificationAnswer) -> ClarificationAnswer:
        """Stamp the escalated safety rule_id onto a human answer's audit."""
        rule_id = self._escalated_rule_id
        self._escalated_rule_id = None
        if not rule_id:
            return answer
        return merge_answer_audit(answer, escalated_rule_id=rule_id)

    async def _answer(
        self, request: ClarificationRequest, *, announce: bool
    ) -> ClarificationAnswer:
        if announce and self._emit is not None:
            await self._emit(
                "clarification_requested",
                {
                    "questions": list(request.questions),
                    "origin_node": request.origin_node,
                    "mode": "manual",
                },
            )

        payload = interrupt(
            {
                "type": INTERRUPT_TYPE_CLARIFICATION,
                "interrupt_id": request.origin_interrupt_id,
                "questions": list(request.questions),
            }
        )

        answers = self._extract_answers(
            payload, expected=len(request.questions), origin=request.origin_node
        )
        if answers is None:
            raise ClarificationDeferredError(
                "operator dismissed clarification (no answer)",
                request,
                kind="explicit",
            )

        return ClarificationAnswer(
            answers=tuple(answers),
            source="human",
        )

    def try_static_answer(self, request: ClarificationRequest) -> ClarificationAnswer | None:
        """Statically resolve a request without a human pause, or `None`.

        RFC-634 removed the station-side tool-approval pre-filter (the
        `AutoModeMiddleware` gate resolves deterministic verdicts inline),
        so every request reaching this policy needs its own human pause.
        """
        del request
        return None

    @staticmethod
    def _extract_answers(
        payload: Any, *, expected: int, origin: str | None = None
    ) -> list[str] | None:
        stripped = InteractiveClarificationPolicy._normalize_payload(payload)
        if stripped is None:
            return None

        # Action-selector origins (plan-mode review, tool approval) send
        # [action, optional-comment]; only the action field (index 0) is
        # required — the trailing comment is legitimately blank (approve) or
        # carries edit/refine args. Treat a non-empty action as answered and
        # pad / truncate to the expected length instead of dismissing the
        # whole answer (the TUI always submits both slots).
        if (
            origin in (ORIGIN_PLAN_MODE_REVIEW, ORIGIN_TOOL_APPROVAL)
            and expected in (1, 2)
            and stripped
            and stripped[0]
        ):
            # Preserve the comment (index 1) even when expected=1 — the host
            # decoder (parse_plan_review_answers / _answer_to_decision) reads
            # it from answer.answers[1].
            if len(stripped) == 1:
                stripped.append("")
            # Don't truncate — return both [action, comment].
            return stripped

        if any(not a for a in stripped):
            return None

        if len(stripped) == expected:
            return stripped
        if len(stripped) == 1 and expected > 1:
            # broadcast single answer when caller didn't split per-question
            return stripped * expected
        return None

    @staticmethod
    def _normalize_payload(payload: Any) -> list[str] | None:
        """Extract a raw answer list from an interrupt payload, or `None`."""
        if payload is None:
            return None
        if isinstance(payload, str):
            raw = [payload]
        elif isinstance(payload, dict):
            val = payload.get("answers", payload.get("answer"))
            if val is None:
                return None
            if isinstance(val, str):
                raw = [val]
            elif isinstance(val, list):
                raw = [str(a) for a in val]
            else:
                return None
        elif isinstance(payload, list):
            raw = [str(a) for a in payload]
        else:
            return None
        return [a.strip() for a in raw]


__all__ = ["EmitFn", "InteractiveClarificationPolicy"]
