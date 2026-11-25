"""Tests for the synthesis stream idle watchdog: retry then fallback."""

from __future__ import annotations

import asyncio
import importlib
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessageChunk

from soothe.config import SootheConfig
from soothe.config.models import AgentConfig, StrangeLoopConfig
from soothe.sloop.engine.completion.scenario_classifier import ScenarioClassification
from soothe.sloop.engine.completion.synthesis import (
    _SYNTHESIS_STALL_RETRY_MAX,
    SynthesisGenerator,
)
from soothe.sloop.state.schemas import LoopState, StepExecutionRecord

_IDLE_SECONDS = 0.2


class _ScriptedLlm:
    """LLM stand-in whose astream returns one scripted async generator per call."""

    def __init__(self, scripts: list) -> None:
        self._scripts = list(scripts)
        self.calls = 0

    def astream(self, messages: Any, config: Any = None, **kwargs: Any):
        self.calls += 1
        return self._scripts.pop(0)()  # each script is a zero-arg generator factory


def _stalled_stream():
    async def _stream():
        await asyncio.sleep(30)
        yield AIMessageChunk(content="never")  # pragma: no cover

    return _stream()


def _ok_stream(text: str):
    async def _stream():
        yield AIMessageChunk(content=text)

    return _stream()


def _delayed_stream(delay: float, text: str):
    async def _stream():
        await asyncio.sleep(delay)
        yield AIMessageChunk(content=text)

    return _stream()


def _loop_config(**kwargs: Any) -> SootheConfig:
    return SootheConfig(agent=AgentConfig(loop=StrangeLoopConfig(**kwargs)))


def _partial_then_stall_stream(text: str):
    async def _stream():
        yield AIMessageChunk(content=text)
        await asyncio.sleep(30)

    return _stream()


def _state() -> LoopState:
    return LoopState(
        goal="g",
        thread_id="parent-thread",
        workspace=None,
        step_results=[
            StepExecutionRecord(
                step_id="s1",
                success=True,
                outcome={
                    "type": "generic",
                    "step_input": "run",
                    "output_summary": {"first": "out"},
                },
                error=None,
                duration_ms=1,
                thread_id="parent-thread",
            )
        ],
    )


def _generator(llm: _ScriptedLlm) -> SynthesisGenerator:
    soothe_cfg = SootheConfig(
        agent=AgentConfig(
            loop=StrangeLoopConfig(
                dispatch_idle_seconds=_IDLE_SECONDS,
                goal_synthesis_idle_seconds=_IDLE_SECONDS,
            ),
        ),
    )
    return SynthesisGenerator(llm, MagicMock(), soothe_cfg)


def _collected_text(events: list) -> str:
    parts = []
    for _ns, mode, data in events:
        assert mode == "messages"
        parts.append(str(getattr(data[0], "content", "")))
    return "".join(parts)


async def _collect(gen: SynthesisGenerator) -> list:
    events = []
    async for event in gen.generate_synthesis("g", _state()):
        events.append(event)
    return events


@pytest.mark.asyncio
async def test_stall_with_no_text_retries_and_succeeds() -> None:
    """A stalled empty stream re-dispatches; the retry's report is kept."""
    llm = _ScriptedLlm([_stalled_stream, lambda: _ok_stream("Final report.")])
    gen = _generator(llm)

    events = await _collect(gen)

    assert llm.calls == 2
    assert _collected_text(events) == "Final report."


@pytest.mark.asyncio
async def test_stall_on_all_attempts_yields_fallback() -> None:
    """Persistent stalls exhaust the retry budget and yield the fallback summary."""
    attempts = _SYNTHESIS_STALL_RETRY_MAX + 1
    llm = _ScriptedLlm([_stalled_stream for _ in range(attempts)])
    gen = _generator(llm)

    events = await _collect(gen)

    assert llm.calls == attempts
    assert "Completed 1/1 steps successfully" in _collected_text(events)


@pytest.mark.asyncio
async def test_stall_on_all_attempts_prefers_provided_fallback_text() -> None:
    """A validation-rejected ledger report beats the generated stub on stall."""
    attempts = _SYNTHESIS_STALL_RETRY_MAX + 1
    llm = _ScriptedLlm([_stalled_stream for _ in range(attempts)])
    gen = _generator(llm)

    events = []
    async for event in gen.generate_synthesis("g", _state(), fallback_text="rejected ledger body"):
        events.append(event)

    assert llm.calls == attempts
    assert _collected_text(events) == "rejected ledger body"


@pytest.mark.asyncio
async def test_stall_after_partial_text_is_not_retried() -> None:
    """A stall after partial text keeps the partial report; no duplicate stream."""
    llm = _ScriptedLlm([lambda: _partial_then_stall_stream("partial report")])
    gen = _generator(llm)

    events = await _collect(gen)

    assert llm.calls == 1
    text = _collected_text(events)
    assert text == "partial report"


@pytest.mark.asyncio
async def test_healthy_stream_yields_directly() -> None:
    """A clean stream completes on the first dispatch with no retry."""
    llm = _ScriptedLlm([lambda: _ok_stream("done report")])
    gen = _generator(llm)

    events = await _collect(gen)

    assert llm.calls == 1
    assert _collected_text(events) == "done report"


@pytest.mark.asyncio
async def test_idle_guard_disabled_runs_plain_stream() -> None:
    """A slow-but-healthy stream is not retried while the guard is disabled."""
    soothe_cfg = _loop_config(
        dispatch_idle_seconds=0.2,
        goal_synthesis_idle_seconds=0.0,
    )
    llm = _ScriptedLlm([lambda: _delayed_stream(0.8, "guarded-off report")])
    gen = SynthesisGenerator(llm, MagicMock(), soothe_cfg)

    events = await _collect(gen)

    assert llm.calls == 1
    assert _collected_text(events) == "guarded-off report"


@pytest.mark.asyncio
async def test_reader_uses_synthesis_budget_not_dispatch_deadlock_budget() -> None:
    """A first token between the two budgets must not be treated as a stall."""
    soothe_cfg = _loop_config(
        dispatch_idle_seconds=0.2,
        goal_synthesis_idle_seconds=3.0,
    )
    llm = _ScriptedLlm([lambda: _delayed_stream(1.2, "Late report.")])
    gen = SynthesisGenerator(llm, MagicMock(), soothe_cfg)

    events = await _collect(gen)

    assert llm.calls == 1
    assert _collected_text(events) == "Late report."


@pytest.mark.asyncio
async def test_scratchpad_timeout_uses_dispatch_deadlock_budget(monkeypatch) -> None:
    """Scratchpad streams keep the total timeout from dispatch_idle_seconds."""
    soothe_cfg = _loop_config(
        dispatch_idle_seconds=7.0,
        goal_synthesis_idle_seconds=0.2,
    )
    llm = _ScriptedLlm([lambda: _ok_stream("scratchpad report")])
    gen = SynthesisGenerator(llm, MagicMock(), soothe_cfg)

    async def _force_scratchpad(goal: str, state: LoopState):  # noqa: ARG001
        classification = ScenarioClassification(
            scenario="custom",
            contextual_focus=["Summarize the outcome."],
            evidence_emphasis="Use bullets.",
        )
        return classification, True

    monkeypatch.setattr(gen, "_resolve_classification", _force_scratchpad)

    synthesis_module = importlib.import_module("soothe.sloop.engine.completion.synthesis")
    captured: dict[str, float] = {}
    real_timeout = asyncio.timeout

    class _CapturingAsyncio:
        @staticmethod
        def timeout(seconds: float):
            captured["seconds"] = seconds
            return real_timeout(seconds)

    monkeypatch.setattr(synthesis_module, "asyncio", _CapturingAsyncio)

    events = await _collect(gen)

    assert captured["seconds"] == 7.0
    assert _collected_text(events) == "scratchpad report"


def test_default_idle_budget_differs_from_dispatch_deadlock_budget() -> None:
    """The synthesis idle budget is its own knob (60s vs 240s deadlock watchdog)."""
    gen = SynthesisGenerator(MagicMock(), MagicMock(), SootheConfig())
    assert gen._synthesis_idle_seconds() == 60.0
    assert gen._dispatch_idle_seconds() == 240.0


def test_heavy_model_role_keeps_dispatch_deadlock_budget() -> None:
    """Pinning a non-fast role without setting the field does not lose reports."""
    cfg = _loop_config(goal_synthesis_model_role="default")
    gen = SynthesisGenerator(MagicMock(), MagicMock(), cfg)
    assert gen._synthesis_idle_seconds() == 240.0


def test_explicit_idle_budget_wins_over_role_default() -> None:
    """An explicitly configured budget applies to every model role."""
    cfg = _loop_config(
        goal_synthesis_model_role="default",
        goal_synthesis_idle_seconds=12.0,
    )
    gen = SynthesisGenerator(MagicMock(), MagicMock(), cfg)
    assert gen._synthesis_idle_seconds() == 12.0
