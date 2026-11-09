"""Unit tests for TypeSafe report validation gate."""

from __future__ import annotations

from typing import Any

import pytest

from soothe.sloop.engine.completion import report_validation_typesafe as rv_mod
from soothe.sloop.engine.completion.report_validation_typesafe import (
    validate_report_typesafe,
)

langchain_typesafe = pytest.importorskip("langchain_typesafe")
from langchain_typesafe.types import ChoiceAnswer, ClassifierResponse, Usage  # noqa: E402


def _answer(choice: str, probabilities: dict[str, float], confidence: float) -> Any:
    return ChoiceAnswer(
        type="choice", choice=choice, probabilities=probabilities, confidence=confidence
    )


def _response(choice: str, confidence: float) -> ClassifierResponse:
    other = "invalid" if choice == "valid" else "valid"
    probabilities = {choice: confidence, other: 1 - confidence}
    return ClassifierResponse(
        model="nanojev:Qwen/Qwen3-0.6B",
        request_id=None,
        answers={"report_valid": _answer(choice, probabilities, confidence)},
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
    monkeypatch.setattr(rv_mod, "build_classifier", lambda _cfg: fake)


def _validate(**over: Any) -> Any:
    return validate_report_typesafe(
        user_goal="Design a world state model for the pipeline",
        step_output="## World State Design\n\nThe world state consists of...",
        step_kind="brainstorm",
        soothe_config=_CfgStub(**over),
    )


class TestTrusted:
    @pytest.mark.asyncio
    async def test_confident_valid_returns_true(self, monkeypatch) -> None:
        fake = _FakeClassifier(_response("valid", 0.93))
        _patch(monkeypatch, fake)
        result = await _validate()
        assert result is True

    @pytest.mark.asyncio
    async def test_confident_invalid_returns_false(self, monkeypatch) -> None:
        fake = _FakeClassifier(_response("invalid", 0.85))
        _patch(monkeypatch, fake)
        result = await _validate()
        assert result is False

    @pytest.mark.asyncio
    async def test_state_contains_goal_and_output(self, monkeypatch) -> None:
        fake = _FakeClassifier(_response("valid", 0.91))
        _patch(monkeypatch, fake)
        await _validate()
        state = fake.states[0]["state"]
        assert "world state" in state["goal"]
        assert "World State Design" in state["step_output"]
        assert state["step_kind"] == "brainstorm"


class TestSuppressionBar:
    @pytest.mark.asyncio
    async def test_valid_below_bar_returns_none(self, monkeypatch) -> None:
        fake = _FakeClassifier(_response("valid", 0.65))
        _patch(monkeypatch, fake)
        result = await _validate(suppress_min_confidence=0.8)
        assert result is None

    @pytest.mark.asyncio
    async def test_valid_at_bar_returns_true(self, monkeypatch) -> None:
        fake = _FakeClassifier(_response("valid", 0.80))
        _patch(monkeypatch, fake)
        result = await _validate(suppress_min_confidence=0.8)
        assert result is True

    @pytest.mark.asyncio
    async def test_invalid_below_suppress_bar_still_returns_false(self, monkeypatch) -> None:
        """invalid (fail-safe direction) does not require suppression bar — only trust."""
        fake = _FakeClassifier(_response("invalid", 0.75))
        _patch(monkeypatch, fake)
        result = await _validate(suppress_min_confidence=0.9)
        assert result is False


class TestUnavailable:
    @pytest.mark.asyncio
    async def test_classifier_error_returns_none(self, monkeypatch) -> None:
        fake = _FakeClassifier(error=RuntimeError("connection refused"))
        _patch(monkeypatch, fake)
        result = await _validate()
        assert result is None

    @pytest.mark.asyncio
    async def test_unknown_choice_returns_none(self, monkeypatch) -> None:
        fake = _FakeClassifier(_response("maybe", 0.90))
        _patch(monkeypatch, fake)
        result = await _validate()
        assert result is None

    @pytest.mark.asyncio
    async def test_disabled_config_returns_none(self) -> None:
        from soothe_nano.config import ClassifierConfig

        cfg = _CfgStub()
        cfg.classifier = ClassifierConfig(enabled=False)
        result = await validate_report_typesafe(
            user_goal="test",
            step_output="output",
            soothe_config=cfg,
        )
        assert result is None
