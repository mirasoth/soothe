"""Unit tests for the swappable risk classifier (nano-configured backend)."""

from __future__ import annotations

from typing import Any

import pytest

from soothe.sloop.clarification.risk_classifier import (
    NullRiskClassifier,
    RiskQuery,
    TypeSafeRiskClassifier,
    build_risk_classifier,
)

langchain_typesafe = pytest.importorskip("langchain_typesafe")
from langchain_typesafe.types import ChoiceAnswer  # noqa: E402


def _answer(choice: str, probabilities: dict[str, float], confidence: float) -> Any:
    return ChoiceAnswer(
        type="choice", choice=choice, probabilities=probabilities, confidence=confidence
    )


def _query(tool: str = "run_command", command: str = "pytest -xvs") -> RiskQuery:
    return RiskQuery(tool=tool, args_preview={"command": command})


class _FakeResponse:
    def __init__(self, answer: Any) -> None:
        self.choices = {"risk_decision": answer}
        self.model = "nanojev:Qwen/Qwen3-0.6B"


class _FakeClassifier:
    """Stands in for the langchain runnable (no network)."""

    def __init__(self, answers: list[Any] | None = None, error: Exception | None = None):
        self._answers = answers or []
        self._error = error
        self.states: list[Any] = []

    async def abatch(self, states: list[Any]) -> list[Any]:
        self.states.extend(states)
        if self._error is not None:
            raise self._error
        return [_FakeResponse(a) for a in self._answers[: len(states)]]


def _classifier(answers=None, error=None, **cfg_over) -> TypeSafeRiskClassifier:
    from soothe_nano.config import ClassifierConfig

    cfg = ClassifierConfig(min_confidence=0.8, min_margin=0.15, **cfg_over)
    clf = TypeSafeRiskClassifier.__new__(TypeSafeRiskClassifier)
    clf._config = cfg
    clf._max_states = 32
    clf._classifier = _FakeClassifier(answers, error)
    return clf


# ---------------------------------------------------------------------------
# Trust + margin gates
# ---------------------------------------------------------------------------


class TestGates:
    @pytest.mark.asyncio
    async def test_confident_decision_is_usable(self) -> None:
        clf = _classifier([_answer("reject", {"reject": 0.9, "allow": 0.1}, 0.96)])
        verdicts = await clf.classify([_query()])
        assert verdicts[0].band == "reject"
        assert verdicts[0].usable is True

    @pytest.mark.asyncio
    async def test_low_confidence_is_untrusted(self) -> None:
        """Out-of-distribution input: the label must not drive a decision."""
        clf = _classifier([_answer("reject", {"reject": 0.9, "allow": 0.1}, 0.37)])
        verdicts = await clf.classify([_query()])
        assert verdicts[0].band == "untrusted"
        assert verdicts[0].usable is False

    @pytest.mark.asyncio
    async def test_near_tie_is_untrusted(self) -> None:
        clf = _classifier([_answer("allow", {"allow": 0.45, "reject": 0.40}, 0.9)])
        verdicts = await clf.classify([_query()])
        assert verdicts[0].band == "untrusted"

    @pytest.mark.asyncio
    async def test_escalate_passes_through(self) -> None:
        clf = _classifier(
            [_answer("escalate", {"escalate": 0.8, "allow": 0.15, "reject": 0.05}, 0.91)]
        )
        verdicts = await clf.classify([_query()])
        assert verdicts[0].band == "escalate"

    @pytest.mark.asyncio
    async def test_unknown_label_is_unavailable(self) -> None:
        clf = _classifier([_answer("maybe", {"maybe": 0.9, "allow": 0.1}, 0.95)])
        verdicts = await clf.classify([_query()])
        assert verdicts[0].band == "unavailable"

    @pytest.mark.asyncio
    async def test_transport_error_is_unavailable(self) -> None:
        """A down local server must degrade, never block or raise."""
        clf = _classifier(error=RuntimeError("connection refused"))
        verdicts = await clf.classify([_query(), _query()])
        assert [v.band for v in verdicts] == ["unavailable", "unavailable"]

    @pytest.mark.asyncio
    async def test_missing_answer_is_unavailable(self) -> None:
        """A server returning fewer responses than inputs must not silently
        drop subjects — every input gets a verdict."""
        clf = _classifier(answers=[])
        clf._classifier = _FakeClassifier([])
        verdicts = await clf.classify([_query(), _query()])
        assert [v.band for v in verdicts] == ["unavailable", "unavailable"]

    @pytest.mark.asyncio
    async def test_state_is_redacted_preview_only(self) -> None:
        """The state sent to the endpoint carries the bounded preview only."""
        clf = _classifier([_answer("allow", {"allow": 0.9, "reject": 0.1}, 0.95)])
        query = RiskQuery(
            tool="write_file",
            args_preview={"file_path": "/ws/a.py"},
            goal_summary="refactor auth",
        )
        await clf.classify([query])
        state = clf._classifier.states[0]
        assert state["tool"] == "write_file"
        assert state["args"] == {"file_path": "/ws/a.py"}
        assert state["goal"] == "refactor auth"


# ---------------------------------------------------------------------------
# Factory: nano config → backend
# ---------------------------------------------------------------------------


class _CfgStub:
    def __init__(self, classifier: Any, resolved: Any = None, raises: bool = False):
        self.classifier = classifier
        self._resolved = resolved
        self._raises = raises

    def classifier_provider_kwargs(self):  # noqa: D102
        if self._raises:
            raise RuntimeError("boom")
        return self._resolved


def _classifier_cfg(**over) -> Any:
    from soothe_nano.config import ClassifierConfig

    return ClassifierConfig(**over)


def test_disabled_returns_null_backend() -> None:
    cfg = _CfgStub(_classifier_cfg(enabled=False))
    assert isinstance(build_risk_classifier(cfg), NullRiskClassifier)


def test_missing_provider_returns_null_backend() -> None:
    cfg = _CfgStub(_classifier_cfg(enabled=True), resolved=None)
    assert isinstance(build_risk_classifier(cfg), NullRiskClassifier)


def test_resolution_failure_returns_null_backend() -> None:
    cfg = _CfgStub(_classifier_cfg(enabled=True), raises=True)
    assert isinstance(build_risk_classifier(cfg), NullRiskClassifier)


def test_unsupported_protocol_returns_null_backend() -> None:
    cfg = _CfgStub(_classifier_cfg(enabled=True), resolved=("other", {}))
    assert isinstance(build_risk_classifier(cfg), NullRiskClassifier)


def test_enabled_typesafe_provider_builds_backend() -> None:
    cfg = _CfgStub(
        _classifier_cfg(enabled=True),
        resolved=(
            "typesafe",
            {
                "base_url": "http://127.0.0.1:8767",
                "api_key": "local-nanojev",
                "model": "jev-latest",
                "timeout": 2.0,
                "max_states_per_request": 32,
            },
        ),
    )
    clf = build_risk_classifier(cfg)
    assert isinstance(clf, TypeSafeRiskClassifier)


def test_no_classifier_attribute_returns_null_backend() -> None:
    assert isinstance(build_risk_classifier(object()), NullRiskClassifier)


def test_backends_are_swappable_via_provider_only() -> None:
    """Same decision parameters, different deployment: only kwargs change."""
    base = {
        "model": "jev-latest",
        "timeout": 2.0,
        "max_states_per_request": 32,
        "api_key": "local-nanojev",
    }
    local = _CfgStub(
        _classifier_cfg(enabled=True),
        resolved=("typesafe", {**base, "base_url": "http://127.0.0.1:8767"}),
    )
    hosted = _CfgStub(
        _classifier_cfg(enabled=True),
        resolved=("typesafe", {**base, "base_url": "https://api.typesafe.dev"}),
    )
    local_clf = build_risk_classifier(local)
    hosted_clf = build_risk_classifier(hosted)
    assert isinstance(local_clf, TypeSafeRiskClassifier)
    assert isinstance(hosted_clf, TypeSafeRiskClassifier)
    assert (
        local_clf._classifier.base_url != hosted_clf._classifier.base_url  # noqa: SLF001
    )
    assert local_clf._config.min_confidence == hosted_clf._config.min_confidence  # noqa: SLF001
