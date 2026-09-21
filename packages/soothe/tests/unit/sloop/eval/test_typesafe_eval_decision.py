"""Unit tests for TypeSafe goal-coverage decision with LLM fallback."""

from __future__ import annotations

from typing import Any

import pytest

from soothe.sloop.eval import typesafe_eval_decision as ts_mod
from soothe.sloop.eval.typesafe_eval_decision import decide_eval_coverage_typesafe

langchain_typesafe = pytest.importorskip("langchain_typesafe")
from langchain_typesafe.types import ChoiceAnswer, ClassificationResponse, Usage  # noqa: E402


def _answer(choice: str, probabilities: dict[str, float], confidence: float) -> Any:
    return ChoiceAnswer(
        type="choice", choice=choice, probabilities=probabilities, confidence=confidence
    )


def _response(choice: str, confidence: float) -> ClassificationResponse:
    probabilities = {choice: 0.9, "eval" if choice == "no_eval" else "no_eval": 0.1}
    return ClassificationResponse(
        model="nanojev:Qwen/Qwen3-0.6B",
        request_id=None,
        answers={"coverage": _answer(choice, probabilities, confidence)},
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
    monkeypatch.setattr(ts_mod, "build_typesafe_classifier", lambda _cfg, _q: fake)


def _decide(**over: Any) -> Any:
    return decide_eval_coverage_typesafe(
        user_goal="refactor the auth module",
        step_history_table="- s1 kind=execute status=done",
        task_complexity="simple",
        soothe_config=_CfgStub(**over),
    )


# ---------------------------------------------------------------------------
# Trusted verdicts
# ---------------------------------------------------------------------------


class TestTrusted:
    @pytest.mark.asyncio
    async def test_confident_no_eval_skips_audit(self, monkeypatch) -> None:
        """A confident suppression skips the audit (the only saving TypeSafe
        offers here — the other direction matches the fail-safe anyway)."""
        fake = _FakeClassifier(_response("no_eval", 0.97))
        _patch(monkeypatch, fake)
        assert await _decide(shadow=False) is False
        assert "refactor" in fake.states[0]["goal"]

    @pytest.mark.asyncio
    async def test_confident_eval_runs_audit(self, monkeypatch) -> None:
        fake = _FakeClassifier(_response("eval", 0.93))
        _patch(monkeypatch, fake)
        assert await _decide(shadow=False) is True

    @pytest.mark.asyncio
    async def test_state_is_truncated(self, monkeypatch) -> None:
        fake = _FakeClassifier(_response("eval", 0.93))
        _patch(monkeypatch, fake)
        await decide_eval_coverage_typesafe(
            user_goal="g" * 9000,
            step_history_table="h" * 9000,
            soothe_config=_CfgStub(shadow=False),
        )
        assert len(fake.states[0]["goal"]) == 1200
        assert len(fake.states[0]["step_history"]) == 4000


# ---------------------------------------------------------------------------
# Asymmetric suppression bar
# ---------------------------------------------------------------------------


class TestSuppressionBar:
    @pytest.mark.asyncio
    async def test_no_eval_below_suppression_bar_falls_back(self, monkeypatch) -> None:
        """Trusted (>= min_confidence) but below suppress_min_confidence:
        suppressing an audit needs a higher bar than running one."""
        _patch(monkeypatch, _FakeClassifier(_response("no_eval", 0.82)))
        assert await _decide(shadow=False) is None

    @pytest.mark.asyncio
    async def test_eval_at_same_confidence_still_counts(self, monkeypatch) -> None:
        """The fail-safe direction is not held to the suppression bar."""
        _patch(monkeypatch, _FakeClassifier(_response("eval", 0.82)))
        assert await _decide(shadow=False) is True

    @pytest.mark.asyncio
    async def test_low_confidence_falls_back(self, monkeypatch) -> None:
        _patch(monkeypatch, _FakeClassifier(_response("no_eval", 0.4)))
        assert await _decide(shadow=False) is None

    @pytest.mark.asyncio
    async def test_near_tie_falls_back(self, monkeypatch) -> None:
        response = _response("no_eval", 0.99)
        response.answers["coverage"] = _answer("no_eval", {"no_eval": 0.51, "eval": 0.49}, 0.99)
        _patch(monkeypatch, _FakeClassifier(response))
        assert await _decide(shadow=False) is None


# ---------------------------------------------------------------------------
# Fallback / shadow
# ---------------------------------------------------------------------------


class TestFallback:
    @pytest.mark.asyncio
    async def test_transport_error_falls_back(self, monkeypatch) -> None:
        _patch(monkeypatch, _FakeClassifier(error=RuntimeError("connection refused")))
        assert await _decide(shadow=False) is None

    @pytest.mark.asyncio
    async def test_unknown_choice_falls_back(self, monkeypatch) -> None:
        _patch(monkeypatch, _FakeClassifier(_response("maybe", 0.99)))
        assert await _decide(shadow=False) is None

    @pytest.mark.asyncio
    async def test_disabled_and_missing_config_return_none(self) -> None:
        from soothe_nano.config import ClassifierConfig

        class _Disabled:
            classifier = ClassifierConfig(enabled=False)

        # Default config is shadow=True → sampling only, never a decision.
        assert await _decide() is None
        assert (
            await decide_eval_coverage_typesafe(
                user_goal="g", step_history_table="h", soothe_config=None
            )
            is None
        )
        assert (
            await decide_eval_coverage_typesafe(
                user_goal="g", step_history_table="h", soothe_config=_Disabled()
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_shadow_never_changes_decision(self, monkeypatch) -> None:
        fake = _FakeClassifier(_response("no_eval", 0.99))
        _patch(monkeypatch, fake)
        assert await _decide(shadow=True) is None
        assert fake.states, "shadow still samples the endpoint"


# ---------------------------------------------------------------------------
# Facade: classifier first, LLM otherwise
# ---------------------------------------------------------------------------


class TestFacade:
    @staticmethod
    def _patch_facade(monkeypatch: pytest.MonkeyPatch, verdict: Any) -> None:
        async def _fake(*_a: Any, **_k: Any) -> Any:
            return verdict

        monkeypatch.setattr("soothe.sloop.eval.eval_decision._classify_eval_coverage", _fake)

    @pytest.mark.asyncio
    async def test_classifier_no_eval_skips_llm(self, monkeypatch) -> None:
        from soothe.sloop.eval.eval_decision import decide_eval_required
        from soothe.sloop.intention.models import IntakeLabel

        self._patch_facade(monkeypatch, False)
        decision = await decide_eval_required(
            fast_model=object(),
            user_goal="g",
            step_history=[],
            intake_label=IntakeLabel.SIMPLE,
        )
        assert decision.should_run_eval is False

    @pytest.mark.asyncio
    async def test_classifier_eval_runs(self, monkeypatch) -> None:
        from soothe.sloop.eval.eval_decision import decide_eval_required
        from soothe.sloop.intention.models import IntakeLabel

        self._patch_facade(monkeypatch, True)
        decision = await decide_eval_required(
            fast_model=object(),
            user_goal="g",
            step_history=[],
            intake_label=IntakeLabel.SIMPLE,
        )
        assert decision.should_run_eval is True

    @pytest.mark.asyncio
    async def test_minimal_short_circuits_without_any_call(self, monkeypatch) -> None:
        from soothe.sloop.eval.eval_decision import decide_eval_required
        from soothe.sloop.intention.models import IntakeLabel

        async def _boom(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("classifier must not run for MINIMAL tasks")

        monkeypatch.setattr("soothe.sloop.eval.eval_decision._classify_eval_coverage", _boom)
        decision = await decide_eval_required(
            fast_model=object(),
            user_goal="g",
            step_history=[],
            intake_label=IntakeLabel.MINIMAL,
        )
        assert decision.should_run_eval is False

    @pytest.mark.asyncio
    async def test_no_fast_model_fails_safe(self, monkeypatch) -> None:
        """No model → require Eval, regardless of the classifier."""
        from soothe.sloop.eval.eval_decision import decide_eval_required
        from soothe.sloop.intention.models import IntakeLabel

        self._patch_facade(monkeypatch, False)
        decision = await decide_eval_required(
            fast_model=None,
            user_goal="g",
            step_history=[],
            intake_label=IntakeLabel.SIMPLE,
        )
        assert decision.should_run_eval is True
