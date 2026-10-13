"""Unit tests for TypeSafe scenario classification with LLM fallback."""

from __future__ import annotations

from typing import Any

import pytest

from soothe.sloop.engine.completion import scenario_typesafe as ts_mod
from soothe.sloop.engine.completion.scenario_typesafe import classify_scenario_typesafe

langchain_typesafe = pytest.importorskip("langchain_typesafe")
from langchain_typesafe.types import ChoiceAnswer, ClassifierResponse, Usage  # noqa: E402


def _answer(choice: str, probabilities: dict[str, float], confidence: float) -> Any:
    return ChoiceAnswer(
        type="choice", choice=choice, probabilities=probabilities, confidence=confidence
    )


def _response(choice: str, confidence: float) -> ClassifierResponse:
    probabilities = {
        choice: 0.9,
        "general_summary" if choice != "general_summary" else "analysis_report": 0.1,
    }
    return ClassifierResponse(
        model="nanojev:Qwen/Qwen3-0.6B",
        request_id=None,
        answers={"scenario": _answer(choice, probabilities, confidence)},
        usage=Usage(),
    )


class _FakeClassifier:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.states: list[Any] = []

    async def ainvoke(self, state: Any) -> Any:
        self.states.append(state)
        if self._error is not None:
            raise self._error
        return self._response


class _CfgStub:
    def __init__(self, **over: Any) -> None:
        from soothe_nano.config import ClassifierConfig

        self.classifier = ClassifierConfig(enabled=True, **over)

    def classifier_provider_kwargs(self) -> tuple[str, dict[str, Any]]:
        return (
            "typesafe",
            {
                "base_url": "http://127.0.0.1:8767",
                "api_key": "local-nanojev",
                "model": "jev-latest",
                "timeout": 2.0,
                "max_states_per_request": 32,
            },
        )


def _patch(monkeypatch: pytest.MonkeyPatch, fake: _FakeClassifier) -> None:
    monkeypatch.setattr(ts_mod, "build_typesafe_classifier", lambda _cfg: fake)


_EXEC_SUMMARY = {
    "total_steps": 3,
    "successful_steps": 3,
    "step_types": ["tool", "tool", "tool"],
    "tools_used": ["grep", "read_file"],
    "evidence_volume": 5000,
}


def _classify(**over: Any) -> Any:
    return classify_scenario_typesafe(
        goal="analyze the codebase architecture",
        intent_type="agentic",
        task_complexity="complex",
        execution_summary=_EXEC_SUMMARY,
        soothe_config=_CfgStub(**over),
    )


# ---------------------------------------------------------------------------
# Trusted verdicts
# ---------------------------------------------------------------------------


class TestTrusted:
    @pytest.mark.asyncio
    async def test_confident_known_scenario_returns_classification(self, monkeypatch) -> None:
        fake = _FakeClassifier(_response("code_architecture_design", 0.93))
        _patch(monkeypatch, fake)
        result = await _classify()
        assert result is not None
        assert result.scenario == "code_architecture_design"
        assert result.sections == []

    @pytest.mark.asyncio
    async def test_confident_research_synthesis(self, monkeypatch) -> None:
        fake = _FakeClassifier(_response("research_synthesis", 0.95))
        _patch(monkeypatch, fake)
        result = await _classify()
        assert result is not None
        assert result.scenario == "research_synthesis"

    @pytest.mark.asyncio
    async def test_state_contains_goal_and_execution_summary(self, monkeypatch) -> None:
        fake = _FakeClassifier(_response("analysis_report", 0.91))
        _patch(monkeypatch, fake)
        await _classify()
        state = fake.states[0]["state"]
        assert "analyze the codebase architecture" in state["goal"]
        assert "total_steps" in state["execution_summary"]
        assert state["intent_type"] == "agentic"
        assert state["task_complexity"] == "complex"


# ---------------------------------------------------------------------------
# Suppression bar (scenario selection suppresses scratchpad LLM call)
# ---------------------------------------------------------------------------


class TestSuppressionBar:
    @pytest.mark.asyncio
    async def test_below_suppression_bar_falls_back(self, monkeypatch) -> None:
        """Trusted (>= min_confidence) but below suppress_min_confidence:
        suppressing the scratchpad LLM call needs a higher bar."""
        _patch(monkeypatch, _FakeClassifier(_response("code_architecture_design", 0.82)))
        assert await _classify() is None

    @pytest.mark.asyncio
    async def test_at_suppression_bar_passes(self, monkeypatch) -> None:
        _patch(monkeypatch, _FakeClassifier(_response("tutorial_guide", 0.90)))
        result = await _classify()
        assert result is not None
        assert result.scenario == "tutorial_guide"


# ---------------------------------------------------------------------------
# Untrusted / fallback
# ---------------------------------------------------------------------------


class TestFallback:
    @pytest.mark.asyncio
    async def test_low_confidence_falls_back(self, monkeypatch) -> None:
        _patch(monkeypatch, _FakeClassifier(_response("analysis_report", 0.4)))
        assert await _classify() is None

    @pytest.mark.asyncio
    async def test_near_tie_falls_back(self, monkeypatch) -> None:
        response = _response("analysis_report", 0.99)
        response.answers["scenario"] = _answer(
            "analysis_report", {"analysis_report": 0.51, "research_synthesis": 0.49}, 0.99
        )
        _patch(monkeypatch, _FakeClassifier(response))
        assert await _classify() is None

    @pytest.mark.asyncio
    async def test_transport_error_falls_back(self, monkeypatch) -> None:
        _patch(monkeypatch, _FakeClassifier(error=RuntimeError("connection refused")))
        assert await _classify() is None

    @pytest.mark.asyncio
    async def test_disabled_config_returns_none(self) -> None:
        from soothe_nano.config import ClassifierConfig

        class _Disabled:
            classifier = ClassifierConfig(enabled=False)

        result = await classify_scenario_typesafe(
            goal="g",
            intent_type="agentic",
            task_complexity=None,
            execution_summary=_EXEC_SUMMARY,
            soothe_config=_Disabled(),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_none_config_returns_none(self) -> None:
        result = await classify_scenario_typesafe(
            goal="g",
            intent_type="agentic",
            task_complexity=None,
            execution_summary=_EXEC_SUMMARY,
            soothe_config=None,
        )
        assert result is None
