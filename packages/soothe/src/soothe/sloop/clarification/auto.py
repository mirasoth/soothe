"""Auto-mode clarification policy backed by the veritas subagent."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection

from soothe.sloop.clarification.protocol import (
    ClarificationAnswer,
    ClarificationDeferredError,
    ClarificationOrigin,
    ClarificationPolicy,
    ClarificationRequest,
    DeferKind,
)
from soothe.subagents.veritas.schemas import VeritasAnswerSchema

logger = logging.getLogger(__name__)

VeritasAnswerFn = Callable[[ClarificationRequest], Awaitable[VeritasAnswerSchema]]

_RATIONALE_PREFIX_STRUCTURED = "structured_output_failed"
_RATIONALE_MARKER_QUESTION = "answer_was_question"

# DeferKind → reason_template.
_REASON_TEMPLATES: dict[DeferKind, str] = {
    "structured_output_failed": "veritas structured output failed: {rationale}",
    "low_confidence": "veritas low confidence ({conf:.2f} < {min:.2f})",
    "explicit": "veritas explicit defer (confidence={conf:.2f})",
    "answer_was_question": "veritas answer was a question",
}

# Sentinel answer for autopilot retry — tells the LLM to try a different action.
_RETRY_SENTINEL = "(retry)"


class AutoClarificationPolicy:
    """Delegate clarifications to veritas; fall back on any failure.

    TUI fallback: routes to the interactive relay (auto→manual upgrade).
    Autopilot fallback: returns a synthetic retry answer prompting the LLM
    to try a different action.

    RFC-634: `tool_approval` requests skip veritas — `AutoModeMiddleware`
    resolves deterministic verdicts inline and only escalates to an
    interrupt when a human is attached, so these route straight to the
    interactive relay.
    """

    def __init__(
        self,
        veritas_answer: VeritasAnswerFn,
        *,
        min_confidence: float = 0.4,
        interactive_fallback: ClarificationPolicy | None = None,
        force_manual_origins: Collection[ClarificationOrigin] | None = None,
        degrade_to_manual_on_failure: bool = True,
        autopilot_retry_on_fail: bool = True,
    ) -> None:
        """Wire veritas and the fallback policy."""
        self._veritas_answer = veritas_answer
        self._min_confidence = min_confidence
        self._interactive_fallback = interactive_fallback
        self._force_manual_origins: frozenset[str] = frozenset(force_manual_origins or ())
        self._degrade_to_manual_on_failure = degrade_to_manual_on_failure
        self._autopilot_retry_on_fail = autopilot_retry_on_fail

    @property
    def min_confidence(self) -> float:
        """Minimum veritas confidence threshold for auto-answering."""
        return self._min_confidence

    @property
    def degrade_to_manual_on_failure(self) -> bool:
        """Whether to fall back to interactive policy on veritas failure."""
        return self._degrade_to_manual_on_failure

    @property
    def autopilot_retry_on_fail(self) -> bool:
        """Whether to emit a synthetic retry answer when veritas fails."""
        return self._autopilot_retry_on_fail

    @property
    def force_manual_origins(self) -> frozenset[str]:
        """Origins that must skip veritas and go straight to the human."""
        return self._force_manual_origins

    @property
    def interactive_fallback(self) -> ClarificationPolicy | None:
        """Interactive policy used when auto-answering is unavailable."""
        return self._interactive_fallback

    def requires_manual(self, origin_node: str) -> bool:
        """True when this origin must not be auto-answered by veritas."""
        return origin_node in self._force_manual_origins

    async def answer(self, request: ClarificationRequest) -> ClarificationAnswer:
        """Resolve a clarification request via relay, veritas, or fallback."""
        # --- tool-approval: middleware-originated, human-only (RFC-634) ---
        if request.origin_node == "tool_approval":
            return await self._answer_tool_approval(request)

        # --- force-manual origins: skip veritas, go straight to human ---
        if self.requires_manual(request.origin_node):
            return await self._answer_force_manual(request)

        # --- veritas LLM auto-answer ---
        return await self._answer_veritas(request)

    async def _answer_tool_approval(self, request: ClarificationRequest) -> ClarificationAnswer:
        """Route a middleware tool-approval interrupt to the human relay.

        `AutoModeMiddleware` already resolved every deterministic verdict
        inline; an interrupt reaching the station implies a human decision
        is wanted. Resume replays delegate without re-announcing (the card
        already exists). Autopilot (no human attached) is defensive — the
        middleware degrades safety escalations to instructive rejects before
        interrupting — and gets the synthetic retry answer.
        """
        if self._interactive_fallback is not None:
            announce = not request.metadata.get("resume_turn")
            logger.info(
                "[clarification] tool_approval interrupt from auto gate; routing to human relay"
            )
            return await self._delegate_to_fallback(request, announce=announce)
        if self._autopilot_retry_on_fail:
            logger.info("[clarification] tool_approval without human; autopilot retry")
            return self._build_retry_answer(request)
        raise ClarificationDeferredError(
            "tool_approval: no human attached and autopilot retry disabled",
            request,
            kind="explicit",
        )

    async def _answer_force_manual(self, request: ClarificationRequest) -> ClarificationAnswer:
        """Force-manual origins skip veritas entirely.

        `await_clarification` already emitted with `mode=manual`, so we
        call `answer()` directly (no re-announce).
        """
        if self._interactive_fallback is not None:
            logger.info(
                "[clarification] origin=%s requires manual confirmation; skipping veritas",
                request.origin_node,
            )
            return await self._interactive_fallback.answer(request)
        # No human — try autopilot retry, else hard defer.
        if self._autopilot_retry_on_fail:
            return self._build_retry_answer(request)
        raise ClarificationDeferredError(
            f"origin {request.origin_node} requires manual confirmation",
            request,
            kind="explicit",
        )

    async def _answer_veritas(self, request: ClarificationRequest) -> ClarificationAnswer:
        """Veritas LLM auto-answer with confidence-based fallback.

        RFC-635: requests carrying the `gate_deferred` marker already ran
        veritas inline (`AskUserGateMiddleware`) and deferred — skip the LLM
        call and route straight through the fallback ladder (human when
        attached, autopilot retry, hard defer).

        On any veritas failure (DeferKind is not None):
        - TUI: route to the interactive relay when
          `degrade_to_manual_on_failure` is True.
        - Autopilot: return a synthetic retry answer when
          `autopilot_retry_on_fail` is True, prompting the LLM to try a
          different action.
        - Otherwise: hard defer.
        """
        if request.metadata.get("gate_deferred"):
            return await self._answer_gate_deferred(request)

        result = await self._veritas_answer(request)
        kind = self._classify(result)

        if kind is not None:
            reason = self._reason_for(kind, result)
            # Path 1: TUI — degrade to manual on any failure.
            if self._degrade_to_manual_on_failure and self._interactive_fallback is not None:
                if kind == "structured_output_failed":
                    logger.warning(
                        "[veritas] structured output failed; falling back to interactive relay"
                    )
                else:
                    logger.info(
                        "[veritas] %s; degrading to interactive relay",
                        kind,
                    )
                return await self._delegate_to_fallback(request)

            # Path 2: Autopilot — synthetic retry so the LLM tries again.
            if self._autopilot_retry_on_fail and self._interactive_fallback is None:
                logger.info(
                    "[veritas] %s; autopilot retry (letting LLM try a different action)",
                    kind,
                )
                return self._build_retry_answer(request)

            # Path 3: hard defer.
            raise ClarificationDeferredError(reason, request, kind=kind)

        answers = tuple(str(a).strip() for a in result.answers)
        if not answers or any(not a for a in answers):
            # Empty answers — same fallback logic as above.
            reason = "veritas returned empty answer(s)"
            if self._degrade_to_manual_on_failure and self._interactive_fallback is not None:
                logger.info("[veritas] empty answers; degrading to interactive relay")
                return await self._delegate_to_fallback(request)
            if self._autopilot_retry_on_fail and self._interactive_fallback is None:
                logger.info("[veritas] empty answers; autopilot retry")
                return self._build_retry_answer(request)
            raise ClarificationDeferredError(reason, request, kind="explicit")

        return ClarificationAnswer(
            answers=answers,
            source="veritas",
            confidence=result.confidence,
            defer=False,
            audit={"rationale": result.rationale},
        )

    async def _answer_gate_deferred(self, request: ClarificationRequest) -> ClarificationAnswer:
        """Route a gate-deferred question through the fallback ladder.

        `AskUserGateMiddleware` already ran veritas inline (RFC-635) — the
        gate defers only for genuine "I don't know" verdicts, so the station
        goes straight to the human relay when one is attached, the autopilot
        retry sentinel when enabled, or a hard defer (park) otherwise.
        """
        kind = str(request.metadata.get("gate_deferred_kind") or "explicit")
        if self._interactive_fallback is not None:
            logger.info("[clarification] ask_user gate deferred (kind=%s); routing to human", kind)
            return await self._delegate_to_fallback(request)
        if self._autopilot_retry_on_fail:
            logger.info("[clarification] ask_user gate deferred (kind=%s); autopilot retry", kind)
            return self._build_retry_answer(request)
        raise ClarificationDeferredError(
            f"ask_user gate deferred (kind={kind})",
            request,
            kind=kind,  # type: ignore[arg-type]
        )

    def _build_retry_answer(self, request: ClarificationRequest) -> ClarificationAnswer:
        """Build a synthetic retry answer for autopilot mode.

        The sentinel `"(retry)"` is fed back to the CoreAgent as the tool
        result for the ask_user / tool_approval interrupt, prompting the LLM
        to try a different action instead of parking the goal.
        """
        n_questions = len(request.questions) or 1
        return ClarificationAnswer(
            answers=tuple([_RETRY_SENTINEL] * n_questions),
            source="retry",
            confidence=0.0,
            audit={"reason": "veritas failed; autopilot retry"},
        )

    async def _delegate_to_fallback(
        self,
        request: ClarificationRequest,
        *,
        announce: bool = True,
    ) -> ClarificationAnswer:
        """Route to the interactive relay with auto→manual re-announce.

        Prefers `answer_as_manual_fallback` (mode=manual emit) over bare
        `answer()`. `announce=False` for resume replays, where the card
        already exists.
        """
        fallback = self._interactive_fallback
        upgrade = getattr(fallback, "answer_as_manual_fallback", None)
        if callable(upgrade):
            return await upgrade(request, announce=announce)
        return await fallback.answer(request)

    def _classify(self, result: VeritasAnswerSchema) -> DeferKind | None:
        """Resolve a veritas result to a :data:`DeferKind`, or `None` to accept."""
        if result.defer:
            if result.rationale.startswith(_RATIONALE_PREFIX_STRUCTURED):
                return "structured_output_failed"
            if result.rationale == _RATIONALE_MARKER_QUESTION:
                return "answer_was_question"
            return "explicit"
        if result.confidence < self._min_confidence:
            return "low_confidence"
        return None

    def _reason_for(self, kind: DeferKind, result: VeritasAnswerSchema) -> str:
        """Build the defer reason message for the given kind."""
        template = _REASON_TEMPLATES.get(kind, "veritas deferred")
        return template.format(
            rationale=result.rationale,
            conf=result.confidence,
            min=self._min_confidence,
        )


__all__ = ["AutoClarificationPolicy", "VeritasAnswerFn"]
