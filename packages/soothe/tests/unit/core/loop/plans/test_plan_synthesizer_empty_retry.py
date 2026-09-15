"""Tests for empty-response retry in ``synthesize_plan``.

Thinking models (e.g. glm-5.2 with ``hide_thinking_tokens=True``) can
spend their entire output budget on internal reasoning, producing zero
visible text after thinking-token stripping. ``synthesize_plan`` should
detect this and retry with a nudge message, up to ``_MAX_EMPTY_RETRIES``
times, before returning empty.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from soothe.sloop.plans.plan_synthesizer import (
    _EMPTY_RETRY_NUDGE,
    _MAX_EMPTY_RETRIES,
    synthesize_plan,
)


def _build_ctx() -> types.SimpleNamespace:
    """Minimal LoopRuntimeContext for synthesis tests."""
    loop_state = types.SimpleNamespace(
        goal="build a face detection demo",
        thread_id="t1",
        iteration=0,
        workspace="/ws",
    )
    return types.SimpleNamespace(loop_state=loop_state)


@pytest.mark.asyncio
async def test_empty_then_nonempty_retries_and_succeeds() -> None:
    """First call returns empty (thinking-only); retry returns real plan."""
    ctx = _build_ctx()
    call_count = 0

    async def fake_ainvoke(messages):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate thinking-only response → empty after strip
            return AIMessage(content="")
        # Retry: return a real plan
        return AIMessage(content="## Plan: Revised\n\nReal plan content.")

    llm = types.SimpleNamespace(ainvoke=AsyncMock(side_effect=fake_ainvoke))

    result = await synthesize_plan(
        ctx,
        llm=llm,
        config=None,
        refinement_comments="use deepagents",
        prior_plan="## Plan: Original\n\nOld plan.",
    )

    assert result == "## Plan: Revised\n\nReal plan content."
    assert call_count == 2  # one empty + one retry

    # Verify the nudge message was appended on retry
    second_call_messages = llm.ainvoke.call_args_list[1].args[0]
    assert any(
        isinstance(m, HumanMessage) and _EMPTY_RETRY_NUDGE in str(m.content)
        for m in second_call_messages
    )


@pytest.mark.asyncio
async def test_all_retries_empty_returns_empty() -> None:
    """All attempts return empty → function returns empty after max retries."""
    ctx = _build_ctx()

    async def fake_ainvoke(messages):
        return AIMessage(content="")

    llm = types.SimpleNamespace(ainvoke=AsyncMock(side_effect=fake_ainvoke))

    result = await synthesize_plan(
        ctx,
        llm=llm,
        config=None,
        refinement_comments="use deepagents",
        prior_plan="## Plan: Original\n\nOld plan.",
    )

    assert result == ""
    # Initial call + _MAX_EMPTY_RETRIES retries
    assert llm.ainvoke.call_count == 1 + _MAX_EMPTY_RETRIES


@pytest.mark.asyncio
async def test_nonempty_first_call_no_retry() -> None:
    """Non-empty first response → no retry, single call."""
    ctx = _build_ctx()

    async def fake_ainvoke(messages):
        return AIMessage(content="## Plan: First Try\n\nContent here.")

    llm = types.SimpleNamespace(ainvoke=AsyncMock(side_effect=fake_ainvoke))

    result = await synthesize_plan(
        ctx,
        llm=llm,
        config=None,
        refinement_comments="use deepagents",
        prior_plan="## Plan: Original\n\nOld plan.",
    )

    assert result == "## Plan: First Try\n\nContent here."
    assert llm.ainvoke.call_count == 1


@pytest.mark.asyncio
async def test_whitespace_only_treated_as_empty() -> None:
    """Response with only whitespace → treated as empty → triggers retry."""
    ctx = _build_ctx()
    call_count = 0

    async def fake_ainvoke(messages):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return AIMessage(content="   \n  \t  ")
        return AIMessage(content="## Plan: Retry Worked\n\nReal plan.")

    llm = types.SimpleNamespace(ainvoke=AsyncMock(side_effect=fake_ainvoke))

    result = await synthesize_plan(
        ctx,
        llm=llm,
        config=None,
        refinement_comments="use deepagents",
        prior_plan="## Plan: Original\n\nOld plan.",
    )

    assert result == "## Plan: Retry Worked\n\nReal plan."
    assert call_count == 2


@pytest.mark.asyncio
async def test_llm_exception_returns_empty_no_retry() -> None:
    """LLM exception → returns empty immediately, no retry."""
    ctx = _build_ctx()

    async def fake_ainvoke(messages):
        raise RuntimeError("model unavailable")

    llm = types.SimpleNamespace(ainvoke=AsyncMock(side_effect=fake_ainvoke))

    result = await synthesize_plan(
        ctx,
        llm=llm,
        config=None,
        refinement_comments="use deepagents",
        prior_plan="## Plan: Original\n\nOld plan.",
    )

    assert result == ""
    assert llm.ainvoke.call_count == 1  # exception → no retry


@pytest.mark.asyncio
async def test_nudge_message_appended_only_after_empty() -> None:
    """The nudge HumanMessage is appended only after an empty response."""
    ctx = _build_ctx()
    call_count = 0

    async def fake_ainvoke(messages):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: verify no nudge in messages
            assert not any(
                isinstance(m, HumanMessage) and _EMPTY_RETRY_NUDGE in str(m.content)
                for m in messages
            )
            return AIMessage(content="")
        # Second call: verify nudge IS present
        assert any(
            isinstance(m, HumanMessage) and _EMPTY_RETRY_NUDGE in str(m.content) for m in messages
        )
        return AIMessage(content="## Plan: Got It\n\nPlan body.")

    llm = types.SimpleNamespace(ainvoke=AsyncMock(side_effect=fake_ainvoke))

    result = await synthesize_plan(
        ctx,
        llm=llm,
        config=None,
        refinement_comments="use deepagents",
        prior_plan="## Plan: Original\n\nOld plan.",
    )

    assert result == "## Plan: Got It\n\nPlan body."
