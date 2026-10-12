"""Unit tests for TypeSafe decompose finish-vs-split decision."""

from __future__ import annotations

from typing import Any

import pytest

from soothe.sloop.decompose import decompose_typesafe as ts_mod
from soothe.sloop.decompose.decompose_typesafe import decide_finish_vs_split_typesafe

langchain_typesafe = pytest.importorskip("langchain_typesafe")
from langchain_typesafe.types import ChoiceAnswer, ClassifierResponse, Usage  # noqa: E402


def _answer(choice: str, probabilities: dict[str, float], confidence: float) -> Any:
    return ChoiceAnswer(
        type="choice", choice=choice, probabilities=probabilities, confidence=confidence
    )


def _response(choice: str, confidence: float) -> ClassifierResponse:
    other = "split" if choice == "finish" else "finish"
    probabilities = {choice: 0.9, other: 0.1}
    return ClassifierResponse(
        model="nanojev:Qwen/Qwen3-0.6B",
        request_id=None,
        answers={"finish_split": _answer(choice, probabilities, confidence)},
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


def _decide(**over: Any) -> Any:
    return decide_finish_vs_split_typesafe(
        step_description="Fix the login bug in auth.py",
        user_goal="Fix all critical bugs in the authentication module",
        soothe_config=_CfgStub(**over),
    )


# ---------------------------------------------------------------------------
# Trusted verdicts
# ---------------------------------------------------------------------------


class TestTrusted:
    @pytest.mark.asyncio
    async def test_confident_finish_returns_finish(self, monkeypatch) -> None:
        _patch(monkeypatch, _FakeClassifier(_response("finish", 0.93)))
        result = await _decide()
        assert result == "finish"

    @pytest.mark.asyncio
    async def test_confident_split_returns_split(self, monkeypatch) -> None:
        _patch(monkeypatch, _FakeClassifier(_response("split", 0.95)))
        result = await _decide()
        assert result == "split"

    @pytest.mark.asyncio
    async def test_state_contains_step_and_goal(self, monkeypatch) -> None:
        fake = _FakeClassifier(_response("finish", 0.91))
        _patch(monkeypatch, fake)
        await _decide()
        state = fake.states[0]["state"]
        assert "Fix the login bug" in state["step_description"]
        assert "Fix all critical bugs" in state["goal"]


# ---------------------------------------------------------------------------
# Suppression bar (finish suppresses decompose_task tool)
# ---------------------------------------------------------------------------


class TestSuppressionBar:
    @pytest.mark.asyncio
    async def test_finish_below_suppression_bar_falls_back(self, monkeypatch) -> None:
        """Trusted but below suppress_min_confidence: suppressing the decompose
        tool needs a higher bar."""
        _patch(monkeypatch, _FakeClassifier(_response("finish", 0.82)))
        assert await _decide() is None

    @pytest.mark.asyncio
    async def test_split_below_suppression_bar_still_returns(self, monkeypatch) -> None:
        """Split is fail-safe direction — no suppression bar needed."""
        _patch(monkeypatch, _FakeClassifier(_response("split", 0.65)))
        result = await _decide()
        assert result == "split"


# ---------------------------------------------------------------------------
# Untrusted / fallback
# ---------------------------------------------------------------------------


class TestFallback:
    @pytest.mark.asyncio
    async def test_low_confidence_falls_back(self, monkeypatch) -> None:
        _patch(monkeypatch, _FakeClassifier(_response("finish", 0.4)))
        assert await _decide() is None

    @pytest.mark.asyncio
    async def test_near_tie_falls_back(self, monkeypatch) -> None:
        response = _response("finish", 0.99)
        response.answers["finish_split"] = _answer("finish", {"finish": 0.51, "split": 0.49}, 0.99)
        _patch(monkeypatch, _FakeClassifier(response))
        assert await _decide() is None

    @pytest.mark.asyncio
    async def test_transport_error_falls_back(self, monkeypatch) -> None:
        _patch(monkeypatch, _FakeClassifier(error=RuntimeError("connection refused")))
        assert await _decide() is None

    @pytest.mark.asyncio
    async def test_disabled_config_returns_none(self) -> None:
        from soothe_nano.config import ClassifierConfig

        class _Disabled:
            classifier = ClassifierConfig(enabled=False)

        result = await decide_finish_vs_split_typesafe(
            step_description="d",
            user_goal="g",
            soothe_config=_Disabled(),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_none_config_returns_none(self) -> None:
        result = await decide_finish_vs_split_typesafe(
            step_description="d",
            user_goal="g",
            soothe_config=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_build_classifier_exception_returns_none(self, monkeypatch) -> None:
        monkeypatch.setattr(
            ts_mod,
            "build_typesafe_classifier",
            lambda _cfg: (_ for _ in ()).throw(ValueError("boom")),
        )
        assert await _decide() is None
