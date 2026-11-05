"""Tests for the synthesis stream idle watchdog: retry then fallback."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessageChunk

from soothe.config import SootheConfig
from soothe.config.models import AgentConfig, StrangeLoopConfig
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
        agent=AgentConfig(loop=StrangeLoopConfig(dispatch_idle_seconds=_IDLE_SECONDS)),
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
