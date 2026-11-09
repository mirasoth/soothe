"""Unit tests for the LLM fast-model classifier adapter (IG-LOOP-017).

`LLMChoiceClassifier` is the structural stand-in for `TypeSafeClassifier`
when the TypeSafe backend is unavailable. It must produce the same
`ClassifierResponse` / `ChoiceAnswer` shape so `verdict_trusted` and every
consumer work unchanged, and it must never raise — a failed LLM call
degrades to empty answers (the consumer's fail-safe), mirroring a TypeSafe
backend that returns no answer.
"""

from __future__ import annotations

from typing import Any

import pytest

from soothe.sloop.clarification import llm_classifier as lc_mod
from soothe.sloop.clarification.llm_classifier import LLMChoiceClassifier

langchain_typesafe = pytest.importorskip("langchain_typesafe")
from langchain_typesafe import Choice  # noqa: E402
from langchain_typesafe.types import ClassifierRequest  # noqa: E402


class _StubModel:
    """Minimal chat-model stand-in; the structured invoke is monkeypatched."""


def _request(
    labels: list[str],
    *,
    qid: str = "q1",
    state: dict[str, Any] | None = None,
    instructions: str = "pick the best label",
) -> ClassifierRequest:
    criteria = {label: f"description for {label}" for label in labels}
    return ClassifierRequest(
        state=state or {"goal": "fix the bug"},
        questions={qid: Choice(instructions=instructions, criteria=criteria)},
    )


def _patch_invoke_ok(
    monkeypatch: pytest.MonkeyPatch,
    result: dict[str, Any],
) -> None:
    """Patch the structured-invoke + rate-limit policy to return ``result``."""

    async def _fake_invoke(
        model: Any, messages: Any, *, json_schema: Any, schema_name: Any, strict: Any, config: Any
    ) -> dict[str, Any]:
        return result

    async def _fake_policy(factory: Any, *, config: Any, **_: Any) -> Any:
        return await factory()

    monkeypatch.setattr(lc_mod, "invoke_structured_chat", _fake_invoke)
    monkeypatch.setattr(lc_mod, "llm_rate_limit_config_from", lambda _cfg: object())
    monkeypatch.setattr(lc_mod, "await_with_llm_call_policy", _fake_policy)


class TestAinvoke:
    @pytest.mark.asyncio
    async def test_returns_choice_answer_with_valid_label(self, monkeypatch) -> None:
        _patch_invoke_ok(
            monkeypatch,
            {
                "questions": {
                    "q1": {
                        "choice": "split",
                        "confidence": 0.82,
                        "probabilities": {"split": 0.8, "finish": 0.2},
                    }
                }
            },
        )
        clf = LLMChoiceClassifier(_StubModel())
        resp = await clf.ainvoke(_request(["finish", "split"]))
        ans = resp.answers["q1"]
        assert ans.choice == "split"
        assert ans.confidence == pytest.approx(0.82)
        assert set(ans.probabilities) == {"finish", "split"}

    @pytest.mark.asyncio
    async def test_invalid_choice_label_skipped(self, monkeypatch) -> None:
        _patch_invoke_ok(
            monkeypatch,
            {
                "questions": {
                    "q1": {
                        "choice": "bogus",
                        "confidence": 0.9,
                        "probabilities": {"finish": 0.5, "split": 0.5},
                    }
                }
            },
        )
        clf = LLMChoiceClassifier(_StubModel())
        resp = await clf.ainvoke(_request(["finish", "split"]))
        assert resp.answers == {}

    @pytest.mark.asyncio
    async def test_invoke_failure_returns_empty_answers(self, monkeypatch) -> None:
        async def _boom(*a: Any, **k: Any) -> dict[str, Any]:
            raise RuntimeError("model down")

        async def _policy(factory: Any, *, config: Any, **_: Any) -> Any:
            return await factory()

        monkeypatch.setattr(lc_mod, "invoke_structured_chat", _boom)
        monkeypatch.setattr(lc_mod, "llm_rate_limit_config_from", lambda _cfg: object())
        monkeypatch.setattr(lc_mod, "await_with_llm_call_policy", _policy)
        clf = LLMChoiceClassifier(_StubModel())
        resp = await clf.ainvoke(_request(["a", "b"]))
        assert resp.answers == {}

    @pytest.mark.asyncio
    async def test_empty_questions_returns_empty(self) -> None:
        clf = LLMChoiceClassifier(_StubModel())
        resp = await clf.ainvoke(ClassifierRequest(state={"x": 1}, questions={}))
        assert resp.answers == {}

    @pytest.mark.asyncio
    async def test_probabilities_normalized_to_sum_one(self, monkeypatch) -> None:
        _patch_invoke_ok(
            monkeypatch,
            {
                "questions": {
                    "q1": {
                        "choice": "a",
                        "confidence": 0.5,
                        "probabilities": {"a": 3.0, "b": 1.0},
                    }
                }
            },
        )
        clf = LLMChoiceClassifier(_StubModel())
        resp = await clf.ainvoke(_request(["a", "b"]))
        probs = resp.answers["q1"].probabilities
        assert abs(sum(probs.values()) - 1.0) < 1e-6
        assert probs["a"] > probs["b"]

    @pytest.mark.asyncio
    async def test_missing_probabilities_yield_uniform(self, monkeypatch) -> None:
        _patch_invoke_ok(
            monkeypatch,
            {"questions": {"q1": {"choice": "a", "confidence": 0.4}}},
        )
        clf = LLMChoiceClassifier(_StubModel())
        resp = await clf.ainvoke(_request(["a", "b", "c"]))
        probs = resp.answers["q1"].probabilities
        assert set(probs) == {"a", "b", "c"}
        assert abs(sum(probs.values()) - 1.0) < 1e-6
        assert all(abs(p - probs["a"]) < 1e-6 for p in probs.values())

    @pytest.mark.asyncio
    async def test_confidence_clamped_to_unit_interval(self, monkeypatch) -> None:
        _patch_invoke_ok(
            monkeypatch,
            {
                "questions": {
                    "q1": {
                        "choice": "a",
                        "confidence": 1.5,
                        "probabilities": {"a": 1.0, "b": 0.0},
                    }
                }
            },
        )
        clf = LLMChoiceClassifier(_StubModel())
        resp = await clf.ainvoke(_request(["a", "b"]))
        assert resp.answers["q1"].confidence == 1.0

    @pytest.mark.asyncio
    async def test_multiple_questions_each_answered(self, monkeypatch) -> None:
        _patch_invoke_ok(
            monkeypatch,
            {
                "questions": {
                    "q1": {"choice": "a", "confidence": 0.9, "probabilities": {"a": 0.9, "b": 0.1}},
                    "q2": {"choice": "y", "confidence": 0.7, "probabilities": {"x": 0.3, "y": 0.7}},
                }
            },
        )
        clf = LLMChoiceClassifier(_StubModel())
        resp = await clf.ainvoke(
            ClassifierRequest(
                state={"goal": "g"},
                questions={
                    "q1": Choice(instructions="i1", criteria={"a": "d", "b": "d"}),
                    "q2": Choice(instructions="i2", criteria={"x": "d", "y": "d"}),
                },
            )
        )
        assert set(resp.answers) == {"q1", "q2"}
        assert resp.answers["q1"].choice == "a"
        assert resp.answers["q2"].choice == "y"
