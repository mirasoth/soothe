"""Unit tests for TypeSafe LoopRail auto-pick with LLM fallback."""

from __future__ import annotations

from typing import Any

import pytest

from soothe.rails import rail_typesafe as ts_mod
from soothe.rails.rail_typesafe import pick_rail_typesafe

langchain_typesafe = pytest.importorskip("langchain_typesafe")
from langchain_typesafe.types import ChoiceAnswer, ClassifierResponse, Usage  # noqa: E402


def _answer(choice: str, probabilities: dict[str, float], confidence: float) -> Any:
    return ChoiceAnswer(
        type="choice", choice=choice, probabilities=probabilities, confidence=confidence
    )


def _response(choice: str, confidence: float) -> ClassifierResponse:
    probabilities = {choice: 0.9, "abstain" if choice != "abstain" else "spike": 0.1}
    return ClassifierResponse(
        model="nanojev:Qwen/Qwen3-0.6B",
        request_id=None,
        answers={"rail_pick": _answer(choice, probabilities, confidence)},
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


def _rail(rail_id: str, summary: str = "", applies_when: str = "") -> Any:
    """Build a minimal RailDefinition-like object for tests."""
    from dataclasses import dataclass, field
    from pathlib import Path

    @dataclass
    class _Rail:
        id: str = ""
        version: str = "1.0.0"
        summary: str = ""
        applies_when: str = ""
        conditions: dict = field(default_factory=dict)
        flow: list = field(default_factory=list)
        rules: list = field(default_factory=list)
        fanout: dict = field(default_factory=dict)
        verbs: dict = field(default_factory=dict)
        worktrees: dict = field(default_factory=dict)
        auto_pick: bool = True
        source_path: Path | None = None
        integrity_hash: str = "abc123"

    return _Rail(
        id=rail_id,
        summary=summary or f"{rail_id} rail",
        applies_when=applies_when or f"use when {rail_id} applies",
    )


_CANDIDATES = [
    _rail("autoresearch", "Automated research rail", "use for multi-source research"),
    _rail("feature-dev", "Feature development rail", "use for implementing new features"),
    _rail("spike", "Spike/investigation rail", "use for quick exploratory work"),
]


def _pick(**over: Any) -> Any:
    return pick_rail_typesafe(
        description="Build a new feature for user authentication",
        candidates=_CANDIDATES,
        soothe_config=_CfgStub(**over),
    )


# ---------------------------------------------------------------------------
# Trusted verdicts
# ---------------------------------------------------------------------------


class TestTrusted:
    @pytest.mark.asyncio
    async def test_confident_pick_returns_rail_id(self, monkeypatch) -> None:
        fake = _FakeClassifier(_response("feature-dev", 0.93))
        _patch(monkeypatch, fake)
        result = await _pick()
        assert result == "feature-dev"

    @pytest.mark.asyncio
    async def test_confident_abstain_returns_abstain(self, monkeypatch) -> None:
        fake = _FakeClassifier(_response("abstain", 0.91))
        _patch(monkeypatch, fake)
        result = await _pick()
        assert result == "abstain"

    @pytest.mark.asyncio
    async def test_state_contains_description(self, monkeypatch) -> None:
        fake = _FakeClassifier(_response("feature-dev", 0.92))
        _patch(monkeypatch, fake)
        await _pick()
        state = fake.states[0]["state"]
        assert "authentication" in state["job_description"]


# ---------------------------------------------------------------------------
# Suppression bar (rail pick suppresses the LLM call)
# ---------------------------------------------------------------------------


class TestSuppressionBar:
    """Abstaining suppresses the LLM pick (work-suppression direction).

    Picking a rail is the fail-safe direction — no suppression bar needed.
    Only abstain requires suppress_min_confidence.
    """

    @pytest.mark.asyncio
    async def test_abstain_below_suppression_bar_falls_back(self, monkeypatch) -> None:
        """Trusted but below suppress_min_confidence: abstaining needs a
        higher bar because it suppresses the LLM pick."""
        _patch(monkeypatch, _FakeClassifier(_response("abstain", 0.82)))
        assert await _pick() is None

    @pytest.mark.asyncio
    async def test_abstain_at_suppression_bar_passes(self, monkeypatch) -> None:
        _patch(monkeypatch, _FakeClassifier(_response("abstain", 0.90)))
        result = await _pick()
        assert result == "abstain"

    @pytest.mark.asyncio
    async def test_pick_below_suppression_bar_still_returns(self, monkeypatch) -> None:
        """Picking a rail is fail-safe — no suppression bar needed."""
        _patch(monkeypatch, _FakeClassifier(_response("spike", 0.65)))
        result = await _pick()
        assert result == "spike"


# ---------------------------------------------------------------------------
# Untrusted / fallback
# ---------------------------------------------------------------------------


class TestFallback:
    @pytest.mark.asyncio
    async def test_low_confidence_falls_back(self, monkeypatch) -> None:
        _patch(monkeypatch, _FakeClassifier(_response("feature-dev", 0.4)))
        assert await _pick() is None

    @pytest.mark.asyncio
    async def test_near_tie_falls_back(self, monkeypatch) -> None:
        response = _response("feature-dev", 0.99)
        response.answers["rail_pick"] = _answer(
            "feature-dev", {"feature-dev": 0.51, "autoresearch": 0.49}, 0.99
        )
        _patch(monkeypatch, _FakeClassifier(response))
        assert await _pick() is None

    @pytest.mark.asyncio
    async def test_transport_error_falls_back(self, monkeypatch) -> None:
        _patch(monkeypatch, _FakeClassifier(error=RuntimeError("connection refused")))
        assert await _pick() is None

    @pytest.mark.asyncio
    async def test_disabled_config_returns_none(self) -> None:
        from soothe_nano.config import ClassifierConfig

        class _Disabled:
            classifier = ClassifierConfig(enabled=False)

        result = await pick_rail_typesafe(
            description="test",
            candidates=_CANDIDATES,
            soothe_config=_Disabled(),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_none_config_returns_none(self) -> None:
        result = await pick_rail_typesafe(
            description="test",
            candidates=_CANDIDATES,
            soothe_config=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_candidates_returns_none(self) -> None:
        result = await pick_rail_typesafe(
            description="test",
            candidates=[],
            soothe_config=_CfgStub(),
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
        assert await _pick() is None
