"""Tests for loop-scoped token usage helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from langchain_core.outputs import LLMResult
from soothe_nano.utils.token_usage import (
    DirectLLMTokenTarget,
    accumulate_loop_tokens_from_llm_result,
    extract_token_usage_from_messages,
    merge_direct_llm_tokens_into_state,
)

from soothe.sloop.utils.token_usage import (
    coerce_total_tokens_used,
    direct_llm_token_call_scope,
    estimate_token_usage,
    loop_token_accumulation_scope,
)


def test_coerce_total_tokens_used_parses_and_clamps() -> None:
    assert coerce_total_tokens_used("42") == 42
    assert coerce_total_tokens_used(-5) == 0
    assert coerce_total_tokens_used("bad") == 0


def test_merge_direct_llm_tokens_into_state() -> None:
    state = SimpleNamespace(total_tokens_used=100)
    sink = DirectLLMTokenTarget(total_tokens_used=250)
    delta = merge_direct_llm_tokens_into_state(state, sink)
    assert delta == 250
    assert state.total_tokens_used == 350


def test_accumulate_loop_tokens_from_llm_result_requires_direct_scope() -> None:
    target = DirectLLMTokenTarget()
    response = MagicMock(spec=LLMResult)
    assert accumulate_loop_tokens_from_llm_result(response) == 0

    with loop_token_accumulation_scope(target), direct_llm_token_call_scope():
        with patch(
            "soothe_nano.llm.observability.extract_token_counts_from_llm_result",
            return_value={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        ):
            delta = accumulate_loop_tokens_from_llm_result(response)

    assert delta == 15
    assert target.total_tokens_used == 15


def test_extract_token_usage_sums_all_ai_turns() -> None:
    from langchain_core.messages import AIMessage

    messages = [
        AIMessage(
            content="hop1",
            usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        ),
        AIMessage(
            content="hop2",
            usage_metadata={"input_tokens": 200, "output_tokens": 20, "total_tokens": 220},
        ),
    ]
    usage = extract_token_usage_from_messages(messages)
    assert usage == {"prompt": 300, "completion": 30, "total": 330}


def test_estimate_token_usage_host_alias_actual_first() -> None:
    """Host re-export of the unified estimate_token_usage API (actual-first)."""
    from langchain_core.messages import AIMessage, HumanMessage

    messages = [
        HumanMessage(content="x" * 100_000),
        AIMessage(
            content="done",
            usage_metadata={"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050},
        ),
    ]
    usage = estimate_token_usage(messages)
    assert usage == {
        "input_tokens": 1000,
        "output_tokens": 50,
        "total_tokens": 1050,
        "source": "actual",
    }


def test_estimate_token_usage_host_alias_estimates_on_fallback() -> None:
    """Host re-export estimates input + output when no usage_metadata."""
    from langchain_core.messages import AIMessage, HumanMessage

    messages = [
        HumanMessage(content="prompt " * 200),
        AIMessage(content="response " * 200),
    ]
    usage = estimate_token_usage(messages)
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]
    assert usage["source"] == "estimated"


def test_loop_state_token_usage_source_default_actual() -> None:
    from soothe.sloop.state.schemas import LoopState

    state = LoopState(goal="g", thread_id="t1")
    assert state.token_usage_source() == "actual"
    assert state.token_usage_estimated is False


def test_loop_state_token_usage_source_estimated() -> None:
    from soothe.sloop.state.schemas import LoopState

    state = LoopState(goal="g", thread_id="t1", token_usage_estimated=True)
    assert state.token_usage_source() == "estimated"


def test_loop_state_token_usage_estimated_backcompat_default() -> None:
    """Older persisted LoopState payloads lack the field: default False."""
    from soothe.sloop.state.schemas import LoopState

    state = LoopState.model_validate({"goal": "g", "thread_id": "t1", "total_tokens_used": 347})
    assert state.token_usage_estimated is False
    assert state.token_usage_source() == "actual"


def test_aggregate_wave_metrics_marks_estimated_provenance() -> None:
    """Estimated waves flip the sticky provenance flag on LoopState."""
    from langchain_core.messages import AIMessage, HumanMessage

    from soothe.coreagent import SootheNanoAgent as CoreAgent
    from soothe.sloop.engine.execute.executor import Executor
    from soothe.sloop.state.schemas import LoopState

    executor = Executor(core_agent=MagicMock(spec=CoreAgent), max_parallel_steps=1)
    state = LoopState(goal="g", thread_id="t1")

    # Estimated wave: no usage_metadata anywhere.
    executor._aggregate_wave_metrics(
        [], "", [HumanMessage(content="do the thing"), AIMessage(content="ok")], state
    )
    assert state.total_tokens_used > 0
    assert state.token_usage_estimated is True
    assert state.token_usage_source() == "estimated"


def test_aggregate_wave_metrics_actual_usage_keeps_provenance_actual() -> None:
    from langchain_core.messages import AIMessage, HumanMessage

    from soothe.coreagent import SootheNanoAgent as CoreAgent
    from soothe.sloop.engine.execute.executor import Executor
    from soothe.sloop.state.schemas import LoopState

    executor = Executor(core_agent=MagicMock(spec=CoreAgent), max_parallel_steps=1)
    state = LoopState(goal="g", thread_id="t1")

    executor._aggregate_wave_metrics(
        [],
        "",
        [
            HumanMessage(content="do the thing"),
            AIMessage(
                content="ok",
                usage_metadata={"input_tokens": 500, "output_tokens": 20, "total_tokens": 520},
            ),
        ],
        state,
    )
    assert state.total_tokens_used == 520
    assert state.token_usage_estimated is False
    assert state.token_usage_source() == "actual"


def test_update_goal_tokens_refreshes_snapshot_after_synthesis() -> None:
    """record_progress snapshots tokens before synthesis; refresh covers it."""
    from datetime import UTC, datetime

    from soothe.sloop.state.checkpoint import StrangeLoopCheckpoint, ThreadHealthMetrics
    from soothe.sloop.state.execution_checkpoint import GoalIndexEntry
    from soothe.sloop.state.sloop_manager import StrangeLoopStateManager

    manager = StrangeLoopStateManager.__new__(StrangeLoopStateManager)
    goal = GoalIndexEntry(goal_id="g1", thread_id="t1", tokens_used=100)
    now = datetime.now(UTC)
    checkpoint = StrangeLoopCheckpoint(
        loop_id="loop-1",
        current_thread_id="t1",
        status="running",
        created_at=now,
        updated_at=now,
        thread_health_metrics=ThreadHealthMetrics(thread_id="t1", last_updated=now),
        goal_history=[goal],
    )
    manager._checkpoint = checkpoint

    import asyncio

    asyncio.run(manager.update_goal_tokens(goal, total_tokens=650))

    assert goal.tokens_used == 650
    assert checkpoint.goal_history[0].tokens_used == 650


def test_update_goal_tokens_is_monotonic() -> None:
    """A lower or equal total never decreases the snapshot."""
    from datetime import UTC, datetime

    from soothe.sloop.state.checkpoint import StrangeLoopCheckpoint, ThreadHealthMetrics
    from soothe.sloop.state.execution_checkpoint import GoalIndexEntry
    from soothe.sloop.state.sloop_manager import StrangeLoopStateManager

    manager = StrangeLoopStateManager.__new__(StrangeLoopStateManager)
    goal = GoalIndexEntry(goal_id="g1", thread_id="t1", tokens_used=650)
    now = datetime.now(UTC)
    checkpoint = StrangeLoopCheckpoint(
        loop_id="loop-1",
        current_thread_id="t1",
        status="running",
        created_at=now,
        updated_at=now,
        thread_health_metrics=ThreadHealthMetrics(thread_id="t1", last_updated=now),
        goal_history=[goal],
    )
    manager._checkpoint = checkpoint

    import asyncio

    asyncio.run(manager.update_goal_tokens(goal, total_tokens=300))

    assert goal.tokens_used == 650


def test_update_goal_tokens_noop_without_checkpoint_or_goal() -> None:
    from soothe.sloop.state.execution_checkpoint import GoalIndexEntry
    from soothe.sloop.state.sloop_manager import StrangeLoopStateManager

    manager = StrangeLoopStateManager.__new__(StrangeLoopStateManager)
    manager._checkpoint = None

    import asyncio

    asyncio.run(manager.update_goal_tokens(None, total_tokens=10))
    asyncio.run(
        manager.update_goal_tokens(GoalIndexEntry(goal_id="g", thread_id="t"), total_tokens=10)
    )
