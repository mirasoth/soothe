"""Host-side cache-aware token breakdown + event wire-contract tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from soothe.events import (
    StrangeLoopCompletedEvent,
    StrangeLoopPlanPhaseStatusEvent,
    StrangeLoopStepCompletedEvent,
)
from soothe.sloop.utils.token_usage_breakdown import (
    TokenUsageAccumulator,
    extract_usage_breakdown,
    merge_usage_breakdown,
)


def test_extract_langchain_usage_with_cache_details() -> None:
    msg = AIMessage(
        content="hi",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_token_details": {"cache_read": 80, "cache_creation": 5},
        },
    )
    assert extract_usage_breakdown(msg) == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 80,
        "cache_creation_tokens": 5,
        "total_tokens": 120,
    }


def test_extract_openai_cached_tokens() -> None:
    wire = {
        "response_metadata": {
            "token_usage": {
                "prompt_tokens": 50,
                "completion_tokens": 4,
                "total_tokens": 54,
                "prompt_tokens_details": {"cached_tokens": 40},
            }
        }
    }
    assert extract_usage_breakdown(wire)["cache_read_tokens"] == 40


def test_cache_is_subset_of_input_not_re_added_to_total() -> None:
    usage = {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "input_token_details": {"cache_read": 80},
    }
    assert extract_usage_breakdown({"usage_metadata": usage})["total_tokens"] == 120


def test_extract_returns_none_without_usage() -> None:
    assert extract_usage_breakdown(AIMessage(content="hi")) is None
    assert extract_usage_breakdown({"type": "ai", "content": ""}) is None


def test_merge_usage_breakdown_sums_fields() -> None:
    a = {
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_read_tokens": 5,
        "cache_creation_tokens": 1,
        "total_tokens": 12,
    }
    b = {
        "input_tokens": 3,
        "output_tokens": 1,
        "cache_read_tokens": 2,
        "cache_creation_tokens": 0,
        "total_tokens": 4,
    }
    assert merge_usage_breakdown(a, b) == {
        "input_tokens": 13,
        "output_tokens": 3,
        "cache_read_tokens": 7,
        "cache_creation_tokens": 1,
        "total_tokens": 16,
    }


def test_accumulator_dedupes_by_generation_id_and_sums_calls() -> None:
    acc = TokenUsageAccumulator()

    def gen(msg_id: str, total: int) -> tuple:
        return (
            ("ns",),
            "messages",
            ({"id": msg_id, "usage_metadata": {"input_tokens": total, "total_tokens": total}}, {}),
        )

    acc.observe(gen("g1", 10))
    acc.observe(gen("g1", 18))  # cumulative snapshot for same generation: last wins
    acc.observe(gen("g2", 6))
    assert acc.snapshot()["total_tokens"] == 24

    empty = TokenUsageAccumulator()
    assert empty.snapshot() is None


def test_accumulator_ignores_non_messages_chunks() -> None:
    acc = TokenUsageAccumulator()
    acc.observe((("ns",), "custom", {"type": "soothe.stream.tool_call.update"}))
    assert acc.snapshot() is None


def test_events_omit_token_usage_when_unset() -> None:
    completed = StrangeLoopCompletedEvent(
        thread_id="t", status="done", goal_progress="complete", evidence_summary=""
    ).to_dict()
    assert "token_usage" not in completed
    assert completed["total_tokens_used"] == 0

    phase = StrangeLoopPlanPhaseStatusEvent(label="Interpreting goal").to_dict()
    assert "token_usage" not in phase


def test_events_carry_token_usage_breakdown_when_set() -> None:
    usage = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 80,
        "cache_creation_tokens": 5,
        "total_tokens": 120,
    }
    d = StrangeLoopStepCompletedEvent(
        step_id="s1",
        success=True,
        summary="ok",
        duration_ms=1,
        total_tokens_used=555,
        token_usage=usage,
    ).to_dict()
    assert d["total_tokens_used"] == 555
    assert d["token_usage"]["cache_read_tokens"] == 80

    c = StrangeLoopCompletedEvent(
        thread_id="t",
        status="done",
        goal_progress="complete",
        evidence_summary="",
        total_tokens_used=777,
        token_usage=usage,
    ).to_dict()
    assert c["total_tokens_used"] == 777
    assert c["token_usage"]["total_tokens"] == 120


@pytest.mark.asyncio
async def test_compaction_does_not_reset_cumulative_total(monkeypatch) -> None:
    from soothe.sloop.engine.execute import context_window_manager as cwm
    from soothe.sloop.state.schemas import LoopState

    config = MagicMock()
    config.agent.loop.context_window_limit = 200_000
    config.create_chat_model = MagicMock(return_value=MagicMock())

    checkpoint = MagicMock()
    checkpoint.channel_values = {"messages": [HumanMessage(content=f"m{i}") for i in range(8)]}
    tuple_mock = MagicMock()
    tuple_mock.checkpoint = checkpoint
    checkpointer = MagicMock()
    checkpointer.aget_tuple = AsyncMock(return_value=tuple_mock)
    checkpointer.aupdate = AsyncMock()

    manager = cwm.ContextWindowManager(checkpointer, config)
    manager.estimate_checkpoint_tokens = AsyncMock(return_value=50)

    summary = MagicMock()
    summary.content = "compacted summary"
    monkeypatch.setattr(
        "soothe_nano.llm.ainvoke_traced", AsyncMock(return_value=summary), raising=False
    )

    state = LoopState(goal="g", thread_id="thread-1", total_tokens_used=9999)
    await manager.compact_checkpoint_inplace("thread-1", state)

    assert state.total_tokens_used == 9999
    assert state.context_percentage_consumed > 0
