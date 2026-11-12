"""Unit tests for `build_classifier` TypeSafe → LLM fallback resolution.

Covers the resolution order: TypeSafe first, LLM fast-model
fallback whenever TypeSafe fails, `None` when the fast model cannot be
resolved, and `None` when the classifier is disabled (no LLM attempt).
"""

from __future__ import annotations

from typing import Any

import pytest

from soothe.sloop.clarification import typesafe_client as ts_mod
from soothe.sloop.clarification.typesafe_client import build_classifier

langchain_typesafe = pytest.importorskip("langchain_typesafe")


class _FakeFastModel:
    """Stand-in for a resolved `BaseChatModel`."""


class _CfgStub:
    """Minimal `SootheConfig` stand-in for classifier resolution."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        api_key: str = "local-nanojev",
        fast_model: Any = None,
        fast_error: Exception | None = None,
    ) -> None:
        from soothe_nano.config import ClassifierConfig

        self.classifier = ClassifierConfig(enabled=enabled)
        self._fast_model = fast_model
        self._fast_error = fast_error
        self._api_key = api_key

    def classifier_provider_kwargs(self) -> tuple[str, dict[str, Any]]:
        return (
            "typesafe",
            {
                "base_url": "http://127.0.0.1:8767",
                "api_key": self._api_key,
                "model": "jev-latest",
                "timeout": 2.0,
            },
        )

    def create_chat_model(self, role: str = "default") -> Any:
        if self._fast_error is not None:
            raise self._fast_error
        return self._fast_model


def _break_typesafe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force `TypeSafeClassifier` construction to raise (missing key)."""

    def _boom(**_: Any) -> Any:
        raise ValueError("TYPESAFE_API_KEY required")

    monkeypatch.setattr("langchain_typesafe.TypeSafeClassifier", _boom)


class TestBuildClassifierResolution:
    def test_typesafe_ok_returns_typesafe_classifier(self) -> None:
        cfg = _CfgStub(enabled=True, api_key="local-nanojev")
        clf = build_classifier(cfg)
        assert clf is not None
        assert clf.__class__.__name__ == "TypeSafeClassifier"

    def test_typesafe_fails_falls_back_to_llm(self, monkeypatch) -> None:
        _break_typesafe(monkeypatch)
        cfg = _CfgStub(fast_model=_FakeFastModel())
        clf = build_classifier(cfg)
        assert clf is not None
        assert clf.__class__.__name__ == "LLMChoiceClassifier"

    def test_typesafe_fails_fast_model_unresolved_returns_none(self, monkeypatch) -> None:
        _break_typesafe(monkeypatch)
        cfg = _CfgStub(fast_error=RuntimeError("no fast model"))
        assert build_classifier(cfg) is None

    def test_typesafe_fails_fast_model_returns_none(self, monkeypatch) -> None:
        _break_typesafe(monkeypatch)
        cfg = _CfgStub(fast_model=None)
        assert build_classifier(cfg) is None

    def test_disabled_classifier_returns_none_without_llm_attempt(self, monkeypatch) -> None:
        # Even with a fast model available, disabled means None immediately.
        cfg = _CfgStub(enabled=False, fast_model=_FakeFastModel())
        assert build_classifier(cfg) is None

    def test_warning_logged_once_when_fallback_engages(self, monkeypatch, caplog) -> None:
        import logging

        _break_typesafe(monkeypatch)
        # Reset the one-shot flag so the warning fires for this test.
        ts_mod._llm_fallback_warned = False
        cfg = _CfgStub(fast_model=_FakeFastModel())
        with caplog.at_level(logging.WARNING, logger=ts_mod.logger.name):
            build_classifier(cfg)
            build_classifier(cfg)
        warnings = [r for r in caplog.records if "LLM fast-model fallback" in r.getMessage()]
        assert len(warnings) == 1
