"""Unit tests for LLM-structured Eval decision (path B)."""

from __future__ import annotations

import pytest
from soothe_nano.protocols.routing import TaskComplexity

from soothe.sloop.eval.eval_decision import decide_eval_required


@pytest.mark.asyncio
async def test_minimal_short_circuits_without_llm() -> None:
    """MINIMAL tasks never call the LLM — short-circuit to should_run_eval=False."""
    decision = await decide_eval_required(
        fast_model=object(),  # would crash if called; proves it isn't
        user_goal="trivial question",
        step_history=[],
        task_complexity=TaskComplexity.MINIMAL,
    )
    assert decision.should_run_eval is False


@pytest.mark.asyncio
async def test_no_fast_model_fails_safe_to_run_eval() -> None:
    """When no fast model is available, fail-safe: require Eval."""
    decision = await decide_eval_required(
        fast_model=None,
        user_goal="do work",
        step_history=[],
        task_complexity=TaskComplexity.SIMPLE,
    )
    assert decision.should_run_eval is True


@pytest.mark.asyncio
async def test_simple_with_no_fast_model_fails_safe() -> None:
    """SIMPLE + no model → should_run_eval=True (fail-safe, not silent skip)."""
    decision = await decide_eval_required(
        fast_model=None,
        user_goal="build the thing",
        step_history=[],
        task_complexity=TaskComplexity.SIMPLE,
    )
    assert decision.should_run_eval is True
    assert "no fast model" in decision.reasoning.lower()


@pytest.mark.asyncio
async def test_pce_evidence_reaches_llm_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """IG-SLP-015: PCE signal lines are appended to the LLM human message."""
    captured: dict[str, object] = {}

    async def fake_structured(_model: object, messages: list, **_kwargs: object) -> dict:
        captured["messages"] = messages
        return {"should_run_eval": True, "reasoning": "audit needed"}

    monkeypatch.setattr(
        "soothe.sloop.eval.eval_decision.ainvoke_structured_traced", fake_structured
    )
    decision = await decide_eval_required(
        fast_model=object(),
        user_goal="do work",
        step_history=[],
        task_complexity=TaskComplexity.SIMPLE,
        pce_evidence=["- kind=stalled conf=0.90 source=pce (untrusted monitor signal)"],
    )
    assert decision.should_run_eval is True
    messages = captured["messages"]
    assert isinstance(messages, list) and len(messages) == 2
    human = str(messages[1].content)
    assert "PCE COVERAGE SIGNALS" in human
    assert "kind=stalled conf=0.90" in human


@pytest.mark.asyncio
async def test_no_pce_evidence_keeps_prompt_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_structured(_model: object, messages: list, **_kwargs: object) -> dict:
        captured["messages"] = messages
        return {"should_run_eval": False, "reasoning": "covered"}

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "soothe.sloop.eval.eval_decision.ainvoke_structured_traced", fake_structured
    )
    await decide_eval_required(
        fast_model=object(),
        user_goal="do work",
        step_history=[],
        task_complexity=TaskComplexity.SIMPLE,
    )
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert "PCE COVERAGE SIGNALS" not in str(messages[1].content)
