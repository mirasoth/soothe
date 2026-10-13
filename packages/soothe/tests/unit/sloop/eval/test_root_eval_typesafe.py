"""Unit tests for TypeSafe root Eval re-audit vs finalize decision."""

from __future__ import annotations

from typing import Any

import pytest

from soothe.sloop.stations.decompose import root_eval_typesafe as ts_mod
from soothe.sloop.stations.decompose.root_eval_typesafe import (
    decide_reaudit_vs_finalize_typesafe,
)

langchain_typesafe = pytest.importorskip("langchain_typesafe")
from langchain_typesafe.types import ChoiceAnswer, ClassifierResponse, Usage  # noqa: E402


def _answer(choice: str, probabilities: dict[str, float], confidence: float) -> Any:
    return ChoiceAnswer(
        type="choice", choice=choice, probabilities=probabilities, confidence=confidence
    )


def _response(choice: str, confidence: float) -> ClassifierResponse:
    probabilities = {choice: 0.9, "reaudit" if choice == "finalize" else "finalize": 0.1}
    return ClassifierResponse(
        model="nanojev:Qwen/Qwen3-0.6B",
        request_id=None,
        answers={"reaudit": _answer(choice, probabilities, confidence)},
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


_STEP_HISTORY = (
    "- S1 kind=action status=completed: Build feature\n"
    "  close_report={'goal_portion_complete': True, 'early_exit': False}\n"
    "  outcome={'type': 'tool', 'tool_name': 'edit_file'}"
)


def _decide(**over: Any) -> Any:
    return decide_reaudit_vs_finalize_typesafe(
        user_goal="Build a user authentication feature",
        step_history_table=_STEP_HISTORY,
        soothe_config=_CfgStub(**over),
    )


# ---------------------------------------------------------------------------
# Trusted verdicts
# ---------------------------------------------------------------------------


class TestTrusted:
    @pytest.mark.asyncio
    async def test_confident_finalize_returns_false(self, monkeypatch) -> None:
        """finalize → False (do NOT re-audit)."""
        fake = _FakeClassifier(_response("finalize", 0.93))
        _patch(monkeypatch, fake)
        result = await _decide()
        assert result is False

    @pytest.mark.asyncio
    async def test_confident_reaudit_returns_true(self, monkeypatch) -> None:
        """reaudit → True (DO re-audit)."""
        fake = _FakeClassifier(_response("reaudit", 0.85))
        _patch(monkeypatch, fake)
        result = await _decide()
        assert result is True

    @pytest.mark.asyncio
    async def test_state_contains_goal_and_history(self, monkeypatch) -> None:
        fake = _FakeClassifier(_response("finalize", 0.91))
        _patch(monkeypatch, fake)
        await _decide()
        state = fake.states[0]["state"]
        assert "authentication" in state["goal"]
        assert "Build feature" in state["step_history"]


# ---------------------------------------------------------------------------
# Suppression bar (finalize suppresses re-audit work)
# ---------------------------------------------------------------------------


class TestSuppressionBar:
    @pytest.mark.asyncio
    async def test_below_suppression_bar_falls_back(self, monkeypatch) -> None:
        """Trusted but below suppress_min_confidence: suppressing re-audit
        needs a higher bar."""
        _patch(monkeypatch, _FakeClassifier(_response("finalize", 0.82)))
        assert await _decide() is None

    @pytest.mark.asyncio
    async def test_at_suppression_bar_passes(self, monkeypatch) -> None:
        _patch(monkeypatch, _FakeClassifier(_response("finalize", 0.90)))
        result = await _decide()
        assert result is False


# ---------------------------------------------------------------------------
# Untrusted / fallback
# ---------------------------------------------------------------------------


class TestFallback:
    @pytest.mark.asyncio
    async def test_low_confidence_falls_back(self, monkeypatch) -> None:
        _patch(monkeypatch, _FakeClassifier(_response("finalize", 0.4)))
        assert await _decide() is None

    @pytest.mark.asyncio
    async def test_near_tie_falls_back(self, monkeypatch) -> None:
        response = _response("finalize", 0.99)
        response.answers["reaudit"] = _answer("finalize", {"finalize": 0.51, "reaudit": 0.49}, 0.99)
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

        result = await decide_reaudit_vs_finalize_typesafe(
            user_goal="test",
            step_history_table="history",
            soothe_config=_Disabled(),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_none_config_returns_none(self) -> None:
        result = await decide_reaudit_vs_finalize_typesafe(
            user_goal="test",
            step_history_table="history",
            soothe_config=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_build_error_falls_back(self, monkeypatch) -> None:
        """build_typesafe_classifier raising should not block."""
        monkeypatch.setattr(
            ts_mod,
            "build_typesafe_classifier",
            lambda _cfg: (_ for _ in ()).throw(RuntimeError("bad endpoint")),
        )
        assert await _decide() is None
