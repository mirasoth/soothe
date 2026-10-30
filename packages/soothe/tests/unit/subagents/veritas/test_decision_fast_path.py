"""Unit tests for the veritas decision fast path (RFC-LOOP-030).

Jev first, LLM second: approval-shaped requests are classified by the
calibrated decision model; trusted allow/reject resolves inline with
`source="decision_model"`, trusted escalate and everything unusable falls
through to the LLM leg (escalate with the risk assessment embedded).
"""

from __future__ import annotations

from typing import Any

import pytest

from soothe.sloop.clarification.protocol import ClarificationRequest
from soothe.subagents.veritas import implementation as veritas_impl
from soothe.subagents.veritas.decision_model import RiskVerdict
from soothe.subagents.veritas.schemas import VeritasAnswerSchema


def _loop_view() -> Any:
    from soothe.sloop.clarification.protocol import LoopStateView

    return LoopStateView(
        goal_id="g",
        goal_description="refactor auth",
        user_request="",
        iteration=0,
        intent_classification=None,
        plan_summary=None,
        recent_step_outputs=(),
        workspace_summary=None,
        active_skills=(),
        active_mcp_servers=(),
    )


def _request(*, with_subject: bool = True, questions: int = 1) -> ClarificationRequest:
    metadata: dict[str, Any] = {}
    if with_subject:
        metadata["action_requests"] = [
            {
                "name": "run_command",
                "args": {"command": "git push -f origin main"},
                "description": "tool approval",
            }
        ]
    return ClarificationRequest(
        questions=tuple(f"Question {i}?" for i in range(questions)),
        origin_node="tool_approval",
        origin_interrupt_id="test:1",
        loop_state=_loop_view(),
        metadata=metadata,
    )


class _StubClassifier:
    def __init__(self, verdict: RiskVerdict) -> None:
        self._verdict = verdict
        self.calls = 0

    async def classify(self, items: list[Any]) -> list[RiskVerdict]:
        self.calls += 1
        return [self._verdict for _ in items]


def _verdict(band: str, confidence: float = 0.95) -> RiskVerdict:
    return RiskVerdict(
        band=band,
        confidence=confidence,
        probabilities={band: 0.9},
        reason=f"stub {band}",
    )


class _NeverModel:
    """A chat model that must never be reached (fast path resolves inline)."""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        self.calls += 1
        raise AssertionError("LLM must not be called when the fast path resolves")


class _RecordingStructured:
    """Patches `ainvoke_structured_traced` and records the user prompt."""

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.user_prompts: list[str] = []

    async def __call__(self, model: Any, messages: list[Any], **kwargs: Any) -> dict[str, Any]:
        self.user_prompts.append(str(messages[-1].content))
        return self.answer


_LLM_ANSWER = {
    "defer": False,
    "answers": ["proceed with care"],
    "confidence": 0.8,
    "rationale": "contextual",
}


class TestFastPathResolution:
    @pytest.mark.asyncio
    async def test_allow_resolves_inline_without_llm(self) -> None:
        clf = _StubClassifier(_verdict("allow"))
        model = _NeverModel()
        result = await veritas_impl.answer(
            _request(),
            model=model,
            decision_model=clf,  # type: ignore[arg-type]
        )
        assert result.defer is False
        assert result.source == "decision_model"
        assert result.answers == ["approve"]
        assert clf.calls == 1
        assert model.calls == 0

    @pytest.mark.asyncio
    async def test_reject_resolves_inline_without_llm(self) -> None:
        clf = _StubClassifier(_verdict("reject"))
        model = _NeverModel()
        result = await veritas_impl.answer(
            _request(),
            model=model,
            decision_model=clf,  # type: ignore[arg-type]
        )
        assert result.source == "decision_model"
        assert result.answers == ["deny"]

    @pytest.mark.asyncio
    async def test_answers_padded_to_question_count(self) -> None:
        clf = _StubClassifier(_verdict("allow"))
        result = await veritas_impl.answer(
            _request(questions=3),
            model=_NeverModel(),
            decision_model=clf,  # type: ignore[arg-type]
        )
        assert result.answers == ["approve"] * 3
        assert result.answer_is_question == [False] * 3


class TestFallbackToLlm:
    @pytest.mark.asyncio
    async def test_no_subject_skips_classifier(self, monkeypatch) -> None:
        """Open question shapes (no action_requests) go straight to the LLM."""
        clf = _StubClassifier(_verdict("allow"))
        recorder = _RecordingStructured(_LLM_ANSWER)
        monkeypatch.setattr(veritas_impl, "ainvoke_structured_traced", recorder)
        result = await veritas_impl.answer(
            _request(with_subject=False),
            model=object(),
            decision_model=clf,  # type: ignore[arg-type]
        )
        assert clf.calls == 0
        assert result.source == "llm"
        assert result.answers == ["proceed with care"]

    @pytest.mark.asyncio
    async def test_untrusted_falls_back_without_hint(self, monkeypatch) -> None:
        clf = _StubClassifier(_verdict("untrusted", confidence=0.3))
        recorder = _RecordingStructured(_LLM_ANSWER)
        monkeypatch.setattr(veritas_impl, "ainvoke_structured_traced", recorder)
        result = await veritas_impl.answer(
            _request(),
            model=object(),
            decision_model=clf,  # type: ignore[arg-type]
        )
        assert clf.calls == 1
        assert result.source == "llm"
        assert "risk assessment" not in recorder.user_prompts[0]

    @pytest.mark.asyncio
    async def test_unavailable_falls_back(self, monkeypatch) -> None:
        clf = _StubClassifier(_verdict("unavailable"))
        monkeypatch.setattr(
            veritas_impl, "ainvoke_structured_traced", _RecordingStructured(_LLM_ANSWER)
        )
        result = await veritas_impl.answer(
            _request(),
            model=object(),
            decision_model=clf,  # type: ignore[arg-type]
        )
        assert result.source == "llm"

    @pytest.mark.asyncio
    async def test_escalate_falls_back_with_risk_hint(self, monkeypatch) -> None:
        """Trusted escalate deep-judges with the LLM, hint embedded."""
        clf = _StubClassifier(_verdict("escalate"))
        recorder = _RecordingStructured(_LLM_ANSWER)
        monkeypatch.setattr(veritas_impl, "ainvoke_structured_traced", recorder)
        result = await veritas_impl.answer(
            _request(),
            model=object(),
            decision_model=clf,  # type: ignore[arg-type]
        )
        assert clf.calls == 1
        assert result.source == "llm"
        assert "Calibrated risk assessment" in recorder.user_prompts[0]
        assert "escalate" in recorder.user_prompts[0]

    @pytest.mark.asyncio
    async def test_classifier_failure_falls_back(self, monkeypatch) -> None:
        class _Boom:
            async def classify(self, items: list[Any]) -> list[RiskVerdict]:
                raise RuntimeError("down")

        monkeypatch.setattr(
            veritas_impl, "ainvoke_structured_traced", _RecordingStructured(_LLM_ANSWER)
        )
        result = await veritas_impl.answer(
            _request(),
            model=object(),
            decision_model=_Boom(),  # type: ignore[arg-type]
        )
        assert result.source == "llm"


class TestSchemaSourceField:
    def test_default_source_is_llm(self) -> None:
        schema = VeritasAnswerSchema.model_validate(
            {"defer": False, "answers": ["x"], "confidence": 0.9, "rationale": "r"}
        )
        assert schema.source == "llm"

    def test_decision_model_source_round_trips(self) -> None:
        schema = VeritasAnswerSchema.model_validate(
            {
                "defer": False,
                "answers": ["approve"],
                "confidence": 0.95,
                "rationale": "r",
                "source": "decision_model",
            }
        )
        assert schema.source == "decision_model"
