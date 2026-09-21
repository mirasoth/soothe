"""Unit tests for TypeSafe intent classification with LLM fallback."""

from __future__ import annotations

from typing import Any

import pytest

from soothe.sloop.intention import typesafe_intent as ti_mod
from soothe.sloop.intention.models import IntakeLabel, ResponseLanguage
from soothe.sloop.intention.typesafe_intent import classify_intent_typesafe

langchain_typesafe = pytest.importorskip("langchain_typesafe")
from langchain_typesafe.types import ChoiceAnswer, ClassificationResponse, Usage  # noqa: E402


def _answer(choice: str, probabilities: dict[str, float], confidence: float) -> Any:
    return ChoiceAnswer(
        type="choice", choice=choice, probabilities=probabilities, confidence=confidence
    )


def _label_distribution(label: str) -> dict[str, float]:
    """A decisive distribution: clear top-1 with a wide margin."""
    others = [member.value for member in IntakeLabel if member.value != label]
    return {label: 0.88, others[0]: 0.07, others[1]: 0.03, others[2]: 0.02}


def _language_distribution(language: str) -> dict[str, float]:
    others = [member.value for member in ResponseLanguage if member.value != language]
    return {language: 0.90, others[0]: 0.05, others[1]: 0.03, others[2]: 0.02}


def _confident(label: str, *, language: str | None = "en") -> ClassificationResponse:
    answers = {"intent": _answer(label, _label_distribution(label), 0.95)}
    if language is not None:
        answers["language"] = _answer(language, _language_distribution(language), 0.93)
    return ClassificationResponse(
        model="nanojev:Qwen/Qwen3-0.6B", request_id=None, answers=answers, usage=Usage()
    )


class _FakeClassifier:
    """Stands in for the langchain runnable (no network)."""

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
    """Minimal config stub exposing nano's `classifier` block."""

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


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake: _FakeClassifier) -> None:
    monkeypatch.setattr(
        ti_mod,
        "build_typesafe_classifier",
        lambda _cfg, _questions: fake,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestTrustedVerdicts:
    """Trusted verdicts route without an LLM round trip."""

    @pytest.mark.asyncio
    async def test_complex_label_routes_without_llm(self, monkeypatch) -> None:
        fake = _FakeClassifier(_confident("complex"))
        _patch_client(monkeypatch, fake)
        intent = await classify_intent_typesafe(
            "refactor the auth module across the repo", soothe_config=_CfgStub()
        )
        assert intent is not None
        assert intent.intake_label is IntakeLabel.COMPLEX
        assert intent.task_complexity is not None
        assert intent.response_language is ResponseLanguage.EN
        assert fake.states and "refactor" in fake.states[0]["query"]

    @pytest.mark.asyncio
    async def test_simple_label_and_language(self, monkeypatch) -> None:
        fake = _FakeClassifier(_confident("simple", language="zh"))
        _patch_client(monkeypatch, fake)
        intent = await classify_intent_typesafe("帮我修个bug", soothe_config=_CfgStub())
        assert intent is not None
        assert intent.intake_label is IntakeLabel.SIMPLE
        assert intent.response_language is ResponseLanguage.ZH

    @pytest.mark.asyncio
    async def test_minimal_label(self, monkeypatch) -> None:
        fake = _FakeClassifier(_confident("minimal"))
        _patch_client(monkeypatch, fake)
        intent = await classify_intent_typesafe("what time is it", soothe_config=_CfgStub())
        assert intent is not None
        assert intent.intake_label is IntakeLabel.MINIMAL

    @pytest.mark.asyncio
    async def test_untrusted_language_leaves_field_unset(self, monkeypatch) -> None:
        """Language rides in the same request but is gated on its own
        confidence — a weak language verdict must not misroute the reply."""
        response = _confident("simple")
        response.answers["language"] = _answer("zh", {"zh": 0.9}, 0.35)
        fake = _FakeClassifier(response)
        _patch_client(monkeypatch, fake)
        intent = await classify_intent_typesafe("...", soothe_config=_CfgStub())
        assert intent is not None
        assert intent.intake_label is IntakeLabel.SIMPLE
        assert intent.response_language is None

    @pytest.mark.asyncio
    async def test_missing_language_answer_leaves_field_unset(self, monkeypatch) -> None:
        response = _confident("complex", language=None)
        _patch_client(monkeypatch, _FakeClassifier(response))
        intent = await classify_intent_typesafe("...", soothe_config=_CfgStub())
        assert intent is not None
        assert intent.response_language is None

    @pytest.mark.asyncio
    async def test_query_is_truncated_before_sending(self, monkeypatch) -> None:
        fake = _FakeClassifier(_confident("complex"))
        _patch_client(monkeypatch, fake)
        await classify_intent_typesafe("a" * 5000, soothe_config=_CfgStub())
        assert len(fake.states[0]["query"]) == 1000


# ---------------------------------------------------------------------------
# Fallback conditions
# ---------------------------------------------------------------------------


class TestFallback:
    @pytest.mark.asyncio
    async def test_chitchat_falls_back_for_generated_reply(self, monkeypatch) -> None:
        """A social reply needs generated text — the decision model cannot
        produce it, so the LLM path stays authoritative."""
        fake = _FakeClassifier(_confident("chitchat"))
        _patch_client(monkeypatch, fake)
        assert await classify_intent_typesafe("hi!", soothe_config=_CfgStub()) is None

    @pytest.mark.asyncio
    async def test_low_confidence_falls_back(self, monkeypatch) -> None:
        """Out-of-distribution input: the verdict must not route."""
        response = _confident("complex")
        response.answers["intent"] = _answer("complex", {"complex": 0.9}, 0.37)
        _patch_client(monkeypatch, _FakeClassifier(response))
        assert await classify_intent_typesafe("...", soothe_config=_CfgStub()) is None

    @pytest.mark.asyncio
    async def test_near_tie_falls_back(self, monkeypatch) -> None:
        response = _confident("complex")
        response.answers["intent"] = _answer("simple", {"simple": 0.45, "complex": 0.40}, 0.9)
        _patch_client(monkeypatch, _FakeClassifier(response))
        assert await classify_intent_typesafe("...", soothe_config=_CfgStub()) is None

    @pytest.mark.asyncio
    async def test_transport_error_falls_back(self, monkeypatch) -> None:
        _patch_client(monkeypatch, _FakeClassifier(error=RuntimeError("connection refused")))
        assert await classify_intent_typesafe("...", soothe_config=_CfgStub()) is None

    @pytest.mark.asyncio
    async def test_missing_label_answer_falls_back(self, monkeypatch) -> None:
        response = _confident("complex")
        response.answers.pop("intent")
        _patch_client(monkeypatch, _FakeClassifier(response))
        assert await classify_intent_typesafe("...", soothe_config=_CfgStub()) is None

    @pytest.mark.asyncio
    async def test_unknown_label_falls_back(self, monkeypatch) -> None:
        response = _confident("banana")
        _patch_client(monkeypatch, _FakeClassifier(response))
        assert await classify_intent_typesafe("...", soothe_config=_CfgStub()) is None

    @pytest.mark.asyncio
    async def test_backend_unavailable_falls_back(self) -> None:
        """No provider / missing dependency → None (caller uses the LLM)."""
        assert await classify_intent_typesafe("...", soothe_config=None) is None
        from soothe_nano.config import ClassifierConfig

        class _Disabled:
            classifier = ClassifierConfig(enabled=False)

        assert await classify_intent_typesafe("...", soothe_config=_Disabled()) is None


# ---------------------------------------------------------------------------
# Facade integration: TypeSafe first, LLM otherwise
# ---------------------------------------------------------------------------


class _CoordinatorStub:
    """Counts LLM-path invocations; the result shape is irrelevant because the
    facade's `_intake_to_intent` projection is stubbed in these tests."""

    def __init__(self) -> None:
        self.calls = 0

    async def classify(self, query: str, **_kwargs: Any) -> Any:
        self.calls += 1
        return object()


def _facade(coordinator: _CoordinatorStub, **cfg: Any) -> Any:
    from soothe.sloop.intention.classifier import IntentClassifier

    clf = IntentClassifier.__new__(IntentClassifier)
    clf._fast_model = object()
    clf._assistant_name = "Soothe"
    clf._soothe_config = _CfgStub(**cfg)
    clf._coordinator = coordinator
    clf._intake_classifier = None
    return clf


class TestFacadeFallback:
    @pytest.mark.asyncio
    async def test_typesafe_hit_skips_llm(self, monkeypatch) -> None:
        coordinator = _CoordinatorStub()
        clf = _facade(coordinator)

        async def _hit(_q: str) -> Any:
            return _llm_free_intent()

        monkeypatch.setattr(clf, "_classify_typesafe", _hit)
        intent = await clf.classify_intake("refactor auth")
        assert intent.intake_label is IntakeLabel.COMPLEX
        assert coordinator.calls == 0

    @pytest.mark.asyncio
    async def test_typesafe_miss_uses_llm(self, monkeypatch) -> None:
        coordinator = _CoordinatorStub()
        clf = _facade(coordinator)

        async def _miss(_q: str) -> Any:
            return None

        monkeypatch.setattr(clf, "_classify_typesafe", _miss)
        monkeypatch.setattr(clf, "_project_ledger_for_intake", lambda _m: None)
        monkeypatch.setattr(
            clf,
            "_intake_to_intent",
            lambda _r, _q: _llm_free_intent(),
        )
        intent = await clf.classify_intake("refactor auth")
        assert intent.intake_label is IntakeLabel.COMPLEX
        assert coordinator.calls == 1

    @pytest.mark.asyncio
    async def test_classifier_exception_still_uses_llm(self, monkeypatch) -> None:
        """Any failure inside the classifier degrades, never raises — the
        facade's own try/except is what must hold here."""
        coordinator = _CoordinatorStub()
        clf = _facade(coordinator)

        async def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("typesafe exploded")

        monkeypatch.setattr(ti_mod, "classify_intent_typesafe", _boom)
        monkeypatch.setattr(clf, "_project_ledger_for_intake", lambda _m: None)
        monkeypatch.setattr(clf, "_intake_to_intent", lambda _r, _q: _llm_free_intent())
        intent = await clf.classify_intake("refactor auth")
        assert intent.intake_label is IntakeLabel.COMPLEX
        assert coordinator.calls == 1


def _llm_free_intent() -> Any:
    from soothe.sloop.intention.models import (
        IntentClassification,
        derive_task_complexity_from_intake,
    )

    return IntentClassification(
        intake_label=IntakeLabel.COMPLEX,
        reasoning=None,
        task_complexity=derive_task_complexity_from_intake(IntakeLabel.COMPLEX),
    )
